from telegram.ext import ApplicationBuilder, CommandHandler

from constants import BOT_TOKEN
from utils.command_handlers import (
    post_init, post_stop, check, set_target,
    get_targets, get_total, get_spot_balance,
    get_leverage_balance, rebalance, fetch_signal, poll_signal, status, info,
    performance, puller, SIGNAL_POLL_INTERVAL_SECONDS, SIGNAL_POLL_JOB_NAME,
)


if __name__ == "__main__":
    from data.database import init_db
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).post_stop(post_stop).build()

    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("balance", get_spot_balance))
    app.add_handler(CommandHandler("leverage", get_leverage_balance))
    app.add_handler(CommandHandler("get_targets", get_targets))
    app.add_handler(CommandHandler("set_target", set_target))
    app.add_handler(CommandHandler("total", get_total))
    app.add_handler(CommandHandler("rebalance", rebalance))
    app.add_handler(CommandHandler("fetch_signal", fetch_signal))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("info", info))
    app.add_handler(CommandHandler("performance", performance))
    app.add_handler(CommandHandler("puller", puller))

    app.job_queue.run_repeating(
        poll_signal, interval=SIGNAL_POLL_INTERVAL_SECONDS, first=10, name=SIGNAL_POLL_JOB_NAME
    )

    app.run_polling()
