"""A snapshot must never be recorded from balances a venue failed to return.

Exchange fetchers fall back to 0.0 so one dead venue can't stop the rest of the
portfolio from being priced. Persisting that fallback wrote near-$0 totals into
the history, and /performance later divided by one of them.
"""
from unittest.mock import MagicMock, patch

import pytest


def _portfolio(degraded: set[str]):
    from portfolio import Portfolio

    p = Portfolio()
    p.summary = MagicMock()
    p.balance = MagicMock()
    p.balance.degraded = degraded
    p.balance.get_spot_balance.return_value = {"BTC": 0.1}
    p.targets = {"BTC": 100.0}
    p.portfolio = {"BTC": 0.1}
    return p


def test_binance_failure_survives_the_spot_balance_reset():
    """update_portfolio() refreshes Binance first, then reads spot balances.

    The spot-balance read must not clear a Binance failure raised moments earlier,
    or the caller sees healthy venues and records the zeroed total anyway.
    """
    from data.balance import Balance

    b = Balance.__new__(Balance)
    b.binance_client = MagicMock()
    b.binance_client.get_account.side_effect = RuntimeError("read timed out")
    b.kraken_client = None
    b._binance_balances = None
    b._w3 = None
    b._contracts = {}
    b._degraded = set()

    b.refresh_binance_balances()
    assert b.degraded == {"binance"}

    with patch.object(Balance, "get_usdc_balance", return_value=0.0), \
         patch.object(Balance, "get_eth_balance", return_value=0.0), \
         patch.object(Balance, "get_hyperliquid_balances", return_value={}):
        b.get_spot_balance()

    assert b.degraded == {"binance"}


@patch("portfolio.Balance")
@patch("portfolio.record_snapshot")
@patch("portfolio.fetch_prices", return_value={"BTC": 60000.0})
def test_listener_skips_snapshot_when_venue_degraded(_prices, record, _balance_cls):
    p = _portfolio({"binance", "kraken"})
    p.listener()

    record.assert_not_called()
    warnings = [c.args[0] for c in p.summary.add_summary.call_args_list]
    assert any("Snapshot skipped" in w and "binance, kraken" in w for w in warnings)


@patch("portfolio.Balance")
@patch("portfolio.record_snapshot")
@patch("portfolio.get_latest_signal_id", return_value=7)
@patch("portfolio.fetch_prices", return_value={"BTC": 60000.0})
def test_listener_records_snapshot_when_all_venues_healthy(_prices, _sid, record, _balance_cls):
    p = _portfolio(set())
    p.listener()

    record.assert_called_once()
    assert record.call_args.kwargs["total_value_usd"] == pytest.approx(6000.0)
