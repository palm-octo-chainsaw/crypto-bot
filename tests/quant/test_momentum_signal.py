"""Network-free tests for MomentumVolSignal."""

from __future__ import annotations


from quant.research.candles import Candle
from quant.signals.momentum import MOMENTUM_SCALE, MomentumVolSignal


def _bars_from_closes(close_prices):
    """Build synthetic candles from a close series (OHLC flat at close)."""
    out = []
    for i, c in enumerate(close_prices):
        out.append(
            Candle(
                timestamp_ms=i * 3_600_000,
                open=c,
                high=c,
                low=c,
                close=c,
                volume=1.0,
            )
        )
    return out


def _geometric_path(start: float, n: int, ret_per_bar: float):
    """n closes starting at ``start``, each step multiplying by (1+r)."""
    prices = [start]
    for _ in range(n - 1):
        prices.append(prices[-1] * (1.0 + ret_per_bar))
    return prices


def test_insufficient_data_neutral():
    sig = MomentumVolSignal(lookback=24, vol_lookback=24)
    candles = _bars_from_closes([100.0] * 10)
    out = sig.score("BTC/USDT", candles)
    assert out.score == 0.0
    assert out.confidence == 0.0
    assert out.meta["reason"] == "insufficient_data"
    assert out.meta["n_candles"] == 10


def test_positive_momentum_positive_score():
    # Mild per-bar drift so realized vol stays inside a wide band.
    sig = MomentumVolSignal(
        lookback=24,
        vol_lookback=24,
        min_vol=0.0,
        max_vol=1.0,
        momentum_scale=MOMENTUM_SCALE,
    )
    closes = _geometric_path(100.0, 50, 0.01)  # +1%/bar -> strong positive momentum
    out = sig.score("ETH/USDT", _bars_from_closes(closes))
    assert out.score > 0.0
    assert out.meta["vol_filter_pass"] is True
    assert out.meta["momentum_return"] > 0
    assert "realized_vol" in out.meta
    assert out.meta["lookback"] == 24
    assert out.meta["vol_lookback"] == 24


def test_high_vol_filtered_to_zero():
    # Alternate big up/down moves -> high realized vol, low net lookback return.
    sig = MomentumVolSignal(
        lookback=10,
        vol_lookback=10,
        min_vol=0.001,
        max_vol=0.01,  # tight band; alternating +/-5% will blow through
    )
    prices = [100.0]
    for i in range(30):
        prices.append(prices[-1] * (1.05 if i % 2 == 0 else 1 / 1.05))
    out = sig.score("SOL/USDT", _bars_from_closes(prices))
    assert out.score == 0.0
    assert out.confidence == 0.0
    assert out.meta["reason"] == "vol_filter"
    assert out.meta["vol_filter_pass"] is False
    assert out.meta["realized_vol"] > sig.max_vol


def test_low_vol_filtered_to_zero():
    # Nearly flat path -> realized vol below min_vol.
    sig = MomentumVolSignal(
        lookback=10,
        vol_lookback=10,
        min_vol=0.01,
        max_vol=0.5,
    )
    prices = [100.0 + i * 1e-9 for i in range(40)]
    out = sig.score("BTC/USDT", _bars_from_closes(prices))
    assert out.score == 0.0
    assert out.confidence == 0.0
    assert out.meta["reason"] == "vol_filter"
    assert out.meta["realized_vol"] < sig.min_vol


def test_score_clipped_to_unit_interval():
    sig = MomentumVolSignal(
        lookback=5,
        vol_lookback=5,
        min_vol=0.0,
        max_vol=10.0,
        momentum_scale=0.01,  # tiny scale -> raw ratio >> 1 -> clip
    )
    closes = _geometric_path(100.0, 20, 0.05)
    out = sig.score("BTC/USDT", _bars_from_closes(closes))
    assert -1.0 <= out.score <= 1.0
    assert out.score == 1.0  # strongly positive, clipped


def test_negative_momentum_negative_score():
    sig = MomentumVolSignal(
        lookback=24,
        vol_lookback=24,
        min_vol=0.0,
        max_vol=1.0,
    )
    closes = _geometric_path(100.0, 50, -0.01)
    out = sig.score("BTC/USDT", _bars_from_closes(closes))
    assert out.score < 0.0
    assert out.meta["momentum_return"] < 0


def test_meta_fields_present_on_ok():
    sig = MomentumVolSignal(lookback=8, vol_lookback=8, min_vol=0.0, max_vol=1.0)
    closes = _geometric_path(50.0, 30, 0.002)
    out = sig.score("ETH/USDT", _bars_from_closes(closes))
    for key in (
        "momentum_return",
        "realized_vol",
        "lookback",
        "vol_lookback",
        "vol_filter_pass",
        "reason",
        "n_candles",
        "min_candles",
    ):
        assert key in out.meta
    assert out.meta["reason"] == "ok"
    assert 0.0 <= out.confidence <= 1.0


def test_momentum_return_matches_formula():
    sig = MomentumVolSignal(lookback=4, vol_lookback=4, min_vol=0.0, max_vol=1.0)
    closes = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 20.0]
    out = sig.score("BTC/USDT", _bars_from_closes(closes))
    expected = closes[-1] / closes[-4] - 1.0
    assert out.meta["momentum_return"] == expected
    assert out.score == max(-1.0, min(1.0, expected / sig.momentum_scale))
