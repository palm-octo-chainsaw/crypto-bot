"""Hyperliquid balance fetching in data.balance.Balance."""
from unittest.mock import MagicMock, patch

import data.balance as balance_mod


def _response(balances: list[dict]) -> MagicMock:
    fake = MagicMock()
    fake.json.return_value = {"balances": balances}
    fake.raise_for_status = MagicMock()
    return fake


def test_fetch_hyperliquid_returns_empty_when_meta_mask_missing(bare_balance, monkeypatch):
    monkeypatch.setattr(balance_mod, "META_MASK", "")
    assert bare_balance._fetch_hyperliquid_spot_balances() == []


def test_get_hyperliquid_balances_returns_total_per_coin(bare_balance, monkeypatch):
    monkeypatch.setattr(balance_mod, "META_MASK", "0xmaster")
    fake_response = _response([
        {"coin": "HYPE", "total": "48.37", "hold": "0.0"},
        {"coin": "USDC", "total": "12.5", "hold": "5.0"},
    ])
    with patch.object(balance_mod.requests, "post", return_value=fake_response):
        result = bare_balance.get_hyperliquid_balances()

    assert result == {"HYPE": 48.37, "USDC": 12.5}


def test_get_hyperliquid_free_balance_subtracts_hold(bare_balance, monkeypatch):
    monkeypatch.setattr(balance_mod, "META_MASK", "0xmaster")
    fake_response = _response([{"coin": "HYPE", "total": "48.37", "hold": "10.0"}])
    with patch.object(balance_mod.requests, "post", return_value=fake_response):
        free = bare_balance.get_hyperliquid_free_balance("HYPE")

    assert free == 38.37


def test_get_hyperliquid_free_balance_returns_zero_for_missing_coin(bare_balance, monkeypatch):
    monkeypatch.setattr(balance_mod, "META_MASK", "0xmaster")
    with patch.object(balance_mod.requests, "post", return_value=_response([])):
        assert bare_balance.get_hyperliquid_free_balance("HYPE") == 0.0


def test_fetch_hyperliquid_swallows_request_errors(bare_balance, monkeypatch):
    monkeypatch.setattr(balance_mod, "META_MASK", "0xmaster")
    with patch.object(balance_mod.requests, "post", side_effect=RuntimeError("boom")):
        assert bare_balance._fetch_hyperliquid_spot_balances() == []
        assert bare_balance.degraded == {"hyperliquid"}
