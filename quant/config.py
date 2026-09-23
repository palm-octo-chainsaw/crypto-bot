"""Quant overlay configuration.

Defaults are PAPER-only. No exchange API keys are required for v1
(public Binance OHLCV via ccxt + in-memory paper ledger).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from os import getenv
from typing import List


class ExecutionMode(str, Enum):
    """Execution backend selector for the quant overlay."""

    PAPER = "PAPER"
    LIVE = "LIVE"


# Universe: BTC/ETH/SOL quoted in USDT (Binance spot symbols).
DEFAULT_UNIVERSE: List[str] = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
DEFAULT_TIMEFRAME = "1h"
PAPER_EQUITY = 10_000.0  # USDT virtual starting cash


@dataclass(frozen=True)
class RiskCaps:
    """Placeholder risk limits — wired in PR2/PR3.

    These caps are documented so callers know the intended envelope; the
    RiskEngine stub only enforces the kill-switch / degraded refuse path in v1.
    """

    max_position_pct: float = 0.25  # max fraction of equity per symbol
    max_gross_exposure_pct: float = 1.0  # sum of abs notionals / equity
    max_daily_loss_pct: float = 0.05  # kill-switch trigger placeholder
    max_leverage: float = 1.0  # spot-only for v1


@dataclass(frozen=True)
class QuantConfig:
    """Runtime config for the paper quant overlay."""

    universe: List[str] = field(default_factory=lambda: list(DEFAULT_UNIVERSE))
    timeframe: str = DEFAULT_TIMEFRAME
    paper_equity: float = PAPER_EQUITY
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    risk_caps: RiskCaps = field(default_factory=RiskCaps)
    exchange_id: str = "binance"  # public OHLCV only in v1


def load_config() -> QuantConfig:
    """Load config from environment with safe PAPER defaults.

    ``EXECUTION_MODE`` defaults to PAPER. LIVE is accepted as a string for
    tests of the hard-fail path, but automation must never place live orders.
    """
    raw_mode = getenv("EXECUTION_MODE", ExecutionMode.PAPER.value).strip().upper()
    try:
        mode = ExecutionMode(raw_mode)
    except ValueError:
        mode = ExecutionMode.PAPER

    equity_raw = getenv("PAPER_EQUITY", str(PAPER_EQUITY))
    try:
        equity = float(equity_raw)
    except ValueError:
        equity = PAPER_EQUITY

    timeframe = getenv("QUANT_TIMEFRAME", DEFAULT_TIMEFRAME).strip() or DEFAULT_TIMEFRAME
    universe_env = getenv("QUANT_UNIVERSE", "")
    if universe_env.strip():
        universe = [s.strip() for s in universe_env.split(",") if s.strip()]
    else:
        universe = list(DEFAULT_UNIVERSE)

    return QuantConfig(
        universe=universe,
        timeframe=timeframe,
        paper_equity=equity,
        execution_mode=mode,
        risk_caps=RiskCaps(),
    )
