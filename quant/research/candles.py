"""Public OHLCV fetch interface (Binance via ccxt).

PR1: thin stub / interface only. Do not require network in tests.
TODO(PR2): implement resilient fetch_ohlcv with retries, rate limits,
and local candle cache; wire into momentum signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Protocol, Sequence


@dataclass(frozen=True)
class Candle:
    """Single OHLCV bar."""

    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class CandleSource(Protocol):
    """Protocol for OHLCV providers (ccxt, fixtures, mocks)."""

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        since: Optional[int] = None,
        limit: int = 100,
    ) -> Sequence[Candle]:
        """Return recent candles for ``symbol``."""
        ...


def candles_from_ccxt_rows(rows: Sequence[Sequence[Any]]) -> List[Candle]:
    """Convert ccxt ``fetch_ohlcv`` rows ``[ts, o, h, l, c, v]`` to ``Candle``."""
    out: List[Candle] = []
    for row in rows:
        out.append(
            Candle(
                timestamp_ms=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
        )
    return out


class PublicCcxtCandleSource:
    """Notional ccxt wrapper — not called by the PR1 headless runner.

    Instantiation does not hit the network. ``fetch_ohlcv`` creates a
    public (no-key) exchange client on demand.

    TODO(PR2): inject exchange, add backoff, and avoid per-call create.
    """

    def __init__(self, exchange_id: str = "binance") -> None:
        self.exchange_id = exchange_id

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        since: Optional[int] = None,
        limit: int = 100,
    ) -> Sequence[Candle]:
        # Lazy import so unit tests never need ccxt wired for imports.
        import ccxt  # type: ignore

        exchange_cls = getattr(ccxt, self.exchange_id)
        exchange = exchange_cls({"enableRateLimit": True})
        rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit)
        return candles_from_ccxt_rows(rows)
