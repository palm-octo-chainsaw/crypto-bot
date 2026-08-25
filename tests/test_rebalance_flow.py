"""Rebalance orchestration: trade classification, reporting lines and failure paths."""
from unittest.mock import MagicMock

import pytest

import portfolio as pf


class FakeExchange:
    def __init__(self, free: dict, markets: dict):
        self._free = free
        self.markets = markets
        self.orders = []

    def fetch_balance(self):
        return {"free": self._free}

    def amount_to_precision(self, symbol, amount):
        return f"{float(amount):.6f}"

    def cost_to_precision(self, symbol, cost):
        return f"{float(cost):.2f}"

    def create_market_order(self, symbol, side, amount, price=None):
        self.orders.append((symbol, side, amount))
        return {"id": "ord1", "status": "closed", "fee": {}}

    def create_market_buy_order_with_cost(self, symbol, cost):
        self.orders.append((symbol, "buy", cost))
        return {"id": "ord2", "status": "closed", "filled": cost / 50_000, "cost": cost, "fee": {}}


def _portfolio(holdings: dict) -> pf.Portfolio:
    p = pf.Portfolio.__new__(pf.Portfolio)
    p.portfolio = holdings
    return p


@pytest.fixture
def live_portfolio(monkeypatch):
    """A Portfolio wired for execute_rebalance without touching a venue or the database."""
    monkeypatch.setattr(pf, "BINANCE_API_KEY", "key")
    monkeypatch.setattr(pf, "BINANCE_API_SECRET", "secret")
    monkeypatch.setattr(pf, "get_latest_signal_id", lambda: 1)
    monkeypatch.setattr(pf, "record_trade", lambda **kwargs: None)

    p = _portfolio({"BTC": 0.02, "USDC": 1000.0})
    p.targets = {"BTC": 50.0, "USDC": 50.0}
    p.balance = MagicMock()
    p.balance.degraded = set()
    monkeypatch.setattr(p, "update_portfolio", lambda: None)
    monkeypatch.setattr(p, "fetch_live_data",
                        lambda: ({"BTC": 50_000.0, "USDC": 1.0},
                                 {"BTC": 1000.0, "USDC": 1000.0}, 2000.0))
    return p


def test_trade_status_precedence():
    assert pf._trade_status({"manual": True, "dust": True}) == "manual"
    assert pf._trade_status({"dust": True}) == "dust"
    assert pf._trade_status({"skipped": True}) == "skipped"
    assert pf._trade_status({"error": "boom"}) == "error"
    assert pf._trade_status({"dry_run": True}) == "dry_run"
    assert pf._trade_status({"id": "1"}) == "filled"


def test_format_trade_line_manual_points_at_kraken():
    line = pf._format_trade_line({"symbol": "PAXG", "side": "sell", "usd_value": 42.0, "manual": True})
    assert "MANUAL SELL PAXG ($42.00)" in line
    assert "Kraken" in line


def test_format_trade_line_dust_names_the_minimum():
    line = pf._format_trade_line({"symbol": "ETH/USDC", "side": "sell", "amount": 0.0001,
                                  "usd_value": 0.25, "min_usd": 5.0, "dust": True})
    assert "DUST ETH/USDC ($0.25)" in line and "below $5.00 minimum" in line


def test_format_trade_line_skipped_and_error():
    assert "SKIP HYPE/USDC" in pf._format_trade_line(
        {"symbol": "HYPE/USDC", "side": "sell", "skipped": True})
    assert "SELL ETH/USDC: trade failed" in pf._format_trade_line(
        {"symbol": "ETH/USDC", "side": "sell", "error": "boom"})


def test_format_trade_line_dry_run_and_filled():
    assert pf._format_trade_line({"symbol": "ETH/USDC", "side": "sell", "amount": 1.5,
                                  "dry_run": True}) == "📋 SELL `1.5` ETH/USDC"
    assert "id: ord2" in pf._format_trade_line(
        {"symbol": "BTC/USDC", "side": "buy", "amount": 0.01, "id": "ord2"})


def test_format_trade_line_quotes_cost_when_amount_is_unknown():
    """A quoteOrderQty buy reports what it spent, not a filled size."""
    line = pf._format_trade_line({"symbol": "BTC/USDC", "side": "buy", "amount": 0,
                                  "cost": 600.0, "id": "ord2"})
    assert "$600.00 USDC" in line


def test_plan_trades_splits_sells_buys_dust_and_manual(monkeypatch):
    monkeypatch.setattr(pf, "MANUAL_ASSETS", {"PAXG"})
    monkeypatch.setattr(pf, "MIN_TRADE_USD", 1.0)
    p = _portfolio({})
    prices = {"BTC": 50_000.0, "ETH": 2500.0, "PAXG": 3000.0, "SOL": 100.0}

    sells, buys, skipped = p._plan_trades(
        {"BTC": 0.02, "ETH": -0.4, "PAXG": 0.05, "SOL": 0.000001,
         "USDC": 500.0, "DOGE": 100.0},
        prices,
    )

    assert sells == {"ETH": 0.4}
    assert buys == {"BTC": 0.02}
    statuses = {t["symbol"]: pf._trade_status(t) for t in skipped}
    assert statuses == {"PAXG": "manual", "SOL": "dust"}
    # USDC is the stable itself and DOGE has no price — neither is planned.
    assert "DOGE" not in sells and "DOGE" not in buys


def test_calculate_rebalance_lists_buy_and_sell_legs():
    from summary import Summary

    p = _portfolio({"BTC": 0.02, "ETH": 1.0, "USDC": 0.0})
    p.summary = Summary()
    p.targets = {"BTC": 60.0, "ETH": 40.0, "USDC": 0.0}
    prices = {"BTC": 50_000.0, "ETH": 2500.0, "USDC": 1.0}
    values = {"BTC": 1000.0, "ETH": 2500.0, "USDC": 0.0}

    p.calculate_rebalance(prices, values, 3500.0)

    plan = "\n".join(p.summary.rebalances)
    assert "Rebalance Plan" in plan
    assert "Buy [" in plan and "$BTC" in plan
    assert "Sell [" in plan and "$ETH" in plan


def test_cross_pairs_returns_early_without_both_sides():
    ex = FakeExchange(free={"ETH": 1.0}, markets={"ETH/BTC": {}})
    p = _portfolio({"ETH": 1.0})

    assert p._execute_cross_pairs(ex, {}, {"BTC": 0.02}, {"BTC": 50_000.0}, dry_run=False) == []
    assert p._execute_cross_pairs(ex, {"ETH": 0.4}, {}, {"ETH": 2500.0}, dry_run=False) == []
    assert ex.orders == []


def test_cross_pairs_skips_when_matched_value_is_below_the_venue_minimum():
    ex = FakeExchange(
        free={"ETH": 1.0},
        markets={"ETH/BTC": {"limits": {"cost": {"min": 100.0}}}, "ETH/USDC": {}, "BTC/USDC": {}},
    )
    p = _portfolio({"ETH": 1.0})
    sells, buys = {"ETH": 0.4}, {"BTC": 0.0002}  # only $10 overlap

    results = p._execute_cross_pairs(ex, sells, buys, {"ETH": 2500.0, "BTC": 50_000.0}, dry_run=False)

    assert results == [] and ex.orders == []
    assert sells == {"ETH": 0.4} and buys == {"BTC": 0.0002}


def test_cross_pairs_skips_when_nothing_is_free_to_sell():
    ex = FakeExchange(free={"ETH": 0.0}, markets={"ETH/BTC": {}})
    p = _portfolio({"ETH": 1.0})

    results = p._execute_cross_pairs(ex, {"ETH": 0.4}, {"BTC": 0.02},
                                     {"ETH": 2500.0, "BTC": 50_000.0}, dry_run=False)

    assert results == [] and ex.orders == []


def test_cross_pairs_buy_side_sizes_the_order_in_the_base_token():
    """Only BTC/ETH exists → the cross is a BTC buy sized in BTC."""
    ex = FakeExchange(free={"ETH": 1.0}, markets={"BTC/ETH": {}})
    p = _portfolio({"ETH": 1.0})

    results = p._execute_cross_pairs(ex, {"ETH": 0.4}, {"BTC": 0.02},
                                     {"ETH": 2500.0, "BTC": 50_000.0}, dry_run=False)

    symbol, side, amount = ex.orders[0]
    assert (symbol, side) == ("BTC/ETH", "buy")
    assert amount == pytest.approx(0.02)
    assert results[0]["id"] == "ord1"


def test_cross_pairs_reports_precision_failure_without_trading():
    class PrecisionFailExchange(FakeExchange):
        def amount_to_precision(self, symbol, amount):
            raise ValueError("size below precision")

    ex = PrecisionFailExchange(free={"ETH": 1.0}, markets={"ETH/BTC": {}})
    p = _portfolio({"ETH": 1.0})
    sells, buys = {"ETH": 0.4}, {"BTC": 0.02}

    results = p._execute_cross_pairs(ex, sells, buys, {"ETH": 2500.0, "BTC": 50_000.0}, dry_run=False)

    assert results == [] and ex.orders == []
    assert sells == {"ETH": 0.4} and buys == {"BTC": 0.02}


def test_cross_pairs_records_order_rejection_as_an_error_leg():
    class RejectingExchange(FakeExchange):
        def create_market_order(self, symbol, side, amount, price=None):
            raise RuntimeError("binance -1013")

    ex = RejectingExchange(free={"ETH": 1.0}, markets={"ETH/BTC": {}})
    p = _portfolio({"ETH": 1.0})

    results = p._execute_cross_pairs(ex, {"ETH": 0.4}, {"BTC": 0.02},
                                     {"ETH": 2500.0, "BTC": 50_000.0}, dry_run=False)

    assert results[0]["symbol"] == "ETH/BTC"
    assert "binance -1013" in results[0]["error"]


def test_cross_pairs_never_trades_a_token_against_itself():
    """A token can only be on one side of the plan, but the loop must not build X/X."""
    ex = FakeExchange(free={"ETH": 1.0}, markets={"ETH/BTC": {}})
    p = _portfolio({"ETH": 1.0})

    results = p._execute_cross_pairs(ex, {"ETH": 0.4}, {"ETH": 0.4}, {"ETH": 2500.0}, dry_run=False)

    assert results == [] and ex.orders == []


def test_cross_pairs_skips_when_free_balance_makes_the_leg_dust():
    """Plan says $1000 but only a cent is free → the cross would be dust, so skip it."""
    ex = FakeExchange(
        free={"ETH": 0.000004},
        markets={"ETH/BTC": {"limits": {"cost": {"min": 5.0}}}},
    )
    p = _portfolio({"ETH": 1.0})

    results = p._execute_cross_pairs(ex, {"ETH": 0.4}, {"BTC": 0.02},
                                     {"ETH": 2500.0, "BTC": 50_000.0}, dry_run=False)

    assert results == [] and ex.orders == []


def test_cross_pairs_skips_when_precision_rounds_the_size_to_zero():
    class RoundToZeroExchange(FakeExchange):
        def amount_to_precision(self, symbol, amount):
            return "0"

    ex = RoundToZeroExchange(free={"ETH": 1.0}, markets={"ETH/BTC": {}})
    p = _portfolio({"ETH": 1.0})

    results = p._execute_cross_pairs(ex, {"ETH": 0.4}, {"BTC": 0.02},
                                     {"ETH": 2500.0, "BTC": 50_000.0}, dry_run=False)

    assert results == [] and ex.orders == []


def test_sells_and_buys_return_early_when_nothing_is_planned():
    ex = FakeExchange(free={"ETH": 1.0}, markets={"ETH/USDC": {}})
    p = _portfolio({"ETH": 1.0})

    assert p._execute_sells(ex, {}, {"ETH": 2500.0}, dry_run=False) == []
    assert p._execute_buys(ex, {}, {"ETH": 2500.0}, dry_run=False) == []


def test_sell_skipped_when_no_venue_pair_exists():
    ex = FakeExchange(free={"HYPE": 50.0}, markets={"ETH/USDC": {}})
    p = _portfolio({"HYPE": 50.0})

    results = p._execute_sells(ex, {"HYPE": 10.0}, {"HYPE": 48.0}, dry_run=False)

    assert results[0]["skipped"] is True
    assert ex.orders == []


def test_sell_reports_error_when_size_rounds_to_zero():
    class RoundToZeroExchange(FakeExchange):
        def amount_to_precision(self, symbol, amount):
            return "0"

    ex = RoundToZeroExchange(free={"ETH": 1.0}, markets={"ETH/USDC": {}})
    p = _portfolio({"ETH": 1.0})

    results = p._execute_sells(ex, {"ETH": 0.5}, {"ETH": 2500.0}, dry_run=False)

    assert results[0]["error"] == pf.ERR_SIZE_BELOW_PRECISION
    assert ex.orders == []


def test_sell_records_order_rejection_as_an_error_leg():
    class RejectingExchange(FakeExchange):
        def create_market_order(self, symbol, side, amount, price=None):
            raise RuntimeError("binance -2010")

    ex = RejectingExchange(free={"ETH": 1.0}, markets={"ETH/USDC": {}})
    p = _portfolio({"ETH": 1.0})

    results = p._execute_sells(ex, {"ETH": 0.5}, {"ETH": 2500.0}, dry_run=False)

    assert "binance -2010" in results[0]["error"]


def test_buy_skipped_when_no_venue_pair_exists():
    ex = FakeExchange(free={"USDC": 1000.0}, markets={"BTC/USDC": {}})
    p = _portfolio({"USDC": 1000.0})

    results = p._execute_buys(ex, {"HYPE": 10.0}, {"HYPE": 48.0}, dry_run=False)

    assert results[0]["skipped"] is True
    assert ex.orders == []


def test_buy_records_order_rejection_as_an_error_leg():
    class RejectingExchange(FakeExchange):
        def create_market_buy_order_with_cost(self, symbol, cost):
            raise RuntimeError("binance -1013")

    ex = RejectingExchange(free={"USDC": 1000.0}, markets={"BTC/USDC": {}})
    p = _portfolio({"USDC": 1000.0})

    results = p._execute_buys(ex, {"BTC": 0.012}, {"BTC": 50_000.0}, dry_run=False)

    assert "binance -1013" in results[0]["error"]
    assert results[0]["cost"] == pytest.approx(600.0)


def test_persist_trades_only_stores_filled_and_error_legs(monkeypatch):
    monkeypatch.setattr(pf, "get_latest_signal_id", lambda: 7)
    rows = []
    monkeypatch.setattr(pf, "record_trade", lambda **kwargs: rows.append(kwargs))
    p = _portfolio({})

    p._persist_trades(
        [
            {"symbol": "ETH/USDC", "side": "sell", "amount": 1.0, "id": "ord1",
             "fee_amount": 0.5, "fee_currency": "USDC", "fee_rate": 0.001},
            {"symbol": "BTC/USDC", "side": "buy", "amount": 0.01, "error": "boom"},
            {"symbol": "PAXG", "side": "sell", "usd_value": 42.0, "manual": True},
            {"symbol": "SOL/USDC", "side": "sell", "amount": 0.0001, "dust": True},
            {"symbol": "ETH/USDC", "side": "sell", "amount": 1.0, "dry_run": True},
        ],
        {"ETH": 2500.0, "BTC": 50_000.0},
    )

    assert [(r["symbol"], r["status"]) for r in rows] == [
        ("ETH/USDC", "filled"), ("BTC/USDC", "error")]
    assert rows[0]["signal_id"] == 7
    assert rows[0]["price"] == 2500.0
    assert rows[0]["fee_amount"] == 0.5


def test_execute_hype_skips_without_credentials(monkeypatch):
    monkeypatch.setattr(pf, "HYPERLIQUID_PRIVATE_KEY", None)
    monkeypatch.setattr(pf, "HYPERLIQUID_ACCOUNT_ADDRESS", None)
    p = _portfolio({"HYPE": 50.0})

    results = p._execute_hype(-10.0, "sell", {"HYPE": 48.0}, dry_run=False)

    assert results[0] == {"symbol": "HYPE/USDC", "side": "sell", "amount": 10.0, "skipped": True}


def test_execute_hype_reports_connection_failure(monkeypatch):
    monkeypatch.setattr(pf, "HYPERLIQUID_PRIVATE_KEY", "x")
    monkeypatch.setattr(pf, "HYPERLIQUID_ACCOUNT_ADDRESS", "0xagent")

    def boom(*args, **kwargs):
        raise RuntimeError("hyperliquid unreachable")
    monkeypatch.setattr(pf, "create_hyperliquid", boom)
    p = _portfolio({"HYPE": 50.0})

    results = p._execute_hype(10.0, "buy", {"HYPE": 48.0}, dry_run=False)

    assert "hyperliquid unreachable" in results[0]["error"]


def test_execute_hype_reports_order_failure(monkeypatch):
    monkeypatch.setattr(pf, "HYPERLIQUID_PRIVATE_KEY", "x")
    monkeypatch.setattr(pf, "HYPERLIQUID_ACCOUNT_ADDRESS", "0xagent")
    hl = MagicMock()
    hl.amount_to_precision = lambda symbol, amount: f"{float(amount):.6f}"
    monkeypatch.setattr(pf, "create_hyperliquid", lambda *a, **kw: hl)

    def boom(*args, **kwargs):
        raise RuntimeError("Order has zero size")
    monkeypatch.setattr(pf, "place_order", boom)
    p = _portfolio({"HYPE": 50.0})

    results = p._execute_hype(10.0, "buy", {"HYPE": 48.0}, dry_run=False)

    assert "Order has zero size" in results[0]["error"]


def test_execute_rebalance_refuses_without_binance_credentials(monkeypatch):
    monkeypatch.setattr(pf, "BINANCE_API_KEY", None)
    p = _portfolio({})

    assert "credentials not set" in p.execute_rebalance(dry_run=True)


def test_execute_rebalance_refuses_on_degraded_balances(live_portfolio):
    live_portfolio.balance.degraded = {"kraken"}

    out = live_portfolio.execute_rebalance(dry_run=True)

    assert "Balance fetch failed (kraken)" in out
    assert "refusing to trade" in out


def test_execute_rebalance_reports_a_balanced_portfolio(monkeypatch, live_portfolio):
    monkeypatch.setattr(pf, "REBALANCE_RESERVE_PCT", 0.0)  # holdings sit exactly on target
    assert live_portfolio.execute_rebalance(dry_run=True) == "✅ Portfolio is balanced — no trades needed."


def test_execute_rebalance_reports_nothing_executable(monkeypatch, live_portfolio):
    """Every leg is manual or dust → report them instead of claiming balance."""
    monkeypatch.setattr(pf, "MANUAL_ASSETS", {"PAXG"})
    live_portfolio.portfolio = {"PAXG": 0.5, "USDC": 1000.0}
    live_portfolio.targets = {"PAXG": 90.0, "USDC": 10.0}
    monkeypatch.setattr(live_portfolio, "fetch_live_data",
                        lambda: ({"PAXG": 3000.0, "USDC": 1.0},
                                 {"PAXG": 1500.0, "USDC": 1000.0}, 2500.0))

    out = live_portfolio.execute_rebalance(dry_run=True)

    assert "nothing executable" in out
    assert "MANUAL BUY PAXG" in out


def test_execute_rebalance_reports_binance_connection_failure(monkeypatch, live_portfolio):
    live_portfolio.targets = {"BTC": 90.0, "USDC": 10.0}

    def boom(*args, **kwargs):
        raise RuntimeError("dns failure")
    monkeypatch.setattr(pf, "create_binance", boom)

    assert "Failed to connect to Binance" in live_portfolio.execute_rebalance(dry_run=True)


def test_execute_rebalance_routes_hype_to_hyperliquid(monkeypatch, live_portfolio):
    live_portfolio.portfolio = {"HYPE": 50.0, "USDC": 1000.0}
    live_portfolio.targets = {"HYPE": 10.0, "USDC": 90.0}
    monkeypatch.setattr(live_portfolio, "fetch_live_data",
                        lambda: ({"HYPE": 48.0, "USDC": 1.0},
                                 {"HYPE": 2400.0, "USDC": 1000.0}, 3400.0))
    hype_calls = []
    monkeypatch.setattr(
        pf.Portfolio, "_execute_hype",
        lambda self, amount, side, prices, dry_run: hype_calls.append((side, amount))
        or [{"symbol": "HYPE/USDC", "side": side, "amount": abs(amount), "id": "hl1"}],
    )

    def no_binance(*args, **kwargs):
        raise AssertionError("HYPE-only rebalance must not open a Binance session")
    monkeypatch.setattr(pf, "create_binance", no_binance)

    out = live_portfolio.execute_rebalance(dry_run=True)

    assert [side for side, _ in hype_calls] == ["sell"]
    assert "HYPE/USDC" in out


def test_execute_rebalance_routes_a_hype_buy_to_hyperliquid(monkeypatch, live_portfolio):
    live_portfolio.portfolio = {"HYPE": 5.0, "USDC": 1000.0}
    live_portfolio.targets = {"HYPE": 50.0, "USDC": 50.0}
    monkeypatch.setattr(live_portfolio, "fetch_live_data",
                        lambda: ({"HYPE": 48.0, "USDC": 1.0},
                                 {"HYPE": 240.0, "USDC": 1000.0}, 1240.0))
    hype_calls = []
    monkeypatch.setattr(
        pf.Portfolio, "_execute_hype",
        lambda self, amount, side, prices, dry_run: hype_calls.append((side, amount))
        or [{"symbol": "HYPE/USDC", "side": side, "amount": abs(amount), "id": "hl1"}],
    )

    live_portfolio.execute_rebalance(dry_run=True)

    assert [side for side, _ in hype_calls] == ["buy"]


def test_execute_rebalance_persists_only_live_trades(monkeypatch, live_portfolio):
    live_portfolio.targets = {"BTC": 90.0, "USDC": 10.0}
    ex = FakeExchange(free={"BTC": 0.02, "USDC": 1000.0}, markets={"BTC/USDC": {}})
    monkeypatch.setattr(pf, "create_binance", lambda *a, **kw: ex)
    rows = []
    monkeypatch.setattr(pf, "record_trade", lambda **kwargs: rows.append(kwargs))

    dry = live_portfolio.execute_rebalance(dry_run=True)
    assert "DRY RUN" in dry
    assert rows == []

    live = live_portfolio.execute_rebalance(dry_run=False)
    assert "LIVE" in live
    assert [r["symbol"] for r in rows] == ["BTC/USDC"]


def test_get_and_set_target_round_trip():
    p = _portfolio({})
    p.targets = {"BTC": 60.0}

    assert p.get_targets() == {"BTC": 60.0}
    assert p.set_target("ETH", 40) == {"BTC": 60.0, "ETH": 40}


def test_cross_pairs_skips_a_leg_planned_at_zero(monkeypatch):
    """A leg left at 0 after an earlier match must not become a zero-size order."""
    monkeypatch.setattr(pf, "MIN_TRADE_USD", 0.0)
    ex = FakeExchange(free={"ETH": 1.0}, markets={"ETH/BTC": {}})
    p = _portfolio({"ETH": 1.0})

    results = p._execute_cross_pairs(ex, {"ETH": 0.0}, {"BTC": 0.02},
                                     {"ETH": 2500.0, "BTC": 50_000.0}, dry_run=False)

    assert results == [] and ex.orders == []


def test_listener_reports_drift_and_records_a_snapshot(monkeypatch):
    from summary import Summary

    p = _portfolio({"BTC": 0.02, "USDC": 1000.0})
    p.summary = Summary()
    p.targets = {"BTC": 50.0, "USDC": 50.0}
    p.balance = MagicMock()
    p.balance.degraded = set()
    monkeypatch.setattr(p, "update_portfolio", lambda: None)
    monkeypatch.setattr(p, "fetch_live_data",
                        lambda: ({"BTC": 50_000.0, "USDC": 1.0},
                                 {"BTC": 1500.0, "USDC": 1000.0}, 2500.0))
    monkeypatch.setattr(pf, "get_latest_signal_id", lambda: 3)
    snapshots = []
    monkeypatch.setattr(pf, "record_snapshot", lambda **kwargs: snapshots.append(kwargs))

    out = p.listener()

    assert p.send_rebalance is True
    assert "Rebalance Needed" in out and "Rebalance Plan" in out
    assert snapshots[0]["signal_id"] == 3
    assert snapshots[0]["partial"] is False


def test_listener_flags_a_partial_snapshot_when_a_venue_is_degraded(monkeypatch):
    from summary import Summary

    p = _portfolio({"BTC": 0.02, "USDC": 1000.0})
    p.summary = Summary()
    p.targets = {"BTC": 50.0, "USDC": 50.0}
    p.balance = MagicMock()
    p.balance.degraded = {"kraken"}
    monkeypatch.setattr(p, "update_portfolio", lambda: None)
    monkeypatch.setattr(p, "fetch_live_data",
                        lambda: ({"BTC": 50_000.0, "USDC": 1.0},
                                 {"BTC": 1000.0, "USDC": 1000.0}, 2000.0))
    monkeypatch.setattr(pf, "get_latest_signal_id", lambda: 3)
    snapshots = []
    monkeypatch.setattr(pf, "record_snapshot", lambda **kwargs: snapshots.append(kwargs))

    out = p.listener()

    assert "Partial snapshot" in out and "kraken" in out
    assert snapshots[0]["partial"] is True


def test_cross_pairs_keeps_the_larger_buy_leg_for_usdc_routing():
    """Sell side is the smaller one — the buy leg survives the cross with a remainder."""
    ex = FakeExchange(free={"ETH": 0.12}, markets={"ETH/BTC": {}, "ETH/USDC": {}, "BTC/USDC": {}})
    p = _portfolio({"ETH": 0.12})
    sells, buys = {"ETH": 0.12}, {"BTC": 0.02}   # $300 sell vs $1000 buy
    prices = {"ETH": 2500.0, "BTC": 50_000.0}

    p._execute_cross_pairs(ex, sells, buys, prices, dry_run=False)

    assert "ETH" not in sells, "the sell leg is fully consumed"
    assert buys["BTC"] * prices["BTC"] == pytest.approx(700.0, rel=1e-2)
