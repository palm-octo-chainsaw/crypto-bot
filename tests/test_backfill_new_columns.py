"""The one-time backfill in scripts/backfill_new_columns.py, without a database."""
from datetime import date, datetime, timezone

import pytest

import scripts.backfill_new_columns as bf


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _trade(symbol, side, direction=None, perp=None, order_id="1", opened=None, closed=None):
    return {"id": 1, "symbol": symbol, "side": side, "order_id": order_id, "direction": direction,
            "perp": perp, "opened": opened, "closed": closed}


def test_plan_trade_fills_a_spot_trade_from_its_symbol():
    assert bf.plan_trade(_trade("ETH/BTC", "buy"), {}) == {
        "perp": False, "direction": "long", "opened": "ETH", "closed": "BTC"}


def test_plan_trade_reads_a_perp_direction_from_its_fill():
    plan = bf.plan_trade(_trade("SOL/USDC:USDC", "sell", order_id=42), {"42": "short"})
    assert plan == {"perp": True, "direction": "short", "opened": "SOL", "closed": None}


def test_plan_trade_leaves_a_perp_without_a_fill():
    assert bf.plan_trade(_trade("SOL/USDC:USDC", "sell"), {}) == {"perp": True}


def test_plan_trade_keeps_what_is_already_recorded():
    trade = _trade("SOL/USDC:USDC", "buy", direction="short", perp=True, closed="SOL")
    assert bf.plan_trade(trade, {}) == {}


def test_rate_on_uses_the_last_working_day():
    rates = [(date(2026, 10, 30), 0.9), (date(2026, 11, 2), 0.8)]
    assert bf.rate_on(rates, date(2026, 11, 1)) == 0.9  # a Sunday
    assert bf.rate_on(rates, date(2026, 11, 2)) == 0.8
    assert bf.rate_on(rates, date(2026, 10, 1)) == 0.9  # before the first: the earliest


def test_daily_usd_rates_asks_from_a_week_before(monkeypatch):
    asked = []

    def fake_get(url, params, timeout):
        asked.append(url)
        return FakeResponse({"rates": {"2026-11-02": {"EUR": 0.8}, "2026-10-30": {"EUR": 0.9}}})

    monkeypatch.setattr(bf.requests, "get", fake_get)
    rates = bf.daily_usd_rates("EUR", date(2026, 11, 1), date(2026, 11, 2))
    assert asked == ["https://api.frankfurter.dev/v1/2026-10-25..2026-11-02"]
    assert rates == [(date(2026, 10, 30), 0.9), (date(2026, 11, 2), 0.8)]


def test_daily_usd_rates_in_usd_needs_no_request(monkeypatch):
    monkeypatch.setattr(bf.requests, "get", None)
    assert bf.daily_usd_rates("USD", date(2026, 11, 1), date(2026, 11, 2)) == [(date(2026, 11, 1), 1.0)]


def test_hyperliquid_directions_pages_through_the_fills(monkeypatch):
    monkeypatch.setattr(bf, "FILLS_PAGE", 2)
    pages = [
        [{"oid": 1, "dir": "Open Short", "time": 10}, {"oid": 2, "dir": "Close Long", "time": 20}],
        [{"oid": 2, "dir": "Close Long", "time": 20}, {"oid": 3, "dir": "Long > Short", "time": 30}],
        [],
    ]
    starts = []

    def fake_post(url, json, timeout):
        starts.append(json["startTime"])
        return FakeResponse(pages[len(starts) - 1])

    monkeypatch.setattr(bf.requests, "post", fake_post)
    since = datetime.fromtimestamp(0.005, tz=timezone.utc)

    assert bf.hyperliquid_directions("0xabc", since) == {"1": "short", "2": "long"}
    assert starts == [5, 20, 30]
