"""Public OHLCV fetch (Binance via ccxt) and candle helpers.

Public fetch needs **no API keys** — ccxt talks to Binance's public market
data endpoints. Unit tests must **mock** the exchange client or use
``FixtureCandleSource`` / ``candles_from_ccxt_rows`` so they stay network-free.
"""

from __future__ import annotations

import math
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


def closes(candles: Sequence[Candle]) -> List[float]:
    """Extract close prices in chronological order."""
    return [c.close for c in candles]


def log_returns(close_prices: Sequence[float]) -> List[float]:
    """Simple log returns: ``ln(c[i] / c[i-1])`` for ``i >= 1``.

    Raises ``ValueError`` if any non-positive close would make ``log`` undefined.
    """
    if len(close_prices) < 2:
        return []
    out: List[float] = []
    for i in range(1, len(close_prices)):
        prev = close_prices[i - 1]
        cur = close_prices[i]
        if prev <= 0 or cur <= 0:
            raise ValueError("closes must be positive for log returns")
        out.append(math.log(cur / prev))
    return out


class FixtureCandleSource:
    """In-memory candle source for network-free tests and dry runs."""

    def __init__(self, candles_by_symbol: Optional[dict] = None) -> None:
        self._data: dict = dict(candles_by_symbol or {})

    def set_candles(self, symbol: str, candles: Sequence[Candle]) -> None:
        self._data[symbol] = list(candles)

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        since: Optional[int] = None,
        limit: int = 100,
    ) -> Sequence[Candle]:
        _ = timeframe  # fixture ignores timeframe; tests control bars explicitly
        bars = list(self._data.get(symbol, []))
        if since is not None:
            bars = [c for c in bars if c.timestamp_ms >= since]
        if limit is not None and limit > 0:
            bars = bars[-limit:]
        return bars


class PublicCcxtCandleSource:
    """ccxt public OHLCV wrapper (no API keys).

    Reuses a single exchange instance with ``enableRateLimit=True``.
    Instantiation does not hit the network; ``fetch_ohlcv`` does (unless
    an exchange is injected — preferred for tests).
    """

    def __init__(
        self,
        exchange_id: str = "binance",
        exchange: Any = None,
    ) -> None:
        self.exchange_id = exchange_id
        self._exchange = exchange

    def _get_exchange(self) -> Any:
        if self._exchange is not None:
            return self._exchange
        # Lazy import so unit tests never need ccxt wired for imports.
        import ccxt  # type: ignore

        if not hasattr(ccxt, self.exchange_id):
            raise ValueError(f"unknown ccxt exchange_id: {self.exchange_id!r}")
        exchange_cls = getattr(ccxt, self.exchange_id)
        self._exchange = exchange_cls({"enableRateLimit": True})
        return self._exchange

    @staticmethod
    def _validate_symbol(symbol: str) -> None:
        if not symbol or not isinstance(symbol, str):
            raise ValueError("symbol must be a non-empty string")
        if "/" not in symbol:
            raise ValueError(
                f"symbol must look like BASE/QUOTE (e.g. BTC/USDT), got {symbol!r}"
            )

    @staticmethod
    def _validate_timeframe(timeframe: str) -> None:
        if not timeframe or not isinstance(timeframe, str):
            raise ValueError("timeframe must be a non-empty string")

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        since: Optional[int] = None,
        limit: int = 100,
    ) -> Sequence[Candle]:
        self._validate_symbol(symbol)
        self._validate_timeframe(timeframe)
        if limit is not None and limit <= 0:
            raise ValueError(f"limit must be positive, got {limit!r}")

        exchange = self._get_exchange()
        rows = exchange.fetch_ohlcv(
            symbol, timeframe=timeframe, since=since, limit=limit
        )
        if not rows:
            raise ValueError(
                f"empty OHLCV response for {symbol!r} timeframe={timeframe!r} "
                f"(exchange={self.exchange_id})"
            )
        return candles_from_ccxt_rows(rows)
