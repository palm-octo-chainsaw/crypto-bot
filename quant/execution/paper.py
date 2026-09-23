"""In-memory paper broker skeleton.

No exchange calls. Starting cash defaults to ``PAPER_EQUITY`` (10_000 USDT).
Fill logic / position updates land in PR2; PR1 exposes ledger state only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from quant.config import PAPER_EQUITY


@dataclass
class PaperFill:
    """Placeholder fill record (unused by runner in PR1)."""

    symbol: str
    side: str
    quantity: float
    price: float
    notional: float


@dataclass
class PaperBroker:
    """Virtual ledger: cash + positions. Never talks to an exchange."""

    cash: float = PAPER_EQUITY
    positions: Dict[str, float] = field(default_factory=dict)
    fills: List[PaperFill] = field(default_factory=list)
    starting_equity: float = PAPER_EQUITY

    def reset(self, equity: float = PAPER_EQUITY) -> None:
        """Reset ledger to a flat cash book."""
        self.cash = equity
        self.starting_equity = equity
        self.positions = {}
        self.fills = []

    def equity(self, marks: Optional[Dict[str, float]] = None) -> float:
        """Mark-to-market equity = cash + sum(qty * mark)."""
        marks = marks or {}
        total = self.cash
        for symbol, qty in self.positions.items():
            price = marks.get(symbol, 0.0)
            total += qty * price
        return total
