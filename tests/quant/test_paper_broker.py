"""Tests for PaperBroker starting equity."""

from quant.config import PAPER_EQUITY
from quant.execution.paper import PaperBroker
from quant.pnl import summarize_paper


def test_starting_equity_10000():
    broker = PaperBroker()
    assert broker.cash == 10_000.0
    assert broker.starting_equity == PAPER_EQUITY
    assert broker.positions == {}
    assert broker.equity() == 10_000.0


def test_reset_restores_equity():
    broker = PaperBroker()
    broker.cash = 1.0
    broker.positions["BTC/USDT"] = 0.1
    broker.reset()
    assert broker.cash == 10_000.0
    assert broker.positions == {}


def test_pnl_summary_flat_book():
    broker = PaperBroker()
    summary = summarize_paper(broker)
    assert summary.equity == 10_000.0
    assert summary.total_pnl == 0.0
    assert summary.cash == 10_000.0
