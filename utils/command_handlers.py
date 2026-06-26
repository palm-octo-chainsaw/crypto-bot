import logging
import os
from datetime import datetime, timedelta, timezone

from telegram import Update, BotCommand
from telegram.ext import ContextTypes, ExtBot, Application

from utils.helpers import format_message, write_json
from portfolio import Portfolio
from constants import CHAT_ID
from data.scraper import fetch_signal as scrape_signal, TRWRateLimitError, TRWInvalidCredentialsError
from data.prices import PriceRateLimitError
from data.database import (
    record_signal, get_latest_message_timestamp, get_latest_allocations,
    get_recent_trades, get_snapshot_at_or_before, get_earliest_snapshot,
)

logger = logging.getLogger(__name__)

portfolio = Portfolio()

TARGETS_FILE = "config/targets.json"
GENERIC_ERROR_REPLY = "⚠️ Something went wrong. Check logs for details."

SIGNAL_POLL_INTERVAL_SECONDS = 900  # 15 minutes
SIGNAL_POLL_JOB_NAME = "signal_poll"

_last_poll_time: datetime | None = None
_last_poll_status: str = "not yet run"
_poll_success_count: int = 0
_poll_failure_count: int = 0
_started_at: datetime = datetime.now(timezone.utc)


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
        BotCommand("puller", "Start or stop automatic TRW polling (/puller start|stop)"),
        BotCommand("info", "Show app version, poller, signal, portfolio, connectivity"),
        BotCommand("performance", "Show portfolio performance over 24h/7d/30d/all"),
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
    running = bool(_get_poll_jobs(context))
    lines = [
        "📡 *Poller Status*",
        "",
        f"Puller: {'🟢 running' if running else '🛑 stopped'}",
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


def _get_poll_jobs(context: ContextTypes.DEFAULT_TYPE) -> tuple:
    job_queue = context.job_queue
    if job_queue is None:
        return ()
    return tuple(job_queue.get_jobs_by_name(SIGNAL_POLL_JOB_NAME))


async def puller(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    action = context.args[0].lower() if context.args else None
    if action == "stop":
        jobs = _get_poll_jobs(context)
        if not jobs:
            await _reply(update, "ℹ️ Puller is already stopped.", formatted=False)
            return
        for job in jobs:
            job.schedule_removal()
        await _reply(update, "🛑 Puller stopped — automatic TRW polling paused.", formatted=False)
    elif action == "start":
        if _get_poll_jobs(context):
            await _reply(update, "ℹ️ Puller is already running.", formatted=False)
            return
        context.job_queue.run_repeating(
            poll_signal,
            interval=SIGNAL_POLL_INTERVAL_SECONDS,
            first=10,
            name=SIGNAL_POLL_JOB_NAME,
        )
        await _reply(update, "🟢 Puller started — polling TRW every 15 min.", formatted=False)
    else:
        await update.message.reply_text("⚠️ Usage: /puller [start|stop]")


def _format_uptime(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _format_version_section(now: datetime) -> list[str]:
    version = os.environ.get("APP_VERSION") or "unknown"
    uptime = _format_uptime(now - _started_at)
    return [
        "🛠️ *Version & Uptime*",
        f"Version: `{version}`",
        f"Uptime: {uptime}",
        f"Started: {_started_at:%Y-%m-%d %H:%M UTC}",
    ]


def _format_poller_section(now: datetime) -> list[str]:
    last = _last_poll_time.strftime("%Y-%m-%d %H:%M:%S UTC") if _last_poll_time else "never"
    lines = [
        "📡 *Poller*",
        f"Last poll: {last}",
        f"Last result: {_last_poll_status}",
        f"Successes: {_poll_success_count}  Failures: {_poll_failure_count} (consecutive: {_scrape_failure_count})",
    ]
    if _credentials_invalid:
        lines.append("⚠️ Paused — TRW credentials invalid")
    remaining = _cooldown_remaining(now)
    if remaining is not None and _rate_limit_until is not None:
        mins = int(remaining.total_seconds() // 60)
        lines.append(f"Cooldown: {mins} min remaining (until {_rate_limit_until:%H:%M UTC})")
    return lines


def _format_signal_section() -> list[str]:
    lines = ["📨 *Latest Signal*"]
    try:
        allocations = get_latest_allocations()
        ts = get_latest_message_timestamp()
        if not allocations:
            lines.append("none recorded")
            return lines
        if ts:
            lines.append(f"Posted: {ts}")
        for symbol, pct in allocations.items():
            lines.append(f"{symbol}: {pct}%")
    except Exception as err:
        lines.append(f"⚠️ unavailable ({err})")
    return lines


def _format_portfolio_section() -> list[str]:
    lines = ["💰 *Portfolio*"]
    try:
        portfolio.update_portfolio()
        _, _, total = portfolio.fetch_live_data()
        non_zero = {s: v for s, v in portfolio.portfolio.items() if v > 0}
        for symbol, value in non_zero.items():
            lines.append(f"{symbol}: {value}")
        lines.append(f"Total: ${total:,.2f}")
    except Exception as err:
        lines.append(f"⚠️ unavailable ({err})")
    return lines


def _ping_binance() -> str:
    try:
        client = portfolio.balance.binance_client
        if client is None:
            return "Binance: ⚠️ no credentials"
        client.ping()
        return "Binance: ✅"
    except Exception as err:
        return f"Binance: ❌ ({err})"


def _ping_arbitrum() -> str:
    try:
        return "Arbitrum: ✅" if portfolio.balance.w3.is_connected() else "Arbitrum: ❌"
    except Exception as err:
        return f"Arbitrum: ❌ ({err})"


def _format_connectivity_section() -> list[str]:
    return ["🔌 *Connectivity*", _ping_binance(), _ping_arbitrum()]


def _format_trades_section() -> list[str]:
    lines = ["📜 *Recent Trades*"]
    try:
        trades = get_recent_trades(limit=5)
    except Exception as err:
        lines.append(f"⚠️ unavailable ({err})")
        return lines
    if not trades:
        lines.append("none recorded")
        return lines
    for t in trades:
        ts = t["timestamp"][:19].replace("T", " ")
        marker = " (dry)" if t["dry_run"] else ""
        usd = f"${t['usd_value']:,.2f}" if t["usd_value"] is not None else "—"
        lines.append(f"{ts} {t['side']} {t['symbol']} {t['amount']:.6f} → {usd} [{t['status']}]{marker}")
    return lines


def _format_info() -> str:
    now = datetime.now(timezone.utc)
    sections = [
        _format_version_section(now),
        _format_poller_section(now),
        _format_signal_section(),
        _format_portfolio_section(),
        _format_connectivity_section(),
        _format_trades_section(),
    ]
    blocks = ["\n".join(section) for section in sections]
    return "\n\n".join(blocks)


PERFORMANCE_WINDOWS: list[tuple[str, timedelta | None]] = [
    ("24h", timedelta(hours=24)),
    ("7d", timedelta(days=7)),
    ("30d", timedelta(days=30)),
    ("all", None),
]
PERFORMANCE_WINDOW_KEYS = [label for label, _ in PERFORMANCE_WINDOWS]
PERFORMANCE_USAGE = "⚠️ Usage: /performance [" + "|".join(PERFORMANCE_WINDOW_KEYS) + "]"


def _format_performance_line(label: str, delta: timedelta | None, end_value: float, now: datetime) -> str:
    if delta is None:
        snap = get_earliest_snapshot()
    else:
        snap = get_snapshot_at_or_before(now - delta)
    if snap is None or snap["total_value_usd"] <= 0:
        return f"{label}: insufficient history"
    start_value = snap["total_value_usd"]
    pnl = end_value - start_value
    pct = pnl / start_value * 100
    emoji = "📈" if pnl >= 0 else "📉"
    sign = "+" if pnl >= 0 else "-"
    return f"{label}: {sign}${abs(pnl):,.2f} ({sign}{abs(pct):.2f}%) {emoji}"


def _format_performance(arg: str | None) -> str:
    if arg is not None and arg.lower() not in PERFORMANCE_WINDOW_KEYS:
        return PERFORMANCE_USAGE
    portfolio.update_portfolio()
    _, _, total = portfolio.fetch_live_data()
    now = datetime.now(timezone.utc)
    selected = (
        PERFORMANCE_WINDOWS
        if arg is None
        else [w for w in PERFORMANCE_WINDOWS if w[0] == arg.lower()]
    )
    lines = [
        "📊 *Portfolio Performance*",
        f"Total: ${total:,.2f} USD",
        "",
    ]
    lines.extend(_format_performance_line(label, delta, total, now) for label, delta in selected)
    return "\n".join(lines)


async def performance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = context.args[0] if context.args else None
    try:
        message = _format_performance(arg)
    except Exception as error:
        logger.error("Command failed: %s", error, exc_info=True)
        await _reply(update, GENERIC_ERROR_REPLY, formatted=False)
        return
    await _reply(update, message)


async def info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        message = _format_info()
    except Exception as error:
        logger.error("Command failed: %s", error, exc_info=True)
        await _reply(update, GENERIC_ERROR_REPLY, formatted=False)
        return
    await _reply(update, message)


async def _reply(update: Update, message: str, *, formatted: bool = True) -> None:
    text = format_message(message) if formatted else message
    await update.message.reply_text(text, parse_mode="Markdown")


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        message = portfolio.listener()
    except Exception as error:
        logger.error("Command failed: %s", error, exc_info=True)
        await _reply(update, GENERIC_ERROR_REPLY, formatted=False)
        return
    await update.message.reply_text(message, parse_mode="Markdown")


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


def _allocations_match(a: dict | None, b: dict | None, tol: float = 0.01) -> bool:
    if a is None or b is None:
        return a is b
    keys = set(a) | set(b)
    return all(abs(a.get(k, 0.0) - b.get(k, 0.0)) <= tol for k in keys)


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
    global _credentials_invalid
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
    if signal_time and last_ts and signal_time == last_ts and _allocations_match(allocations, get_latest_allocations()):
        await _reply(update, f"ℹ️ Signal unchanged — same timestamp ({signal_time}).", formatted=False)
        return

    record_signal(allocations, message_timestamp=signal_time)
    _apply_allocations(allocations)
    await _reply(update, _format_signal_message(allocations, signal_time))


SCRAPE_FAILURE_ALERT_THRESHOLD = 3
RATE_LIMIT_COOLDOWN = timedelta(minutes=40)
PRICE_RATE_LIMIT_ALERT_COOLDOWN = timedelta(minutes=10)
_scrape_failure_count = 0
_rate_limit_until: datetime | None = None
_credentials_invalid: bool = False
_price_rate_limit_alerted_at: datetime | None = None


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


async def _alert_price_rate_limit(context: ContextTypes.DEFAULT_TYPE, error: Exception) -> None:
    """Send a Telegram alert on CoinGecko 429, throttled to one per cooldown window."""
    global _price_rate_limit_alerted_at
    now = datetime.now(timezone.utc)
    if (
        _price_rate_limit_alerted_at is not None
        and now - _price_rate_limit_alerted_at < PRICE_RATE_LIMIT_ALERT_COOLDOWN
    ):
        return
    _price_rate_limit_alerted_at = now
    logger.warning("CoinGecko rate-limited: %s", error)
    await context.bot.send_message(
        chat_id=CHAT_ID,
        text="⏳ *CoinGecko rate-limited* — price fetches failing. Will retry next poll.",
        parse_mode="Markdown",
    )


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
    if signal_time and last_ts and signal_time == last_ts and _allocations_match(allocations, get_latest_allocations()):
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
        check_summary = portfolio.listener()
    except PriceRateLimitError as error:
        _last_poll_status = "price API rate-limited"
        await _alert_price_rate_limit(context, error)
        return
    if not portfolio.send_rebalance:
        _last_poll_status = "new signal applied (within drift threshold)"
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=f"✅ Allocations within 3% drift — skipping rebalance.\n\n{check_summary}",
            parse_mode="Markdown",
        )
        return

    try:
        result = portfolio.execute_rebalance(dry_run=False)
    except PriceRateLimitError as error:
        _last_poll_status = "price API rate-limited"
        await _alert_price_rate_limit(context, error)
        return
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
