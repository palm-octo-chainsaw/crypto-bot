"""Snapshots and trades taken while a venue was unreadable.

Exchange fetchers fall back to 0.0 so one dead venue can't stop the rest of the
portfolio from being priced. That fallback is indistinguishable from an empty
wallet, so a snapshot taken during an outage is marked partial and a rebalance is
refused outright rather than sizing trades against holdings that look sold.
"""
from unittest.mock import MagicMock, patch

import pytest

import portfolio as pf
from data.balance import Balance


def _portfolio(degraded: set[str]) -> pf.Portfolio:
    p = pf.Portfolio.__new__(pf.Portfolio)
    p.summary = MagicMock()
    p.balance = MagicMock()
    p.balance.degraded = degraded
    p.portfolio = {"BTC": 0.1}
    p.venues = {Balance.BINANCE: {"BTC": 0.1}}
    p.update_portfolio = lambda: None
    p.targets = {"BTC": 100.0}
    return p


def _offline_venues():
    """Patch out every venue except the one under test."""
    return (
        patch.object(Balance, "_arbitrum_usdc", return_value=0.0),
        patch.object(Balance, "_arbitrum_eth", return_value=0.0),
        patch.object(Balance, "get_hyperliquid_balances", return_value={}),
    )


# --- snapshot recording --------------------------------------------------------

@patch("portfolio.record_snapshot")
@patch("portfolio.get_latest_signal_id", return_value=7)
@patch("portfolio.fetch_prices", return_value={"BTC": 60000.0})
def test_listener_marks_snapshot_partial_when_venue_degraded(_prices, _sid, record):
    p = _portfolio({"binance", "kraken"})
    p.listener()

    assert record.call_args.kwargs["partial"] is True
    warnings = [c.args[0] for c in p.summary.add_summary.call_args_list]
    assert any("Partial snapshot" in w and "binance, kraken" in w for w in warnings)


@patch("portfolio.record_snapshot")
@patch("portfolio.get_latest_signal_id", return_value=7)
@patch("portfolio.fetch_prices", return_value={"BTC": 60000.0})
def test_listener_records_clean_snapshot_when_all_venues_healthy(_prices, _sid, record):
    p = _portfolio(set())
    p.listener()

    assert record.call_args.kwargs["partial"] is False
    assert record.call_args.kwargs["total_value_usd"] == pytest.approx(6000.0)
    warnings = [c.args[0] for c in p.summary.add_summary.call_args_list]
    assert not any("Partial snapshot" in w for w in warnings)


# --- trading -------------------------------------------------------------------

def test_rebalance_refuses_to_trade_on_degraded_balances(monkeypatch):
    """A venue returning 0.0 looks like its holdings were already sold."""
    monkeypatch.setattr(pf, "BINANCE_API_KEY", "k")
    monkeypatch.setattr(pf, "BINANCE_API_SECRET", "s")

    p = _portfolio({"kraken"})
    p.update_portfolio = lambda: None
    p.fetch_live_data = lambda: pytest.fail("must not price a portfolio it cannot read")

    message = p.execute_rebalance(dry_run=False)
    assert "refusing to trade" in message and "kraken" in message


def test_rebalance_proceeds_when_venues_healthy(monkeypatch):
    """The guard must not block the normal path."""
    monkeypatch.setattr(pf, "BINANCE_API_KEY", "k")
    monkeypatch.setattr(pf, "BINANCE_API_SECRET", "s")

    p = _portfolio(set())
    p.portfolio = {"BTC": 0.1, "USDC": 0.0}
    p.targets = {"BTC": 100.0, "USDC": 0.0}
    p.update_portfolio = lambda: None
    p.fetch_live_data = lambda: (
        {"BTC": 60000.0, "USDC": 1.0}, {"BTC": 6000.0, "USDC": 0.0}, 6000.0,
    )

    assert "refusing to trade" not in p.execute_rebalance(dry_run=True)


# --- degraded-flag lifecycle ---------------------------------------------------

def test_binance_failure_survives_the_spot_balance_reset(bare_balance):
    """update_portfolio() refreshes Binance first, then reads spot balances.

    The spot-balance read must not clear a Binance failure raised moments earlier,
    or the caller sees healthy venues and treats the zeroed total as real.
    """
    bare_balance.binance_client = MagicMock()
    bare_balance.binance_client.get_account.side_effect = RuntimeError("read timed out")
    bare_balance.kraken_client = MagicMock()
    bare_balance.kraken_client.query_private.return_value = {"result": {}}

    bare_balance.refresh_binance_balances()
    assert bare_balance.degraded == {"binance"}

    usdc, eth, hl = _offline_venues()
    with usdc, eth, hl:
        bare_balance.get_spot_balance()

    assert bare_balance.degraded == {"binance"}


def test_degraded_clears_once_every_venue_answers_again(bare_balance):
    """A stuck flag would mark every later snapshot partial forever."""
    bare_balance.binance_client = MagicMock()
    bare_balance.binance_client.get_account.side_effect = RuntimeError("boom")
    bare_balance.kraken_client = MagicMock()
    bare_balance.kraken_client.query_private.return_value = {"result": {}}
    bare_balance.refresh_binance_balances()

    usdc, eth, hl = _offline_venues()
    with usdc, eth, hl:
        bare_balance.get_spot_balance()
        assert bare_balance.degraded == {"binance"}

        bare_balance.binance_client.get_account.side_effect = None
        bare_balance.binance_client.get_account.return_value = {
            "balances": [{"asset": "BTC", "free": "1.0", "locked": "0.0"}]
        }
        bare_balance.refresh_binance_balances()
        bare_balance.get_spot_balance()

    assert bare_balance.degraded == set()


# --- failures that used to raise no flag at all --------------------------------

def test_absent_binance_credentials_count_as_degraded(bare_balance):
    """The bot holds assets on every venue, so no client means an incomplete read."""
    bare_balance.kraken_client = MagicMock()
    bare_balance.kraken_client.query_private.return_value = {"result": {}}

    usdc, eth, hl = _offline_venues()
    with usdc, eth, hl:
        bare_balance.get_spot_balance()

    assert "binance" in bare_balance.degraded


def test_absent_kraken_credentials_count_as_degraded(bare_balance):
    bare_balance.binance_client = MagicMock()
    bare_balance.binance_client.get_account.return_value = {"balances": []}

    usdc, eth, hl = _offline_venues()
    with usdc, eth, hl:
        bare_balance.get_spot_balance()

    assert "kraken" in bare_balance.degraded


def test_malformed_binance_response_is_a_failure_not_an_empty_account(bare_balance):
    """A 200 without the balances key silently zeroed the whole portfolio."""
    bare_balance.binance_client = MagicMock()
    bare_balance.binance_client.get_account.return_value = {"accountType": "SPOT"}

    bare_balance.refresh_binance_balances()

    assert bare_balance.degraded == {"binance"}
    assert bare_balance.get_binance_balance("BTC") == 0.0


def test_missing_meta_mask_counts_as_degraded(bare_balance, monkeypatch):
    import data.balance as balance_mod

    monkeypatch.setattr(balance_mod, "META_MASK", "")
    assert bare_balance._fetch_hyperliquid_spot_balances() == []
    assert bare_balance.degraded == {"hyperliquid"}
