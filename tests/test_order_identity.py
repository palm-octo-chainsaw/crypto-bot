"""A venue that omits symbol/side from its order response must not erase the trade."""
import portfolio as pf
from data.trading import place_order, place_market_buy_cost


class HyperliquidLikeExchange:
    """Answers with the fill alone, the way Hyperliquid does.

    ccxt has no coin or side to parse out of that response, so both come back
    None — the shape that produced "✅ `23.33` None" in Telegram.
    """

    def __init__(self):
        self.orders = []

    def create_market_order(self, symbol, side, amount, price=None):
        self.orders.append((symbol, side, amount))
        return {"id": "525666199143", "status": "closed", "fee": None,
                "symbol": None, "side": None, "amount": 23.33, "filled": 23.33}

    def cost_to_precision(self, symbol, cost):
        return f"{float(cost):.2f}"

    def create_market_buy_order_with_cost(self, symbol, cost):
        return {"id": "525666199144", "status": "closed", "fee": None,
                "symbol": None, "side": None, "filled": 0.5}


class BinanceLikeExchange:
    def cost_to_precision(self, symbol, cost):
        return f"{float(cost):.2f}"

    def create_market_order(self, symbol, side, amount, price=None):
        return {"id": "544826808", "status": "closed", "fee": {},
                "symbol": "SOL/ETH", "side": "buy", "amount": amount}

    def create_market_buy_order_with_cost(self, symbol, cost):
        return {"id": "544826809", "status": "closed", "fee": {},
                "symbol": symbol, "side": "buy", "filled": 0.205, "cost": cost}


def test_market_order_keeps_the_symbol_we_asked_for():
    order = place_order(HyperliquidLikeExchange(), "HYPE/USDC", "sell", 23.33, dry_run=False)

    assert order["symbol"] == "HYPE/USDC"
    assert order["side"] == "sell"
    assert order["amount"] == 23.33


def test_cost_buy_keeps_the_symbol_we_asked_for():
    order = place_market_buy_cost(HyperliquidLikeExchange(), "HYPE/USDC", 50.0, dry_run=False)

    assert order["symbol"] == "HYPE/USDC"
    assert order["side"] == "buy"


def test_venue_supplied_identity_wins():
    """Binance routes cross-pairs, where the executed symbol is the pair, not our label."""
    order = place_order(BinanceLikeExchange(), "SOL/ETH", "buy", 0.205, dry_run=False)

    assert order["symbol"] == "SOL/ETH"
    assert order["side"] == "buy"


def test_trade_line_names_the_asset():
    order = place_order(HyperliquidLikeExchange(), "HYPE/USDC", "sell", 23.33, dry_run=False)

    line = pf._format_trade_line(order)

    assert "None" not in line
    assert "HYPE/USDC" in line
    assert "id: 525666199143" in line


def test_persisted_trade_carries_symbol_and_price(monkeypatch):
    """An empty symbol here is what left HYPE fills unattributable in the DB."""
    recorded = []
    monkeypatch.setattr(pf, "get_latest_signal_id", lambda: 7)
    monkeypatch.setattr(pf, "record_trade", lambda **kwargs: recorded.append(kwargs))

    order = place_order(HyperliquidLikeExchange(), "HYPE/USDC", "sell", 23.33, dry_run=False)
    p = pf.Portfolio.__new__(pf.Portfolio)
    p._persist_trades([order], {"HYPE": 42.5})

    assert recorded[0]["symbol"] == "HYPE/USDC"
    assert recorded[0]["side"] == "sell"
    assert recorded[0]["price"] == 42.5
    assert recorded[0]["order_id"] == "525666199143"
