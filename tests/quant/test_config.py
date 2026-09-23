"""Tests for quant.config defaults."""

from quant.config import (
    DEFAULT_UNIVERSE,
    PAPER_EQUITY,
    ExecutionMode,
    QuantConfig,
    load_config,
)


def test_paper_equity_default_is_10000():
    assert PAPER_EQUITY == 10_000.0


def test_load_config_defaults_to_paper(monkeypatch):
    monkeypatch.delenv("EXECUTION_MODE", raising=False)
    monkeypatch.delenv("PAPER_EQUITY", raising=False)
    monkeypatch.delenv("QUANT_UNIVERSE", raising=False)
    cfg = load_config()
    assert cfg.execution_mode is ExecutionMode.PAPER
    assert cfg.paper_equity == 10_000.0
    assert cfg.universe == DEFAULT_UNIVERSE
    assert set(cfg.universe) >= {"BTC/USDT", "ETH/USDT", "SOL/USDT"}


def test_load_config_respects_env(monkeypatch):
    monkeypatch.setenv("EXECUTION_MODE", "LIVE")
    monkeypatch.setenv("PAPER_EQUITY", "25000")
    monkeypatch.setenv("QUANT_UNIVERSE", "BTC/USDT,ETH/USDT")
    cfg = load_config()
    assert cfg.execution_mode is ExecutionMode.LIVE
    assert cfg.paper_equity == 25_000.0
    assert cfg.universe == ["BTC/USDT", "ETH/USDT"]


def test_quant_config_frozen_defaults():
    cfg = QuantConfig()
    assert cfg.execution_mode is ExecutionMode.PAPER
    assert cfg.paper_equity == PAPER_EQUITY
