"""Tests for data.prices error semantics."""
import pytest
import requests

from data.prices import fetch_prices, PriceFetchError, PriceRateLimitError


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload or {}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code} error")
            err.response = self
            raise err

    def json(self):
        return self._payload


def test_fetch_prices_returns_dict_on_success(monkeypatch):
    payload = {"btc": {"usd": 100.0}, "eth": {"usd": 50.0}}
    monkeypatch.setattr("data.prices.requests.get", lambda *a, **k: FakeResponse(payload))

    prices = fetch_prices(["BTC", "ETH"])

    assert prices == {"BTC": 100.0, "ETH": 50.0}


def test_fetch_prices_raises_rate_limit_on_429(monkeypatch):
    monkeypatch.setattr("data.prices.requests.get", lambda *a, **k: FakeResponse(status_code=429))

    with pytest.raises(PriceRateLimitError):
        fetch_prices(["BTC"])


def test_fetch_prices_raises_fetch_error_on_other_http_error(monkeypatch):
    monkeypatch.setattr("data.prices.requests.get", lambda *a, **k: FakeResponse(status_code=500))

    with pytest.raises(PriceFetchError) as exc_info:
        fetch_prices(["BTC"])
    assert not isinstance(exc_info.value, PriceRateLimitError)


def test_fetch_prices_raises_on_network_error(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("network down")
    monkeypatch.setattr("data.prices.requests.get", boom)

    with pytest.raises(PriceFetchError):
        fetch_prices(["BTC"])


def test_fetch_prices_raises_when_symbol_missing(monkeypatch):
    payload = {"btc": {"usd": 100.0}}  # ETH missing
    monkeypatch.setattr("data.prices.requests.get", lambda *a, **k: FakeResponse(payload))

    with pytest.raises(PriceFetchError, match="ETH"):
        fetch_prices(["BTC", "ETH"])
