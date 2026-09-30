"""Order placement, fee lookup and stable-routing in data/trading."""
import pytest

from data import trading


class FakeExchange:
    def __init__(self, markets=None):
        self.markets = markets if markets is not None else {}
        self.orders = []
        self.response = {"id": "ord1", "status": "closed"}

    def amount_to_precision(self, symbol, amount):
        return f"{float(amount):.6f}"

    def cost_to_precision(self, symbol, cost):
        return f"{float(cost):.2f}"

    def create_market_order(self, symbol, side, amount, price=None):
        self.orders.append((symbol, side, amount, price))
        return dict(self.response)

    def create_market_buy_order_with_cost(self, symbol, cost):
        self.orders.append((symbol, "buy", cost, None))
        return dict(self.response)



def test_min_notional_reads_cost_filter():
    ex = FakeExchange(markets={"ETH/USDC": {"limits": {"cost": {"min": 5.0}}}})
    assert trading.min_notional(ex, "ETH/USDC") == 5.0


def test_min_notional_zero_when_filter_unparseable():
    """A non-numeric NOTIONAL must not take the whole rebalance down."""
    ex = FakeExchange(markets={"ETH/USDC": {"limits": {"cost": {"min": "not-a-number"}}}})
    assert trading.min_notional(ex, "ETH/USDC") == 0.0


def test_effective_min_usd_takes_the_larger_floor():
    ex = FakeExchange(markets={"ETH/USDC": {"limits": {"cost": {"min": 5.0}}}})
    assert trading.effective_min_usd(ex, "ETH/USDC", 1.0) == 5.0
    assert trading.effective_min_usd(ex, "ETH/USDC", 9.0) == 9.0





def test_place_order_dry_run_places_nothing():
    ex = FakeExchange()
    order = trading.place_order(ex, "ETH/USDC", "sell", 1.5, dry_run=True)

    assert ex.orders == []
    assert order == {"symbol": "ETH/USDC", "side": "sell", "amount": 1.5, "dry_run": True}


def test_place_order_fills_in_venue_omissions():
    """A venue that answers with the fill alone — symbol/side/amount come from the request."""
    ex = FakeExchange()
    ex.response = {"id": "hl1", "status": "closed", "symbol": None, "side": None, "fee": {}}

    order = trading.place_order(ex, "HYPE/USDC", "sell", 33.0, dry_run=False)

    assert order["symbol"] == "HYPE/USDC"
    assert order["side"] == "sell"
    assert order["amount"] == 33.0



def test_place_market_buy_cost_dry_run():
    ex = FakeExchange()
    order = trading.place_market_buy_cost(ex, "BTC/USDC", 600.456, dry_run=True)

    assert ex.orders == []
    assert order == {"symbol": "BTC/USDC", "side": "buy", "cost": 600.46, "dry_run": True}


def test_place_market_buy_cost_backfills_amount_and_cost():
    ex = FakeExchange()
    ex.response = {"id": "ord2", "status": "closed", "filled": 0.012, "fee": {"cost": 0.6, "currency": "USDC"}}

    order = trading.place_market_buy_cost(ex, "BTC/USDC", 600.0, dry_run=False)

    assert order["amount"] == 0.012
    assert order["cost"] == 600.0
    assert order["fee_amount"] == 0.6


def test_execute_trade_uses_direct_pair():
    ex = FakeExchange(markets={"ETH/BTC": {}})
    trades = trading.execute_trade(ex, "ETH", "BTC", 1.0, {"ETH": 2500.0, "BTC": 50_000.0},
                                   stable="USDC", dry_run=True)

    assert len(trades) == 1
    assert trades[0]["symbol"] == "ETH/BTC"
    assert trades[0]["side"] == "sell"


def test_execute_trade_direct_pair_inverted_buys_the_quote_amount():
    """Only BTC/ETH exists → buy BTC/ETH sized in BTC, not sell ETH."""
    ex = FakeExchange(markets={"BTC/ETH": {}})
    trades = trading.execute_trade(ex, "ETH", "BTC", 1.0, {"ETH": 2500.0, "BTC": 50_000.0},
                                   stable="USDC", dry_run=True)

    assert trades[0]["symbol"] == "BTC/ETH"
    assert trades[0]["side"] == "buy"
    assert trades[0]["amount"] == pytest.approx(0.05)


def test_execute_trade_routes_through_stable_in_two_legs():
    ex = FakeExchange(markets={"ETH/USDC": {}, "BTC/USDC": {}})
    trades = trading.execute_trade(ex, "ETH", "BTC", 1.0, {"ETH": 2500.0, "BTC": 50_000.0},
                                   stable="USDC", dry_run=True)

    assert [t["symbol"] for t in trades] == ["ETH/USDC", "BTC/USDC"]
    assert [t["side"] for t in trades] == ["sell", "buy"]
    # Leg 2 buys $2500 worth of BTC with the stable the first leg raised.
    assert trades[1]["amount"] == pytest.approx(0.05)


def test_execute_trade_stops_when_sell_leg_has_no_pair():
    ex = FakeExchange(markets={"BTC/USDC": {}})
    trades = trading.execute_trade(ex, "ETH", "BTC", 1.0, {"ETH": 2500.0, "BTC": 50_000.0},
                                   stable="USDC", dry_run=True)

    assert trades == []


def test_execute_trade_keeps_first_leg_when_buy_leg_has_no_pair():
    """Half-routed is still reported: the sold leg rests in the stable."""
    ex = FakeExchange(markets={"ETH/USDC": {}})
    trades = trading.execute_trade(ex, "ETH", "BTC", 1.0, {"ETH": 2500.0, "BTC": 50_000.0},
                                   stable="USDC", dry_run=True)

    assert len(trades) == 1
    assert trades[0]["symbol"] == "ETH/USDC"


def test_create_binance_enables_rate_limiting_and_loads_markets(monkeypatch):
    created = {}

    class FakeBinance:
        def __init__(self, config):
            created["config"] = config
            self.loaded = False

        def load_markets(self):
            self.loaded = True

    monkeypatch.setattr(trading.ccxt, "binance", FakeBinance)

    exchange = trading.create_binance("key", "secret")

    assert created["config"] == {"apiKey": "key", "secret": "secret", "enableRateLimit": True}
    assert exchange.loaded is True


def test_execute_trade_routes_through_inverted_stable_pairs():
    """Only USDC-base markets exist: leg 1 buys the stable pair, leg 2 sells it."""
    ex = FakeExchange(markets={"USDC/ETH": {}, "USDC/BTC": {}})
    trades = trading.execute_trade(ex, "ETH", "BTC", 1.0, {"ETH": 2500.0, "BTC": 50_000.0},
                                   stable="USDC", dry_run=True)

    assert [(t["symbol"], t["side"]) for t in trades] == [("USDC/ETH", "buy"), ("USDC/BTC", "sell")]
    assert trades[0]["amount"] == pytest.approx(2500.0)
    assert trades[1]["amount"] == pytest.approx(2500.0)
