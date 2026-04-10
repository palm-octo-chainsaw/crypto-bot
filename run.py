from telegram.ext import ApplicationBuilder, CommandHandler

from constants import BOT_TOKEN
from utils.command_handlers import (
    portfolio, post_init, check, set_target,
    get_targets, get_total, get_spot_balance,
    get_leverage_balance, rebalance
)


if __name__ == "__main__":
    portfolio.process()

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("balance", get_spot_balance))
    app.add_handler(CommandHandler("leverage", get_leverage_balance))
    app.add_handler(CommandHandler("get_targets", get_targets))
    app.add_handler(CommandHandler("set_target", set_target))
    app.add_handler(CommandHandler("total", get_total))
    app.add_handler(CommandHandler("rebalance", rebalance))

    app.run_polling()
