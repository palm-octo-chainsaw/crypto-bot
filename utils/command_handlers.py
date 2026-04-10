from telegram import Update, BotCommand
from telegram.ext import ContextTypes, ExtBot, Application

from utils.helpers import format_message, write_json
from portfolio import Portfolio


portfolio = Portfolio()

TARGETS_FILE = "config/targets.json"


async def set_bot_commands(bot: ExtBot) -> None:
    await bot.set_my_commands([
        BotCommand("check", "Check portfolio rebalance status"),
        BotCommand("balance", "Get current portfolio spot balance"),
        BotCommand("leverage", "Get current portfolio leverage balance"),
        BotCommand("get_targets", "Show current target allocations"),
        BotCommand("set_target", "Set target % for a token (e.g. /set_target BTC 40)"),
        BotCommand("total", "Get total portfolio value"),
        BotCommand("rebalance", "Dry-run rebalance (/rebalance live to execute real trades)"),
    ])


async def post_init(application: Application) -> None:
    await set_bot_commands(application.bot)


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = portfolio.listener()
    await update.message.reply_text(message, parse_mode="Markdown")


async def get_targets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = "🎯 *Current Targets*\n\n"
    total = 0
    for symbol, percent in portfolio.targets.items():
        total += percent
        message += f"{symbol}: {percent}%\n"
    message += f"\nTotal: {total}%"
    await update.message.reply_text(format_message(message), parse_mode="Markdown")


async def set_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        symbol = context.args[0].upper()
        percent = float(context.args[1])
        if not 0.0 <= percent <= 100.0:
            raise ValueError("Target percentage must be between 0 and 100.")
        portfolio.set_target(symbol, percent)
        write_json(TARGETS_FILE, portfolio.get_targets())
        await update.message.reply_text(f"✅ Target for {symbol} set to {percent}%", parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Usage: /set_target SYMBOL PERCENT")


async def get_total(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        total: float = portfolio.fetch_live_data()[-1]
        message = f"💰 *Total Portfolio Value*:\n\n${total:,.2f} USD"
        await update.message.reply_text(format_message(message), parse_mode="Markdown")
    except Exception as error:
        await update.message.reply_text(f"⚠️ Error: {error}", parse_mode="Markdown")


async def get_spot_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        message = "💰 *Portfolio Balance*:\n\n"
        portfolio.update_portfolio()
        for symbol, value in portfolio.portfolio.items():
            message += f"{symbol}: {value}\n"
        await update.message.reply_text(format_message(message), parse_mode="Markdown")
    except Exception as error:
        await update.message.reply_text(f"⚠️ Error: {error}", parse_mode="Markdown")


async def get_leverage_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        message = "📈 *Leverage Portfolio Balance*:\n\n"
        for symbol, value in portfolio.balance.get_leverage_balance().items():
            message += f"{symbol}: {value}\n"
        await update.message.reply_text(format_message(message), parse_mode="Markdown")
    except Exception as error:
        await update.message.reply_text(f"⚠️ Error: {error}", parse_mode="Markdown")


async def rebalance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    live = context.args and context.args[0].lower() == "live"
    if live:
        await update.message.reply_text("⚠️ *LIVE MODE* — executing real trades on Binance...", parse_mode="Markdown")
    message = portfolio.execute_rebalance(dry_run=not live)
    await update.message.reply_text(format_message(message), parse_mode="Markdown")
