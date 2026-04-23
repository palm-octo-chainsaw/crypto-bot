import logging
from datetime import datetime, timedelta, timezone

from telegram import Update, BotCommand
from telegram.ext import ContextTypes, ExtBot, Application

from utils.helpers import format_message, write_json
from portfolio import Portfolio
from constants import CHAT_ID
from data.scraper import fetch_signal as scrape_signal, TRWRateLimitError, TRWInvalidCredentialsError
from data.database import record_signal, get_latest_message_timestamp

logger = logging.getLogger(__name__)

portfolio = Portfolio()

TARGETS_FILE = "config/targets.json"
GENERIC_ERROR_REPLY = "⚠️ Something went wrong. Check logs for details."

_last_poll_time: datetime | None = None
_last_poll_status: str = "not yet run"
_poll_success_count: int = 0
_poll_failure_count: int = 0


async def set_bot_commands(bot: ExtBot) -> None:
    await bot.set_my_commands([
        BotCommand("check", "Check portfolio rebalance status"),
        BotCommand("balance", "Get current portfolio spot balance"),
        BotCommand("leverage", "Get current portfolio leverage balance"),
        BotCommand("get_targets", "Show current target allocations"),
        BotCommand("set_target", "Set target % for a token (e.g. /set_target BTC 40)"),
        BotCommand("total", "Get total portfolio value"),
        BotCommand("rebalance", "Dry-run rebalance (/rebalance live to execute real trades)"),
        BotCommand("fetch_signal", "Fetch latest RSPS signal from TRW and update targets"),
        BotCommand("status", "Show scheduled poller status"),
    ])


async def post_init(application: Application) -> None:
    await set_bot_commands(application.bot)
    if CHAT_ID:
        await application.bot.send_message(
            chat_id=CHAT_ID,
            text="🟢 *Bot online* — polling TRW every 15 min",
            parse_mode="Markdown",
        )


async def post_stop(application: Application) -> None:
    if CHAT_ID:
        await application.bot.send_message(
            chat_id=CHAT_ID,
            text="🔴 *Bot stopped*",
            parse_mode="Markdown",
        )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(timezone.utc)
    last = _last_poll_time.strftime("%Y-%m-%d %H:%M:%S UTC") if _last_poll_time else "never"
    lines = [
        "📡 *Poller Status*",
        "",
        "Schedule: every 15 min",
        f"Last poll: {last}",
        f"Last result: {_last_poll_status}",
    ]
    if _credentials_invalid:
        lines.append("⚠️ Paused — TRW credentials invalid (update TRW_PASSWORD and restart)")
    remaining = _cooldown_remaining(now)
    if remaining is not None and _rate_limit_until is not None:
        mins = int(remaining.total_seconds() // 60)
        lines.append(
            f"Next poll: {_rate_limit_until:%Y-%m-%d %H:%M UTC} "
            f"(rate-limit cooldown, {mins} min remaining)"
        )
    lines.append(f"Successes: {_poll_success_count}")
    lines.append(f"Failures: {_poll_failure_count} (consecutive: {_scrape_failure_count})")
    await _reply(update, "\n".join(lines))


async def _reply(update: Update, message: str, *, formatted: bool = True) -> None:
    text = format_message(message) if formatted else message
    await update.message.reply_text(text, parse_mode="Markdown")


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(portfolio.listener(), parse_mode="Markdown")


async def get_targets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = "🎯 *Current Targets*\n\n"
    for symbol, percent in portfolio.targets.items():
        message += f"{symbol}: {percent}%\n"
    message += f"\nTotal: {sum(portfolio.targets.values())}%"
    await _reply(update, message)


async def set_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        symbol = context.args[0].upper()
        percent = float(context.args[1])
        if not 0.0 <= percent <= 100.0:
            raise ValueError("Target percentage must be between 0 and 100.")
        portfolio.set_target(symbol, percent)
        write_json(TARGETS_FILE, portfolio.get_targets())
        await _reply(update, f"✅ Target for {symbol} set to {percent}%", formatted=False)
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Usage: /set_target SYMBOL PERCENT")


async def get_total(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        portfolio.update_portfolio()
        _, _, total = portfolio.fetch_live_data()
        await _reply(update, f"💰 *Total Portfolio Value*:\n\n${total:,.2f} USD")
    except Exception as error:
        logger.error("Command failed: %s", error, exc_info=True)
        await _reply(update, GENERIC_ERROR_REPLY, formatted=False)


async def get_spot_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        portfolio.update_portfolio()
        lines = [f"{symbol}: {value}" for symbol, value in portfolio.portfolio.items()]
        await _reply(update, "💰 *Portfolio Balance*:\n\n" + "\n".join(lines))
    except Exception as error:
        logger.error("Command failed: %s", error, exc_info=True)
        await _reply(update, GENERIC_ERROR_REPLY, formatted=False)


async def get_leverage_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        lines = [f"{symbol}: {value}" for symbol, value in portfolio.balance.get_leverage_balance().items()]
        await _reply(update, "📈 *Leverage Portfolio Balance*:\n\n" + "\n".join(lines))
    except Exception as error:
        logger.error("Command failed: %s", error, exc_info=True)
        await _reply(update, GENERIC_ERROR_REPLY, formatted=False)


async def rebalance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    live = context.args and context.args[0].lower() == "live"
    if live:
        await update.message.reply_text(
            "⚠️ *LIVE MODE* — executing real trades on Binance...", parse_mode="Markdown"
        )
    await _reply(update, portfolio.execute_rebalance(dry_run=not live))


def _apply_allocations(allocations: dict) -> None:
    for symbol in portfolio.targets:
        portfolio.targets[symbol] = 0.0
    for symbol, pct in allocations.items():
        portfolio.targets[symbol] = pct
    write_json(TARGETS_FILE, portfolio.targets)


def _format_signal_message(allocations: dict, signal_time: str | None) -> str:
    lines = ["✅ *Targets updated from RSPS Signal*"]
    if signal_time:
        lines.append(f"🕐 Signal posted: {signal_time}")
    lines.append("")
    for symbol, pct in allocations.items():
        lines.append(f"{symbol}: {pct}%")
    lines.append(f"\nTotal: {sum(allocations.values())}%")
    return "\n".join(lines)


async def fetch_signal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(timezone.utc)
    if _credentials_invalid:
        await update.message.reply_text(
            "🔐 TRW credentials are invalid — scrape paused. Update TRW_PASSWORD and restart the container."
        )
        return
    remaining = _cooldown_remaining(now)
    if remaining is not None:
        mins = int(remaining.total_seconds() // 60)
        await update.message.reply_text(
            f"⏳ TRW rate-limit cooldown active — {mins} min remaining. Try again later."
        )
        return

    await update.message.reply_text("🔍 Fetching latest RSPS signal from TRW...")

    try:
        allocations, signal_time = await scrape_signal()
    except TRWRateLimitError as error:
        duration = _set_rate_limit_cooldown(now, error)
        logger.warning("fetch_signal: %s — cooling down for %s min", error, int(duration.total_seconds() // 60))
        await update.message.reply_text(
            f"⏳ TRW rate-limited — scrape paused for {int(duration.total_seconds() // 60)} min."
        )
        return
    except TRWInvalidCredentialsError as error:
        global _credentials_invalid
        _credentials_invalid = True
        logger.error("fetch_signal: %s", error)
        await update.message.reply_text(
            "🔐 TRW rejected credentials. Scrape paused — update TRW_PASSWORD and restart the container."
        )
        return
    except Exception as error:
        logger.error("Failed to fetch signal: %s", error, exc_info=True)
        await _reply(update, "⚠️ Error fetching signal. Check logs for details.", formatted=False)
        return

    if not allocations:
        await update.message.reply_text("⚠️ No allocations found in signal.")
        return

    last_ts = get_latest_message_timestamp()
    if signal_time and last_ts and signal_time == last_ts:
        await _reply(update, f"ℹ️ Signal unchanged — same timestamp ({signal_time}).", formatted=False)
        return

    record_signal(allocations, message_timestamp=signal_time)
    _apply_allocations(allocations)
    await _reply(update, _format_signal_message(allocations, signal_time))


SCRAPE_FAILURE_ALERT_THRESHOLD = 3
RATE_LIMIT_COOLDOWN = timedelta(minutes=40)
_scrape_failure_count = 0
_rate_limit_until: datetime | None = None
_credentials_invalid: bool = False


def _cooldown_remaining(now: datetime) -> timedelta | None:
    if _rate_limit_until and now < _rate_limit_until:
        return _rate_limit_until - now
    return None


def _set_rate_limit_cooldown(now: datetime, error: TRWRateLimitError) -> timedelta:
    """Set _rate_limit_until to retry-after + 1 min (or default 40 min if no hint). Returns duration."""
    global _rate_limit_until
    default_minutes = int(RATE_LIMIT_COOLDOWN.total_seconds() // 60)
    if error.retry_after_minutes is not None:
        minutes = error.retry_after_minutes + 1
    else:
        minutes = default_minutes
    duration = timedelta(minutes=minutes)
    _rate_limit_until = now + duration
    return duration


async def poll_signal(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled job: check TRW for a new signal; if found, update targets and live-rebalance."""
    global _scrape_failure_count, _last_poll_time, _last_poll_status, _poll_success_count, _poll_failure_count, _rate_limit_until, _credentials_invalid

    _last_poll_time = datetime.now(timezone.utc)

    if not CHAT_ID:
        logger.warning("CHAT_ID not set — poll_signal cannot send notifications")
        _last_poll_status = "skipped (no CHAT_ID)"
        return

    if _credentials_invalid:
        logger.info("poll_signal: credentials invalid, skipping")
        _last_poll_status = "paused (invalid credentials)"
        return

    remaining = _cooldown_remaining(_last_poll_time)
    if remaining is not None:
        mins = int(remaining.total_seconds() // 60)
        logger.info("poll_signal: in rate-limit cooldown (%d min remaining), skipping", mins)
        _last_poll_status = f"rate-limit cooldown ({mins} min remaining)"
        return

    try:
        allocations, signal_time = await scrape_signal()
    except TRWRateLimitError as error:
        duration = _set_rate_limit_cooldown(_last_poll_time, error)
        _poll_failure_count += 1
        _last_poll_status = f"rate-limited; cooldown until {_rate_limit_until:%H:%M UTC}"
        logger.warning("poll_signal: %s — cooling down until %s", error, _rate_limit_until)
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=(
                f"⏳ *TRW rate-limited* — pausing scrape for "
                f"{int(duration.total_seconds() // 60)} min."
            ),
            parse_mode="Markdown",
        )
        return
    except TRWInvalidCredentialsError as error:
        _credentials_invalid = True
        _poll_failure_count += 1
        _last_poll_status = "paused (invalid credentials)"
        logger.error("poll_signal: %s — pausing scrape until restart", error)
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=(
                "🔐 *TRW rejected credentials* — scrape paused.\n"
                "Update `TRW_PASSWORD` and restart the container."
            ),
            parse_mode="Markdown",
        )
        return
    except Exception as error:
        _scrape_failure_count += 1
        _poll_failure_count += 1
        _last_poll_status = f"scrape failed: {type(error).__name__}"
        logger.error(
            "poll_signal scrape failed (%d consecutive): %s",
            _scrape_failure_count, error, exc_info=True,
        )
        if _scrape_failure_count == SCRAPE_FAILURE_ALERT_THRESHOLD:
            await context.bot.send_message(
                chat_id=CHAT_ID,
                text=(
                    f"⚠️ *TRW scrape failing* — {_scrape_failure_count} consecutive errors.\n"
                    f"Auto-rebalance is paused. Check logs or run /fetch_signal manually."
                ),
                parse_mode="Markdown",
            )
        return

    if _scrape_failure_count >= SCRAPE_FAILURE_ALERT_THRESHOLD:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text="✅ TRW scrape recovered.",
            parse_mode="Markdown",
        )
    _scrape_failure_count = 0
    _poll_success_count += 1

    if not allocations:
        _last_poll_status = "no allocations parsed"
        return

    last_ts = get_latest_message_timestamp()
    if signal_time and last_ts and signal_time == last_ts:
        logger.info("poll_signal: signal timestamp unchanged (%s)", signal_time)
        _last_poll_status = f"unchanged (timestamp {signal_time})"
        return

    logger.info("poll_signal: new signal detected (timestamp %s), applying and rebalancing", signal_time)
    record_signal(allocations, message_timestamp=signal_time)
    _apply_allocations(allocations)
    _last_poll_status = "new signal detected"

    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=f"🆕 *New RSPS Signal detected*\n\n{_format_signal_message(allocations, signal_time)}",
        parse_mode="Markdown",
    )

    try:
        result = portfolio.execute_rebalance(dry_run=False)
    except Exception as error:
        logger.error("poll_signal rebalance failed: %s", error, exc_info=True)
        _last_poll_status = "rebalance failed"
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text="⚠️ Auto-rebalance failed. Check logs for details.",
            parse_mode="Markdown",
        )
        return

    await context.bot.send_message(chat_id=CHAT_ID, text=format_message(result), parse_mode="Markdown")
