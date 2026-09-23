"""Signal protocol and score types (ML-ready later)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence

from quant.research.candles import Candle


@dataclass(frozen=True)
class SignalScore:
    """Normalized signal output for a single symbol.

    ``score`` is typically in ``[-1, 1]`` (short → long). ``confidence``
    in ``[0, 1]``. Extra features go in ``meta`` for future ML models.
    """

    symbol: str
    score: float
    confidence: float = 0.0
    meta: Mapping[str, Any] = field(default_factory=dict)


class SignalProtocol(Protocol):
    """Pluggable signal interface (momentum stub → ML later)."""

    name: str

    def score(
        self,
        symbol: str,
        candles: Sequence[Candle],
        *,
        context: Optional[Mapping[str, Any]] = None,
    ) -> SignalScore:
        """Compute a score from recent candles."""
        ...
