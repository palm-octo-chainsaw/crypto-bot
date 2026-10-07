"""Tests for data.prices error semantics."""
import pytest
import requests

from data.prices import binance_lists_spot_pair, fetch_prices, PriceFetchError, PriceRateLimitError


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
    payload = {"bitcoin": {"usd": 100.0}, "ethereum": {"usd": 50.0}}
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


def test_fetch_prices_reads_unmapped_symbols_off_binance_usdc_pairs(monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(url)
        if "coingecko" in url:
            return FakeResponse({"bitcoin": {"usd": 100.0}})
        assert params == {"symbols": '["TAOUSDC"]'}
        return FakeResponse([{"symbol": "TAOUSDC", "price": "289.6"}])
    monkeypatch.setattr("data.prices.requests.get", fake_get)

    assert fetch_prices(["BTC", "tao"]) == {"BTC": 100.0, "TAO": 289.6}
    assert len(calls) == 2


def test_fetch_prices_skips_coingecko_when_every_symbol_is_unmapped(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        assert "coingecko" not in url
        return FakeResponse([{"symbol": "TAOUSDC", "price": "289.6"}])
    monkeypatch.setattr("data.prices.requests.get", fake_get)

    assert fetch_prices(["TAO"]) == {"TAO": 289.6}


def test_fetch_prices_raises_on_unmapped_symbol_binance_cannot_price(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        if "coingecko" in url:
            return FakeResponse({"bitcoin": {"usd": 100.0}})
        return FakeResponse({"code": -1121, "msg": "Invalid symbol."}, status_code=400)
    monkeypatch.setattr("data.prices.requests.get", fake_get)

    with pytest.raises(PriceFetchError, match="NOPE"):
        fetch_prices(["BTC", "NOPE"])


@pytest.mark.parametrize("response, expected", [
    (FakeResponse({"symbols": [{"status": "TRADING", "isSpotTradingAllowed": True}]}), True),
    (FakeResponse({"symbols": [{"status": "BREAK", "isSpotTradingAllowed": True}]}), False),
    (FakeResponse({"code": -1121, "msg": "Invalid symbol."}, status_code=400), False),
    (FakeResponse({"code": -1003, "msg": "Too many requests."}, status_code=429), None),
])
def test_binance_lists_spot_pair(monkeypatch, response, expected):
    monkeypatch.setattr("data.prices.requests.get", lambda *a, **k: response)

    assert binance_lists_spot_pair("TAO") is expected


def test_binance_lists_spot_pair_is_unknown_when_binance_is_unreachable(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("dns failure")
    monkeypatch.setattr("data.prices.requests.get", boom)

    assert binance_lists_spot_pair("TAO") is None


def test_fetch_prices_raises_when_symbol_missing(monkeypatch):
    payload = {"bitcoin": {"usd": 100.0}}  # ETH missing
    monkeypatch.setattr("data.prices.requests.get", lambda *a, **k: FakeResponse(payload))

    with pytest.raises(PriceFetchError, match="ETH"):
        fetch_prices(["BTC", "ETH"])


def test_fetch_prices_raises_when_a_coin_comes_back_without_a_usd_quote(monkeypatch):
    """CoinGecko can answer with the id but no `usd` key — that is a missing price, not zero."""
    payload = {"bitcoin": {"usd": 100.0}, "ethereum": {}}
    monkeypatch.setattr("data.prices.requests.get", lambda *a, **k: FakeResponse(payload))

    with pytest.raises(PriceFetchError, match="missing prices for: ETH"):
        fetch_prices(["BTC", "ETH"])


@pytest.mark.parametrize("tickers", [
    [{"symbol": "TAOUSDC"}],
    [{"symbol": "TAOUSDC", "price": "n/a"}],
    {"code": -1100, "msg": "Illegal characters found in parameter 'symbols'."},
])
def test_fetch_prices_raises_price_error_on_a_malformed_binance_ticker(monkeypatch, tickers):
    """Callers handle PriceFetchError; a KeyError or ValueError would escape as a crash."""
    monkeypatch.setattr("data.prices.requests.get", lambda *a, **k: FakeResponse(tickers))

    with pytest.raises(PriceFetchError, match="malformed Binance ticker"):
        fetch_prices(["TAO"])
