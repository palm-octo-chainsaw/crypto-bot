import datetime as dt
import logging

from telegram.ext import ApplicationBuilder, CommandHandler

logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

from constants import BOT_TOKEN
from utils.command_handlers import (
    post_init, check, set_target,
    get_targets, get_total, get_spot_balance,
    get_leverage_balance, rebalance, fetch_signal, poll_signal, status,
)


# Poll TRW every 10 min between 00:00 and 01:00 UTC (exclusive end).
POLL_TIMES_UTC = [
    dt.time(0, minute, tzinfo=dt.timezone.utc) for minute in range(0, 60, 10)
]


if __name__ == "__main__":
    from data.database import init_db
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("balance", get_spot_balance))
    app.add_handler(CommandHandler("leverage", get_leverage_balance))
    app.add_handler(CommandHandler("get_targets", get_targets))
    app.add_handler(CommandHandler("set_target", set_target))
    app.add_handler(CommandHandler("total", get_total))
    app.add_handler(CommandHandler("rebalance", rebalance))
    app.add_handler(CommandHandler("fetch_signal", fetch_signal))
    app.add_handler(CommandHandler("status", status))

    for poll_time in POLL_TIMES_UTC:
        app.job_queue.run_daily(poll_signal, time=poll_time)

    app.run_polling()
