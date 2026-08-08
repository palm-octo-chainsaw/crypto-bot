"""Exchange minimum notionals and manual-only assets."""
import pytest

import portfolio as pf
from data.trading import min_notional, effective_min_usd


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
        self.orders.append(("market", symbol, side, amount))
        return {"id": "ord1", "status": "closed", "fee": {},
                "symbol": symbol, "side": side, "amount": amount}

    def create_market_buy_order_with_cost(self, symbol, cost):
        self.orders.append(("cost", symbol, "buy", cost))
        return {"id": "ord2", "status": "closed", "filled": cost / 3000, "cost": cost, "fee": {},
                "symbol": symbol, "side": "buy"}


def _market(min_cost: float | None) -> dict:
    return {"limits": {"cost": {"min": min_cost}}}


def _portfolio(holdings: dict) -> pf.Portfolio:
    p = pf.Portfolio.__new__(pf.Portfolio)
    p.portfolio = holdings
    return p


# --- exchange minimum notional -------------------------------------------------

def test_min_notional_reads_ccxt_cost_limit():
    ex = FakeExchange(free={}, markets={"ETH/USDC": _market(5.0)})
    assert min_notional(ex, "ETH/USDC") == 5.0


def test_min_notional_defaults_to_zero_for_unknown_symbol_or_limit():
    ex = FakeExchange(free={}, markets={"ETH/USDC": _market(None)})
    assert min_notional(ex, "ETH/USDC") == 0.0
    assert min_notional(ex, "NOPE/USDC") == 0.0


def test_effective_min_prefers_the_stricter_of_the_two():
    ex = FakeExchange(free={}, markets={"ETH/USDC": _market(5.0), "DOGE/USDC": _market(0.5)})
    assert effective_min_usd(ex, "ETH/USDC", 1.0) == 5.0   # exchange stricter
    assert effective_min_usd(ex, "DOGE/USDC", 1.0) == 1.0  # local floor stricter


def test_sell_below_exchange_notional_is_dust_not_an_order():
    """The -1013 case: 0.001 ETH (~$3) clears the $1 local floor but not Binance's $5."""
    ex = FakeExchange(free={"ETH": 0.001}, markets={"ETH/USDC": _market(5.0)})
    portfolio = _portfolio({"ETH": 0.001})

    results = portfolio._execute_sells(ex, {"ETH": 0.001}, {"ETH": 3000.0}, dry_run=False)

    assert ex.orders == []
    assert results[0]["dust"] is True
    assert results[0]["min_usd"] == 5.0


def test_buy_below_exchange_notional_is_dust_not_an_order():
    ex = FakeExchange(free={"USDC": 100.0}, markets={"ETH/USDC": _market(5.0)})
    portfolio = _portfolio({"ETH": 1.0, "USDC": 100.0})

    results = portfolio._execute_buys(ex, {"ETH": 0.001}, {"ETH": 3000.0}, dry_run=False)

    assert ex.orders == []
    assert results[0]["dust"] is True


def test_sell_above_exchange_notional_still_executes():
    ex = FakeExchange(free={"ETH": 1.0}, markets={"ETH/USDC": _market(5.0)})
    portfolio = _portfolio({"ETH": 1.0})

    portfolio._execute_sells(ex, {"ETH": 0.5}, {"ETH": 3000.0}, dry_run=False)

    assert len(ex.orders) == 1
    assert ex.orders[0][1] == "ETH/USDC"


def test_missing_cost_limit_falls_back_to_local_floor():
    """ccxt omits the cost limit for some markets — the $1 floor must still apply."""
    ex = FakeExchange(free={"ETH": 1.0}, markets={"ETH/USDC": _market(None)})
    portfolio = _portfolio({"ETH": 1.0})

    results = portfolio._execute_sells(ex, {"ETH": 0.0001}, {"ETH": 3000.0}, dry_run=False)

    assert ex.orders == []
    assert results[0]["dust"] is True
    assert results[0]["min_usd"] == pf.MIN_TRADE_USD


# --- manual-only assets --------------------------------------------------------

def test_manual_asset_never_reaches_the_exchange_plan(monkeypatch):
    """PAXG is read from Kraken but no bot venue trades it — it must not become a buy leg."""
    monkeypatch.setattr(pf, "MANUAL_ASSETS", {"PAXG"})
    portfolio = _portfolio({"ETH": 1.0, "PAXG": 0.0})

    sells, buys, skipped = portfolio._plan_trades(
        {"ETH": -0.5, "PAXG": 0.5}, {"ETH": 3000.0, "PAXG": 3300.0}
    )

    assert buys == {}
    assert sells == {"ETH": 0.5}   # sell still runs; proceeds rest in USDC
    manual = [t for t in skipped if t.get("manual")]
    assert len(manual) == 1
    assert manual[0]["symbol"] == "PAXG"
    assert manual[0]["usd_value"] == pytest.approx(1650.0)


def test_manual_asset_line_tells_user_where_to_trade():
    line = pf._format_trade_line(
        {"symbol": "PAXG", "side": "buy", "usd_value": 1650.0, "manual": True}
    )
    assert "MANUAL" in line and "PAXG" in line and "Kraken" in line


def test_non_manual_assets_are_unaffected(monkeypatch):
    monkeypatch.setattr(pf, "MANUAL_ASSETS", {"PAXG"})
    portfolio = _portfolio({"BTC": 1.0})

    sells, buys, skipped = portfolio._plan_trades({"BTC": 0.5}, {"BTC": 60_000.0})

    assert buys == {"BTC": 0.5}
    assert skipped == []


def test_dust_beats_manual_when_the_leg_is_tiny(monkeypatch):
    monkeypatch.setattr(pf, "MANUAL_ASSETS", {"PAXG"})
    portfolio = _portfolio({"PAXG": 0.0})

    _, _, skipped = portfolio._plan_trades({"PAXG": 0.0001}, {"PAXG": 3300.0})

    assert skipped[0].get("dust") is True
    assert skipped[0].get("manual") is None


def test_manual_only_plan_reports_instead_of_claiming_balanced(monkeypatch):
    """A plan holding nothing but PAXG must not report '✅ balanced'."""
    monkeypatch.setattr(pf, "MANUAL_ASSETS", {"PAXG"})
    monkeypatch.setattr(pf, "BINANCE_API_KEY", "k")
    monkeypatch.setattr(pf, "BINANCE_API_SECRET", "s")

    portfolio = _portfolio({"PAXG": 0.0, "USDC": 2000.0})
    portfolio.targets = {"PAXG": 100.0, "USDC": 0.0}
    portfolio.update_portfolio = lambda: None
    portfolio.fetch_live_data = lambda: (
        {"PAXG": 3300.0, "USDC": 1.0}, {"PAXG": 0.0, "USDC": 2000.0}, 2000.0,
    )

    message = portfolio.execute_rebalance(dry_run=False)

    assert "MANUAL" in message and "PAXG" in message
    assert "balanced" not in message
