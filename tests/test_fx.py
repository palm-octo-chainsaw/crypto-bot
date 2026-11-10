"""Tests for the display currency (data/fx.py) and the /currency command."""
from unittest.mock import AsyncMock, MagicMock

import pytest
import requests

from data import fx
from utils import command_handlers as ch


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


@pytest.fixture
def rates(monkeypatch):
    """Answer rate requests from a table, counting each one."""
    table = {"EUR": 0.9, "GBP": 0.8, "CHF": 0.88}
    calls = []

    def fake_get(url, params, timeout):
        calls.append(params["symbols"])
        code = params["symbols"]
        if code not in table:
            return FakeResponse(404, {"message": "not found"})
        return FakeResponse(200, {"base": "USD", "rates": {code: table[code]}})

    monkeypatch.setattr(fx.requests, "get", fake_get)
    return calls


@pytest.fixture
def stored(monkeypatch):
    settings = {}
    monkeypatch.setattr(fx, "get_setting", settings.get)
    monkeypatch.setattr(fx, "set_setting", settings.__setitem__)
    return settings


def test_money_converts_usd_to_the_display_currency(rates, monkeypatch):
    monkeypatch.setattr(fx, "_selected", "EUR")
    assert fx.money(1234.5) == "€1,111.05"
    assert fx.money(-10) == "-€9.00"


def test_money_names_a_currency_without_a_symbol(rates, monkeypatch):
    monkeypatch.setattr(fx, "_selected", "CHF")
    assert fx.money(100) == "88.00 CHF"


def test_money_in_usd_needs_no_rate(rates):
    assert fx.money(1234.5) == "$1,234.50"
    assert rates == []


def test_rate_is_fetched_once_per_hour(rates, monkeypatch):
    monkeypatch.setattr(fx, "_selected", "EUR")
    fx.money(1)
    fx.money(2)
    assert rates == ["EUR"]


def test_money_falls_back_to_usd_when_no_rate_can_be_had(monkeypatch):
    def down(*args, **kwargs):
        raise requests.ConnectionError("down")

    calls = []
    monkeypatch.setattr(fx.requests, "get", lambda *a, **k: calls.append(1) or down())
    monkeypatch.setattr(fx, "_selected", "EUR")

    assert fx.money(5) == "$5.00"
    assert fx.money(6) == "$6.00"
    assert len(calls) == 1  # the failure is remembered, not retried per line


def test_money_shows_a_past_amount_at_its_own_rate(rates, monkeypatch):
    monkeypatch.setattr(fx, "_selected", "EUR")
    assert fx.money(100, fx.DisplayRate("EUR", 0.95)) == "€95.00"
    assert rates == []


def test_money_converts_a_past_amount_recorded_in_another_currency_at_today_rate(
        rates, monkeypatch):
    monkeypatch.setattr(fx, "_selected", "EUR")
    assert fx.money(100, fx.DisplayRate("GBP", 0.8)) == "€90.00"
    assert fx.money(100, fx.DisplayRate()) == "€90.00"


def test_rate_now_is_the_display_currency_and_its_rate(rates, monkeypatch):
    monkeypatch.setattr(fx, "_selected", "EUR")
    assert fx.rate_now() == fx.DisplayRate("EUR", 0.9)


def test_rate_now_records_no_rate_when_none_can_be_had(monkeypatch):
    monkeypatch.setattr(fx.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down")))
    monkeypatch.setattr(fx, "_selected", "EUR")
    assert fx.rate_now() == fx.DisplayRate()


def test_a_stale_rate_outlives_a_failed_refresh(rates, monkeypatch):
    monkeypatch.setattr(fx, "_selected", "EUR")
    monkeypatch.setattr(fx, "_rates", {"EUR": (0.5, fx.time.monotonic() - fx.RATE_TTL_SECONDS - 1)})
    monkeypatch.setattr(fx.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(requests.Timeout("slow")))
    assert fx.money(10) == "€5.00"


def test_current_defaults_to_display_currency(stored, monkeypatch):
    monkeypatch.setattr(fx, "_selected", None)
    monkeypatch.setattr(fx.constants, "DISPLAY_CURRENCY", "EUR")
    assert fx.current() == "EUR"


def test_current_prefers_the_stored_choice(stored, monkeypatch):
    monkeypatch.setattr(fx, "_selected", None)
    stored[fx.SETTING_KEY] = "GBP"
    assert fx.current() == "GBP"


def test_current_survives_an_unreadable_database(monkeypatch):
    monkeypatch.setattr(fx, "_selected", None)
    monkeypatch.setattr(fx.constants, "DISPLAY_CURRENCY", "EUR")
    monkeypatch.setattr(fx, "get_setting", MagicMock(side_effect=RuntimeError("db down")))
    assert fx.current() == "EUR"
    assert fx._selected is None  # read again once the database answers


def test_select_stores_a_known_currency(rates, stored):
    assert fx.select(" gbp ") == 0.8
    assert stored == {fx.SETTING_KEY: "GBP"}
    assert fx.current() == "GBP"


def test_select_refuses_an_unknown_currency(rates, stored):
    with pytest.raises(fx.UnknownCurrency):
        fx.select("XYZ")
    assert stored == {}
    assert fx.current() == "USD"


def _update():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def _reply(update):
    return update.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_currency_command_shows_the_current_one(fake_context):
    update = _update()
    await ch.currency(update, fake_context)
    assert "shown in USD" in _reply(update)


@pytest.mark.asyncio
async def test_currency_command_switches(fake_context, rates, stored):
    fake_context.args = ["eur"]
    update = _update()
    await ch.currency(update, fake_context)
    assert _reply(update) == "✅ Values now shown in EUR (1 USD = 0.9000 EUR)."
    assert fx.current() == "EUR"


@pytest.mark.asyncio
async def test_currency_command_rejects_an_unknown_code(fake_context, rates, stored):
    fake_context.args = ["XYZ"]
    update = _update()
    await ch.currency(update, fake_context)
    assert "Unknown currency XYZ" in _reply(update)
    assert fx.current() == "USD"


@pytest.mark.asyncio
async def test_currency_command_keeps_the_old_one_when_rates_are_down(fake_context, stored, monkeypatch):
    monkeypatch.setattr(fx.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down")))
    fake_context.args = ["EUR"]
    update = _update()
    await ch.currency(update, fake_context)
    assert "Could not fetch the EUR rate — still showing USD" in _reply(update)
    assert stored == {}
