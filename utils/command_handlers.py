import logging

from telegram import Update, BotCommand
from telegram.ext import ContextTypes, ExtBot, Application

from utils.helpers import format_message, write_json
from portfolio import Portfolio
from constants import CHAT_ID
from data.scraper import fetch_signal as scrape_signal
from data.database import record_signal, get_latest_allocations

logger = logging.getLogger(__name__)

portfolio = Portfolio()

TARGETS_FILE = "config/targets.json"
GENERIC_ERROR_REPLY = "⚠️ Something went wrong. Check logs for details."


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
    ])


async def post_init(application: Application) -> None:
    await set_bot_commands(application.bot)


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


def _allocations_match(a: dict, b: dict) -> bool:
    if a.keys() != b.keys():
        return False
    return all(abs(a[k] - b[k]) < 0.01 for k in a)


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
    await update.message.reply_text("🔍 Fetching latest RSPS signal from TRW...")

    try:
        allocations, signal_time = await scrape_signal()
    except Exception as error:
        logger.error("Failed to fetch signal: %s", error, exc_info=True)
        await _reply(update, "⚠️ Error fetching signal. Check logs for details.", formatted=False)
        return

    if not allocations:
        await update.message.reply_text("⚠️ No allocations found in signal.")
        return

    previous = get_latest_allocations()
    if previous and _allocations_match(previous, allocations):
        await _reply(update, "ℹ️ Signal unchanged — no update.", formatted=False)
        return

    record_signal(allocations)
    _apply_allocations(allocations)
    await _reply(update, _format_signal_message(allocations, signal_time))


async def poll_signal(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled job: check TRW for a new signal; if found, update targets and live-rebalance."""
    if not CHAT_ID:
        logger.warning("CHAT_ID not set — poll_signal cannot send notifications")
        return

    try:
        allocations, signal_time = await scrape_signal()
    except Exception as error:
        logger.error("poll_signal scrape failed: %s", error, exc_info=True)
        return

    if not allocations:
        return

    previous = get_latest_allocations()
    if previous and _allocations_match(previous, allocations):
        logger.info("poll_signal: no change in allocations")
        return

    logger.info("poll_signal: new signal detected, applying and rebalancing")
    record_signal(allocations)
    _apply_allocations(allocations)

    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=f"🆕 *New RSPS Signal detected*\n\n{_format_signal_message(allocations, signal_time)}",
        parse_mode="Markdown",
    )

    try:
        result = portfolio.execute_rebalance(dry_run=False)
    except Exception as error:
        logger.error("poll_signal rebalance failed: %s", error, exc_info=True)
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text="⚠️ Auto-rebalance failed. Check logs for details.",
            parse_mode="Markdown",
        )
        return

    await context.bot.send_message(chat_id=CHAT_ID, text=format_message(result), parse_mode="Markdown")
