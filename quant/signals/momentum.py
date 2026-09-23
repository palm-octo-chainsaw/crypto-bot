"""Momentum + volatility filter signal (stub for PR1).

Intended design (implement in PR2)
----------------------------------
1. Momentum leg: lookback return (e.g. 24–72 bars on ``1h``) as primary
   trend score, optionally smoothed (EMA of returns).
2. Volatility filter: realized vol (e.g. std of log returns) must sit
   inside a band — too low = chop / no edge; too high = skip or cut size.
3. Output: ``SignalScore`` with ``score`` in [-1, 1] and ``confidence``
   derived from vol-band membership and sample length.
4. No live orders: this module only scores; sizing/risk live elsewhere.

PR1 ships a stub that returns a neutral score so the package imports and
tests stay network-free.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from quant.research.candles import Candle
from quant.signals.base import SignalScore


class MomentumVolSignal:
    """Stub momentum + vol-filter signal. Real logic lands in PR2."""

    name = "momentum_vol"

    def __init__(
        self,
        lookback: int = 24,
        vol_lookback: int = 24,
        min_vol: float = 0.0,
        max_vol: float = 1.0,
    ) -> None:
        self.lookback = lookback
        self.vol_lookback = vol_lookback
        self.min_vol = min_vol
        self.max_vol = max_vol

    def score(
        self,
        symbol: str,
        candles: Sequence[Candle],
        *,
        context: Optional[Mapping[str, Any]] = None,
    ) -> SignalScore:
        """Return a neutral stub score until PR2 implements the filter."""
        _ = context  # reserved for regime / risk context later
        return SignalScore(
            symbol=symbol,
            score=0.0,
            confidence=0.0,
            meta={
                "stub": True,
                "n_candles": len(candles),
                "lookback": self.lookback,
                "vol_lookback": self.vol_lookback,
            },
        )
