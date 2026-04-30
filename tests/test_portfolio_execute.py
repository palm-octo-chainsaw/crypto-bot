"""Fraction-based sell/buy execution in Portfolio."""
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
        self.orders.append(("market", symbol, side, amount))
        return {"id": "ord1", "status": "closed", "fee": {}}

    def create_market_buy_order_with_cost(self, symbol, cost):
        self.orders.append(("cost", symbol, "buy", cost))
        return {"id": "ord2", "status": "closed", "filled": cost / 50_000, "cost": cost, "fee": {}}


def _portfolio(holdings: dict) -> pf.Portfolio:
    p = pf.Portfolio.__new__(pf.Portfolio)
    p.portfolio = holdings
    return p


def test_sell_full_liquidation_uses_free_balance_when_snapshot_overstates():
    """Planned amount 1.1673 > free 1.16589 — must sell 100% of free, not planned."""
    holdings = {"ETH": 1.1673}
    free = {"ETH": 1.16589293}
    ex = FakeExchange(free=free, markets={"ETH/USDC": {}})

    portfolio = _portfolio(holdings)
    results = portfolio._execute_sells(ex, {"ETH": 1.1673}, {"ETH": 2500.0}, dry_run=False)

    assert len(ex.orders) == 1
    _, symbol, side, amount = ex.orders[0]
    assert symbol == "ETH/USDC"
    assert side == "sell"
    assert amount == pytest.approx(1.16589293, rel=1e-4)
    assert results[0].get("error") is None


def test_sell_partial_fraction():
    """Planned 0.5 of holdings 2.0 → sells 50% of free balance."""
    ex = FakeExchange(free={"ETH": 1.8}, markets={"ETH/USDC": {}})
    portfolio = _portfolio({"ETH": 2.0})
    portfolio._execute_sells(ex, {"ETH": 1.0}, {"ETH": 2500.0}, dry_run=False)

    _, _, _, amount = ex.orders[0]
    assert amount == pytest.approx(0.9, rel=1e-4)


def test_buy_uses_quote_order_qty_fraction_of_free_usdc():
    """Two planned buys — cost per buy = free_usdc * (intended_usd / total_intended_usd)."""
    ex = FakeExchange(
        free={"USDC": 1000.0},
        markets={"BTC/USDC": {}, "ETH/USDC": {}},
    )
    portfolio = _portfolio({"BTC": 0.0, "ETH": 0.0, "USDC": 1000.0})
    prices = {"BTC": 50_000.0, "ETH": 2_500.0}
    portfolio._execute_buys(
        ex,
        {"BTC": 0.012, "ETH": 0.16},    # $600 BTC, $400 ETH → 60/40 split
        prices,
        dry_run=False,
    )

    costs = {symbol: cost for kind, symbol, _, cost in ex.orders if kind == "cost"}
    assert costs["BTC/USDC"] == pytest.approx(600.0, rel=1e-4)
    assert costs["ETH/USDC"] == pytest.approx(400.0, rel=1e-4)


def test_buy_no_stable_balance_errors_out():
    ex = FakeExchange(free={"USDC": 0.0}, markets={"BTC/USDC": {}})
    portfolio = _portfolio({"BTC": 0.0, "USDC": 0.0})
    results = portfolio._execute_buys(ex, {"BTC": 0.01}, {"BTC": 50_000.0}, dry_run=False)

    assert ex.orders == []
    assert "USDC" in results[0]["error"]


def test_sell_zero_free_balance_skips_order():
    ex = FakeExchange(free={"ETH": 0.0}, markets={"ETH/USDC": {}})
    portfolio = _portfolio({"ETH": 1.0})
    results = portfolio._execute_sells(ex, {"ETH": 0.5}, {"ETH": 2500.0}, dry_run=False)

    assert ex.orders == []
    assert results[0]["error"] == "zero balance"


def test_cross_pair_matches_sell_and_buy_directly():
    """ETH sell + BTC buy with ETH/BTC market → one direct cross trade, both legs decremented."""
    ex = FakeExchange(
        free={"ETH": 1.0, "BTC": 0.0, "USDC": 0.0},
        markets={"ETH/BTC": {}, "ETH/USDC": {}, "BTC/USDC": {}},
    )
    portfolio = _portfolio({"ETH": 1.0, "BTC": 0.0})
    sells = {"ETH": 0.4}              # 0.4 ETH = $1000 @ $2500
    buys = {"BTC": 0.02}              # 0.02 BTC = $1000 @ $50000
    prices = {"ETH": 2500.0, "BTC": 50_000.0}

    results = portfolio._execute_cross_pairs(ex, sells, buys, prices, dry_run=False)

    assert len(ex.orders) == 1
    kind, symbol, side, amount = ex.orders[0]
    assert symbol == "ETH/BTC"
    assert side == "sell"
    assert amount == pytest.approx(0.4, rel=1e-3)
    assert "ETH" not in sells
    assert "BTC" not in buys
    assert results[0].get("error") is None


def test_cross_pair_partial_match_leaves_remainder_for_usdc_routing():
    """ETH sell of $1000 vs BTC buy of $300 → cross only $300 worth, ETH remainder stays."""
    ex = FakeExchange(
        free={"ETH": 1.0},
        markets={"ETH/BTC": {}, "ETH/USDC": {}, "BTC/USDC": {}},
    )
    portfolio = _portfolio({"ETH": 1.0})
    sells = {"ETH": 0.4}              # $1000 worth
    buys = {"BTC": 0.006}             # $300 worth
    prices = {"ETH": 2500.0, "BTC": 50_000.0}

    portfolio._execute_cross_pairs(ex, sells, buys, prices, dry_run=False)

    _, symbol, side, amount = ex.orders[0]
    assert symbol == "ETH/BTC"
    # ~0.12 ETH (= $300 at $2500/ETH) executed
    assert amount == pytest.approx(0.12, rel=1e-2)
    # BTC fully matched → removed; ETH remains with ~0.28 leftover
    assert "BTC" not in buys
    assert sells["ETH"] == pytest.approx(0.28, rel=1e-2)


def test_cross_pair_skips_when_no_direct_market():
    """No ETH/BTC market → no cross trade, both legs unchanged."""
    ex = FakeExchange(
        free={"ETH": 1.0},
        markets={"ETH/USDC": {}, "BTC/USDC": {}},  # no ETH/BTC
    )
    portfolio = _portfolio({"ETH": 1.0})
    sells = {"ETH": 0.4}
    buys = {"BTC": 0.02}
    prices = {"ETH": 2500.0, "BTC": 50_000.0}

    results = portfolio._execute_cross_pairs(ex, sells, buys, prices, dry_run=False)

    assert ex.orders == []
    assert results == []
    assert sells == {"ETH": 0.4}
    assert buys == {"BTC": 0.02}


def test_sell_dust_when_free_balance_below_min_trade_usd():
    """Free balance worth < MIN_TRADE_USD → marked dust, not attempted as a trade. Other legs execute."""
    ex = FakeExchange(
        free={"ETH": 0.00005, "BTC": 0.5},
        markets={"ETH/USDC": {}, "BTC/USDC": {}},
    )
    portfolio = _portfolio({"ETH": 0.00005, "BTC": 0.5})
    prices = {"ETH": 2500.0, "BTC": 50_000.0}
    results = portfolio._execute_sells(ex, {"ETH": 0.00005, "BTC": 0.1}, prices, dry_run=False)

    eth_result = next(r for r in results if r.get("symbol") == "ETH/USDC")
    assert eth_result.get("dust") is True
    assert eth_result.get("error") is None
    assert any(o[1] == "BTC/USDC" for o in ex.orders), "BTC sell should still execute"


def test_sell_precision_error_caught_when_dust_check_passes():
    """Defensive path: if apply_precision still raises after the dust check, skip cleanly."""
    from ccxt.base.errors import InvalidOrder

    class PrecisionFailExchange(FakeExchange):
        def amount_to_precision(self, symbol, amount):
            if symbol == "ETH/USDC":
                raise InvalidOrder(f"binance amount of {symbol} unexpected error")
            return f"{float(amount):.6f}"

    ex = PrecisionFailExchange(
        free={"ETH": 1.0, "BTC": 0.5},
        markets={"ETH/USDC": {}, "BTC/USDC": {}},
    )
    portfolio = _portfolio({"ETH": 1.0, "BTC": 0.5})
    prices = {"ETH": 2500.0, "BTC": 50_000.0}
    results = portfolio._execute_sells(ex, {"ETH": 0.5, "BTC": 0.1}, prices, dry_run=False)

    eth_result = next(r for r in results if r.get("symbol") == "ETH/USDC")
    assert eth_result["error"] == "size below precision"
    assert any(o[1] == "BTC/USDC" for o in ex.orders)
