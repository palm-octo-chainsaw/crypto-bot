"""Network-free tests for quant.research.candles."""

from __future__ import annotations

import math

import pytest

from quant.research.candles import (
    Candle,
    FixtureCandleSource,
    PublicCcxtCandleSource,
    candles_from_ccxt_rows,
    closes,
    log_returns,
)


def test_candles_from_ccxt_rows():
    rows = [
        [1_700_000_000_000, 100.0, 110.0, 90.0, 105.0, 12.5],
        [1_700_003_600_000, 105.0, 120.0, 100.0, 115.0, 20.0],
    ]
    bars = candles_from_ccxt_rows(rows)
    assert len(bars) == 2
    assert bars[0] == Candle(
        timestamp_ms=1_700_000_000_000,
        open=100.0,
        high=110.0,
        low=90.0,
        close=105.0,
        volume=12.5,
    )
    assert bars[1].close == 115.0


def test_closes_and_log_returns():
    bars = candles_from_ccxt_rows(
        [
            [0, 1, 1, 1, 100.0, 1],
            [1, 1, 1, 1, 110.0, 1],
            [2, 1, 1, 1, 121.0, 1],
        ]
    )
    c = closes(bars)
    assert c == [100.0, 110.0, 121.0]
    lr = log_returns(c)
    assert len(lr) == 2
    assert lr[0] == pytest.approx(math.log(1.1))
    assert lr[1] == pytest.approx(math.log(1.1))


def test_log_returns_rejects_non_positive():
    with pytest.raises(ValueError, match="positive"):
        log_returns([100.0, 0.0])


def test_public_source_validates_symbol_and_timeframe():
    src = PublicCcxtCandleSource(exchange=object())
    with pytest.raises(ValueError, match="BASE/QUOTE"):
        src.fetch_ohlcv("BTCUSDT")
    with pytest.raises(ValueError, match="timeframe"):
        src.fetch_ohlcv("BTC/USDT", timeframe="")


def test_public_source_reuses_exchange_and_maps_rows():
    class FakeExchange:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_ohlcv(self, symbol, timeframe="1h", since=None, limit=100):
            self.calls += 1
            assert symbol == "BTC/USDT"
            assert timeframe == "1h"
            assert limit == 2
            return [
                [1000, 1, 2, 0.5, 1.5, 10],
                [2000, 1.5, 3, 1, 2.5, 11],
            ]

    fake = FakeExchange()
    src = PublicCcxtCandleSource(exchange=fake)
    bars1 = src.fetch_ohlcv("BTC/USDT", limit=2)
    bars2 = src.fetch_ohlcv("BTC/USDT", limit=2)
    assert fake.calls == 2
    assert src._exchange is fake  # reused instance
    assert len(bars1) == 2
    assert bars2[1].close == 2.5


def test_public_source_empty_rows_error():
    class EmptyExchange:
        def fetch_ohlcv(self, *args, **kwargs):
            return []

    src = PublicCcxtCandleSource(exchange=EmptyExchange())
    with pytest.raises(ValueError, match="empty OHLCV"):
        src.fetch_ohlcv("ETH/USDT")


def test_fixture_candle_source():
    bars = candles_from_ccxt_rows(
        [[i * 1000, 1, 1, 1, float(i + 1), 1] for i in range(5)]
    )
    src = FixtureCandleSource({"SOL/USDT": bars})
    got = src.fetch_ohlcv("SOL/USDT", limit=3)
    assert len(got) == 3
    assert got[-1].close == 5.0
    assert src.fetch_ohlcv("BTC/USDT") == []
