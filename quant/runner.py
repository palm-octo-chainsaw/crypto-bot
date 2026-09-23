"""Headless one-shot runner for the paper quant overlay.

Loads config, logs execution mode + paper equity / PnL. Does NOT trade.
No Telegram commands in PR1.
"""

from __future__ import annotations

import logging
import sys

from quant.config import ExecutionMode, load_config
from quant.execution.paper import PaperBroker
from quant.execution.router import ExecutionRouter, LiveExecutionLockedError
from quant.pnl import summarize_paper

logger = logging.getLogger("quant.runner")


def run_once() -> int:
    """Load config, assert PAPER routing, log equity summary. No orders."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    config = load_config()
    logger.info(
        "quant runner start mode=%s universe=%s timeframe=%s paper_equity=%.2f",
        config.execution_mode.value,
        config.universe,
        config.timeframe,
        config.paper_equity,
    )

    if config.execution_mode is ExecutionMode.LIVE:
        logger.error(
            "EXECUTION_MODE=LIVE refused: quant overlay is PAPER-only; "
            "automation must never place live orders"
        )
        return 2

    router = ExecutionRouter(config=config)
    try:
        broker: PaperBroker = router.broker()
    except LiveExecutionLockedError:
        logger.exception("execution router refused non-PAPER mode")
        return 2

    summary = summarize_paper(broker)
    logger.info(
        "paper ledger equity=%.2f cash=%.2f total_pnl=%.2f (%.2f%%) — no trades placed",
        summary.equity,
        summary.cash,
        summary.total_pnl,
        summary.total_pnl_pct * 100.0,
    )
    return 0


def main() -> None:
    sys.exit(run_once())


if __name__ == "__main__":
    main()
