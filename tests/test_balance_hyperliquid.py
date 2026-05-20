"""Hyperliquid balance fetching in data.balance.Balance."""
from unittest.mock import MagicMock, patch

import data.balance as balance_mod
from data.balance import Balance


def _balance() -> Balance:
    b = Balance.__new__(Balance)
    b.binance_client = None
    b.kraken_client = None
    b._binance_balances = None
    b._w3 = None
    b._contracts = {}
    return b


def test_fetch_hyperliquid_returns_empty_when_meta_mask_missing(monkeypatch):
    monkeypatch.setattr(balance_mod, "META_MASK", "")
    assert _balance()._fetch_hyperliquid_spot_balances() == []


def test_get_hyperliquid_balances_returns_total_per_coin(monkeypatch):
    monkeypatch.setattr(balance_mod, "META_MASK", "0xmaster")
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "balances": [
            {"coin": "HYPE", "total": "48.37", "hold": "0.0"},
            {"coin": "USDC", "total": "12.5", "hold": "5.0"},
        ]
    }
    fake_response.raise_for_status = MagicMock()
    with patch.object(balance_mod.requests, "post", return_value=fake_response):
        result = _balance().get_hyperliquid_balances()

    assert result == {"HYPE": 48.37, "USDC": 12.5}


def test_get_hyperliquid_free_balance_subtracts_hold(monkeypatch):
    monkeypatch.setattr(balance_mod, "META_MASK", "0xmaster")
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "balances": [
            {"coin": "HYPE", "total": "48.37", "hold": "10.0"},
        ]
    }
    fake_response.raise_for_status = MagicMock()
    with patch.object(balance_mod.requests, "post", return_value=fake_response):
        free = _balance().get_hyperliquid_free_balance("HYPE")

    assert free == 38.37


def test_get_hyperliquid_free_balance_returns_zero_for_missing_coin(monkeypatch):
    monkeypatch.setattr(balance_mod, "META_MASK", "0xmaster")
    fake_response = MagicMock()
    fake_response.json.return_value = {"balances": []}
    fake_response.raise_for_status = MagicMock()
    with patch.object(balance_mod.requests, "post", return_value=fake_response):
        assert _balance().get_hyperliquid_free_balance("HYPE") == 0.0


def test_fetch_hyperliquid_swallows_request_errors(monkeypatch):
    monkeypatch.setattr(balance_mod, "META_MASK", "0xmaster")
    with patch.object(balance_mod.requests, "post", side_effect=RuntimeError("boom")):
        assert _balance()._fetch_hyperliquid_spot_balances() == []
