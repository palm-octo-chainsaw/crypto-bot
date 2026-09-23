"""Simple equity / PnL summary helpers from the paper ledger."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from quant.execution.paper import PaperBroker


@dataclass(frozen=True)
class PnLSummary:
    """Snapshot of paper book PnL."""

    starting_equity: float
    equity: float
    cash: float
    unrealized_pnl: float
    total_pnl: float
    total_pnl_pct: float

    def as_dict(self) -> Dict[str, float]:
        return {
            "starting_equity": self.starting_equity,
            "equity": self.equity,
            "cash": self.cash,
            "unrealized_pnl": self.unrealized_pnl,
            "total_pnl": self.total_pnl,
            "total_pnl_pct": self.total_pnl_pct,
        }


def summarize_paper(
    broker: PaperBroker,
    marks: Optional[Dict[str, float]] = None,
) -> PnLSummary:
    """Build a PnL summary from ``PaperBroker`` state."""
    marks = marks or {}
    equity = broker.equity(marks)
    starting = broker.starting_equity
    # With no fills yet, positions are empty → unrealized = equity - cash = 0.
    position_value = equity - broker.cash
    # Cost basis unknown until PR2 fills; treat cash delta + MTM as total.
    total_pnl = equity - starting
    pct = (total_pnl / starting) if starting else 0.0
    return PnLSummary(
        starting_equity=starting,
        equity=equity,
        cash=broker.cash,
        unrealized_pnl=position_value,  # proxy until avg-cost tracked
        total_pnl=total_pnl,
        total_pnl_pct=pct,
    )
