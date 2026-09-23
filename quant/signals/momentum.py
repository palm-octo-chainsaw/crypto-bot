"""Momentum + volatility-band filter signal (score only -- no trading).

Design (documented; **not** an alpha claim)
-------------------------------------------
1. **Momentum**: simple lookback return

       momentum_return = close[-1] / close[-lookback] - 1

   Default ``lookback=24`` on a ``1h`` series (~1 trading day of bars).

2. **Score map**: clip the return into ``[-1, 1]``:

       score = clip(momentum_return / MOMENTUM_SCALE, -1, 1)

   with ``MOMENTUM_SCALE = 0.05`` (a 5% lookback return maps to +/-1).
   This is a convenience normalization, not a calibrated edge model.

3. **Vol filter**: realized vol = sample stdev of log returns over the
   last ``vol_lookback`` returns (needs ``vol_lookback + 1`` closes).
   If ``vol < min_vol`` or ``vol > max_vol`` -> ``score=0``, ``confidence=0``,
   ``meta["reason"] = "vol_filter"``.

4. **Confidence**: ``0`` on insufficient data or failed vol filter; otherwise
   higher when realized vol sits near the middle of ``[min_vol, max_vol]``.

5. **Minimum bars**: ``max(lookback, vol_lookback) + 1``. Fewer candles ->
   neutral score with ``meta["reason"] = "insufficient_data"``.

This module only produces a ``SignalScore``. It does not place orders.
"""

from __future__ import annotations

import statistics
from typing import Any, Mapping, Optional, Sequence

from quant.research.candles import Candle, closes, log_returns
from quant.signals.base import SignalScore

# Return that maps to |score| == 1 after clipping (e.g. +/-5% lookback move).
MOMENTUM_SCALE = 0.05

# Default realized-vol band on hourly log returns (illustrative, not optimized).
DEFAULT_MIN_VOL = 0.002
DEFAULT_MAX_VOL = 0.05


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class MomentumVolSignal:
    """Lookback momentum with a realized-vol band filter.

    Constructor params override module defaults; nothing is read from env.
    """

    name = "momentum_vol"

    def __init__(
        self,
        lookback: int = 24,
        vol_lookback: int = 24,
        min_vol: float = DEFAULT_MIN_VOL,
        max_vol: float = DEFAULT_MAX_VOL,
        momentum_scale: float = MOMENTUM_SCALE,
    ) -> None:
        if lookback < 1:
            raise ValueError("lookback must be >= 1")
        if vol_lookback < 2:
            raise ValueError("vol_lookback must be >= 2 (need >=2 log returns for stdev)")
        if min_vol < 0 or max_vol < 0:
            raise ValueError("min_vol and max_vol must be non-negative")
        if min_vol > max_vol:
            raise ValueError("min_vol must be <= max_vol")
        if momentum_scale <= 0:
            raise ValueError("momentum_scale must be > 0")
        self.lookback = lookback
        self.vol_lookback = vol_lookback
        self.min_vol = min_vol
        self.max_vol = max_vol
        self.momentum_scale = momentum_scale

    @property
    def min_candles(self) -> int:
        """Minimum bars required for a non-neutral score."""
        return max(self.lookback, self.vol_lookback) + 1

    def score(
        self,
        symbol: str,
        candles: Sequence[Candle],
        *,
        context: Optional[Mapping[str, Any]] = None,
    ) -> SignalScore:
        """Compute momentum score; apply vol band filter. No trading."""
        _ = context  # reserved for regime / risk context later
        n = len(candles)
        base_meta: dict = {
            "lookback": self.lookback,
            "vol_lookback": self.vol_lookback,
            "momentum_scale": self.momentum_scale,
            "min_vol": self.min_vol,
            "max_vol": self.max_vol,
            "n_candles": n,
            "min_candles": self.min_candles,
            "vol_filter_pass": False,
        }

        if n < self.min_candles:
            return SignalScore(
                symbol=symbol,
                score=0.0,
                confidence=0.0,
                meta={**base_meta, "reason": "insufficient_data"},
            )

        price_closes = closes(candles)
        start = price_closes[-self.lookback]
        end = price_closes[-1]
        if start <= 0 or end <= 0:
            return SignalScore(
                symbol=symbol,
                score=0.0,
                confidence=0.0,
                meta={**base_meta, "reason": "invalid_prices"},
            )

        momentum_return = end / start - 1.0
        raw_score = _clip(momentum_return / self.momentum_scale, -1.0, 1.0)

        # Realized vol over the last vol_lookback log returns.
        lr = log_returns(price_closes[-(self.vol_lookback + 1) :])
        if len(lr) < 2:
            return SignalScore(
                symbol=symbol,
                score=0.0,
                confidence=0.0,
                meta={
                    **base_meta,
                    "momentum_return": momentum_return,
                    "reason": "insufficient_data",
                },
            )
        realized_vol = statistics.stdev(lr)

        meta = {
            **base_meta,
            "momentum_return": momentum_return,
            "realized_vol": realized_vol,
        }

        if realized_vol < self.min_vol or realized_vol > self.max_vol:
            return SignalScore(
                symbol=symbol,
                score=0.0,
                confidence=0.0,
                meta={**meta, "reason": "vol_filter", "vol_filter_pass": False},
            )

        # Confidence peaks at band midpoint; 0 at the edges.
        band_half = (self.max_vol - self.min_vol) / 2.0
        if band_half <= 0:
            confidence = 1.0
        else:
            mid = (self.min_vol + self.max_vol) / 2.0
            confidence = _clip(1.0 - abs(realized_vol - mid) / band_half, 0.0, 1.0)

        return SignalScore(
            symbol=symbol,
            score=raw_score,
            confidence=confidence,
            meta={**meta, "reason": "ok", "vol_filter_pass": True},
        )
