"""Tests for ExecutionRouter PAPER vs LIVE hard-fail."""

import pytest

from quant.config import ExecutionMode, QuantConfig
from quant.execution.paper import PaperBroker
from quant.execution.router import ExecutionRouter, LiveExecutionLockedError


def test_paper_mode_returns_broker():
    cfg = QuantConfig(execution_mode=ExecutionMode.PAPER, paper_equity=10_000.0)
    router = ExecutionRouter(config=cfg)
    broker = router.broker()
    assert isinstance(broker, PaperBroker)
    assert broker.cash == 10_000.0


def test_paper_submit_is_noop():
    cfg = QuantConfig(execution_mode=ExecutionMode.PAPER)
    router = ExecutionRouter(config=cfg)
    assert router.submit("BTC/USDT", "buy", 0.01, price=50_000.0) is None


def test_live_broker_raises():
    cfg = QuantConfig(execution_mode=ExecutionMode.LIVE)
    router = ExecutionRouter(config=cfg)
    with pytest.raises(LiveExecutionLockedError):
        router.broker()


def test_live_submit_raises_not_implemented():
    cfg = QuantConfig(execution_mode=ExecutionMode.LIVE)
    router = ExecutionRouter(config=cfg)
    with pytest.raises((NotImplementedError, LiveExecutionLockedError, RuntimeError)):
        router.submit("BTC/USDT", "buy", 0.01)
