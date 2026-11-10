"""The whole book on Hyperliquid: longs on spot or a 1x perp, read and traded there alone."""
from unittest.mock import MagicMock

import pytest

import constants
import data.balance as balance_module
import data.hyperliquid_book as book
import data.longs as longs_module
import data.prices as prices_module
import portfolio as pf
from data.balance import Balance
from data.hyperliquid_account import holdings
from data.hyperliquid_book import execute_legs
from data.longs import plan_longs, route


def _market(mid, min_cost=10.0):
    return {"info": {"midPx": str(mid), "markPx": str(mid)}, "limits": {"cost": {"min": min_cost}}}


class FakeHyperliquid:
    walletAddress = "0xabc"

    def __init__(self, positions=None, spot=None, free_usdc=10_000.0, mode="unifiedAccount"):
        self.markets = {
            "BTC/USDC": _market(100_000.0),
            "BTC/USDC:USDC": _market(100_000.0),
            "SUI/USDC:USDC": _market(2.0),
            "SOL/USDC:USDC": _market(100.0),
            "KSHIB/USDC:USDC": _market(0.01),
        }
        self.positions = positions or []
        self.spot = spot or {}
        self.free_usdc = free_usdc
        self.mode = mode
        self.orders = []
        self.leverage = []

    def fetch_positions(self):
        return self.positions

    def public_post_info(self, request):
        balances = [{"coin": "USDC", "total": str(self.free_usdc + 500.0), "hold": "500.0"}]
        balances += [{"coin": token, "total": str(total), "hold": "0.0"} for token, total in self.spot.items()]
        return {
            "userAbstraction": self.mode,
            "clearinghouseState": {"withdrawable": "0.0"},
            "spotClearinghouseState": {"balances": balances},
        }[request["type"]]

    def amount_to_precision(self, symbol, amount):
        return f"{float(amount):.6f}"

    def set_leverage(self, leverage, symbol, params):
        self.leverage.append((symbol, leverage, params))

    def milliseconds(self):
        return 1_000

    def create_order(self, symbol, type, side, amount, price, params):
        self.orders.append((symbol, side, amount, params))
        return {"id": f"o{len(self.orders)}", "status": "closed", "filled": amount,
                "average": price, "fee": {"cost": 0.01, "currency": "USDC"}}


def _position(symbol, side, contracts):
    return {"symbol": symbol, "side": side, "contracts": contracts}


@pytest.fixture(autouse=True)
def low_minimums(monkeypatch):
    monkeypatch.setattr(book, "SHORT_LEVERAGE", 1)
    monkeypatch.setattr(book, "MIN_TRADE_USD", 1.0)
    monkeypatch.setattr(longs_module, "MIN_TRADE_USD", 1.0)


PRICES = {"BTC": 100_000.0, "SUI": 2.0, "SHIB": 0.00001, "XMR": 300.0}


# --- routing and planning --------------------------------------------------

def test_deep_spot_coins_go_to_spot_and_the_rest_to_a_perp():
    ex = FakeHyperliquid()
    assert route(ex, "BTC") == ("BTC/USDC", False)
    assert route(ex, "SUI") == ("SUI/USDC:USDC", True)
    # SOL is a spot coin, but this exchange lists no SOL spot pair.
    assert route(ex, "SOL") == ("SOL/USDC:USDC", True)
    assert route(ex, "XMR") is None


def test_buys_are_sized_in_usd_on_their_route():
    exits, entries, skipped = plan_longs(FakeHyperliquid(), {}, {"BTC": 0.01, "SHIB": 10_000_000.0},
                                         PRICES, set())

    assert exits == []
    assert skipped == []
    btc, shib = entries
    assert (btc.symbol, btc.perp, btc.usd) == ("BTC/USDC", False, pytest.approx(1_000.0))
    # $100 of SHIB is 10,000 kSHIB contracts at $0.01.
    assert (shib.symbol, shib.perp, shib.contracts) == ("KSHIB/USDC:USDC", True, pytest.approx(10_000.0))
    assert not shib.reduce_only


def test_sells_are_capped_at_what_the_market_holds():
    ex = FakeHyperliquid(spot={"UBTC": 0.005}, positions=[_position("SUI/USDC:USDC", "long", 50.0)])

    exits, _, _ = plan_longs(ex, {"BTC": 0.01, "SUI": 20.0}, {}, PRICES, set())

    btc, sui = exits
    assert (btc.symbol, btc.contracts, btc.reduce_only) == ("BTC/USDC", pytest.approx(0.005), False)
    assert (sui.symbol, sui.contracts, sui.reduce_only) == ("SUI/USDC:USDC", pytest.approx(20.0), True)


def test_a_dropped_perp_long_is_closed_whole_even_under_the_minimum():
    ex = FakeHyperliquid(positions=[_position("SUI/USDC:USDC", "long", 2.0)])  # $4

    exits, _, skipped = plan_longs(ex, {"SUI": 1.9}, {}, PRICES, {"SUI"})

    assert [(leg.symbol, leg.contracts) for leg in exits] == [("SUI/USDC:USDC", 2.0)]
    assert skipped == []


def test_a_spot_remnant_under_the_minimum_is_dust(monkeypatch):
    monkeypatch.setattr(longs_module, "MIN_TRADE_USD", 10.0)
    ex = FakeHyperliquid(spot={"UBTC": 0.00005})  # $5

    exits, _, skipped = plan_longs(ex, {"BTC": 0.00005}, {}, PRICES, {"BTC"})

    assert exits == []
    assert skipped[0]["dust"]
    assert skipped[0]["symbol"] == "BTC/USDC"


def test_a_coin_hyperliquid_does_not_list_is_skipped():
    _, entries, skipped = plan_longs(FakeHyperliquid(), {}, {"XMR": 1.0}, PRICES, set())

    assert entries == []
    assert skipped == [{"symbol": "XMR", "side": "buy", "amount": 1.0, "skipped": True}]


# --- execution -------------------------------------------------------------

def test_exits_go_first_and_only_perp_entries_set_leverage():
    ex = FakeHyperliquid(spot={"UBTC": 0.01})
    exits, entries, _ = plan_longs(ex, {"BTC": 0.01}, {"SUI": 100.0}, PRICES, set())

    execute_legs(ex, exits, entries, dry_run=False)

    assert [(o[0], o[1], o[3]) for o in ex.orders] == [
        ("BTC/USDC", "sell", {}),
        ("SUI/USDC:USDC", "buy", {}),
    ]
    assert ex.leverage == [("SUI/USDC:USDC", 1, {"marginMode": "cross"})]


def test_spot_buys_perp_longs_and_shorts_share_the_free_usdc():
    ex = FakeHyperliquid(free_usdc=303.0)  # $300 after the 1% headroom
    _, entries, _ = plan_longs(ex, {}, {"BTC": 0.002, "SUI": 100.0}, PRICES, set())  # $200 + $200
    short = book.Leg("SOL/USDC:USDC", "sell", 2.0, 100.0, perp=True, short=True)    # $200

    results, unfunded = execute_legs(ex, [], entries + [short], dry_run=False)

    placed = {o[0]: o[2] for o in ex.orders}
    assert placed["BTC/USDC"] == pytest.approx(0.001, rel=1e-3)
    assert placed["SUI/USDC:USDC"] == pytest.approx(50.0, rel=1e-3)
    assert placed["SOL/USDC:USDC"] == pytest.approx(1.0, rel=1e-3)
    assert unfunded == pytest.approx(300.0, rel=1e-3)
    assert [r["short"] for r in results] == [False, False, True]


def test_dry_run_counts_the_usdc_a_spot_sell_would_free():
    ex = FakeHyperliquid(spot={"UBTC": 0.01}, free_usdc=0.0)
    exits, entries, _ = plan_longs(ex, {"BTC": 0.01}, {"SUI": 100.0}, PRICES, set())

    results, unfunded = execute_legs(ex, exits, entries, dry_run=True)

    assert ex.orders == []
    assert unfunded == 0.0
    assert [r["dry_run"] for r in results] == [True, True]


# --- balances and prices ---------------------------------------------------

def test_holdings_count_spot_tokens_and_long_perps_and_leave_shorts_in_usdc():
    spot = {"balances": [{"coin": "USDC", "total": "1000.0", "hold": "300.0"},
                         {"coin": "UBTC", "total": "0.01", "hold": "0.0"},
                         {"coin": "PURR", "total": "50.0", "hold": "0.0"}]}
    perp = {"assetPositions": [
        {"position": {"coin": "SUI", "szi": "100.0", "positionValue": "200.0"}},
        {"position": {"coin": "kSHIB", "szi": "5000.0", "positionValue": "50.0"}},
        {"position": {"coin": "ETH", "szi": "-0.05", "positionValue": "100.0"}},
    ]}

    held = holdings("unifiedAccount", perp, spot)

    assert held == pytest.approx({"BTC": 0.01, "SUI": 100.0, "SHIB": 5_000_000.0, "USDC": 750.0})


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture
def on_hyperliquid(monkeypatch):
    monkeypatch.setattr(constants, "EXECUTION_VENUE", "hyperliquid")


def test_only_hyperliquid_is_read_when_the_book_lives_there(monkeypatch, on_hyperliquid):
    monkeypatch.setattr(balance_module, "HYPERLIQUID_ACCOUNT_ADDRESS", "0xabc")
    monkeypatch.setattr(balance_module.requests, "post", lambda url, json, timeout: FakeResponse({
        "userAbstraction": "unifiedAccount",
        "clearinghouseState": {"assetPositions": [
            {"position": {"coin": "SUI", "szi": "10.0", "positionValue": "20.0"}}]},
        "spotClearinghouseState": {"balances": [{"coin": "USDC", "total": "500.0", "hold": "20.0"},
                                                {"coin": "UBTC", "total": "0.002", "hold": "0.0"}]},
    }[json["type"]]))
    b = Balance.__new__(Balance)
    b._degraded = set()
    b._extra_symbols = frozenset({"XMR"})
    b.binance_client = b.kraken_client = None

    b.refresh_binance_balances()
    venues = b.get_venue_balances()

    assert set(venues) == {"hyperliquid"}
    held = venues["hyperliquid"]
    assert (held["BTC"], held["SUI"], held["USDC"], held["XMR"]) == (0.002, 10.0, 480.0, 0.0)
    assert set(held) == set(Balance.TRACKED_SYMBOLS) | {"XMR"}
    assert b.degraded == set(), "Binance and Kraken are not read, so cannot be degraded"


def test_a_failed_hyperliquid_read_is_degraded_not_empty(monkeypatch, on_hyperliquid):
    monkeypatch.setattr(balance_module, "HYPERLIQUID_ACCOUNT_ADDRESS", "0xabc")

    def down(url, json, timeout):
        raise RuntimeError("down")
    monkeypatch.setattr(balance_module.requests, "post", down)
    b = Balance.__new__(Balance)
    b._degraded = set()
    b._extra_symbols = frozenset()

    b.get_venue_balances()

    assert b.degraded == {"hyperliquid"}


def test_signal_tokens_are_tracked_by_their_hyperliquid_perp(monkeypatch, on_hyperliquid):
    monkeypatch.setattr(balance_module, "hyperliquid_lists", lambda symbol: symbol == "XMR")
    monkeypatch.setattr(balance_module, "binance_lists_spot_pair", MagicMock())
    b = Balance.__new__(Balance)
    b._degraded = set()
    b._extra_symbols = frozenset()
    b._listed = {}

    b.track_symbols({"XMR": 5.0, "FOMC": 1.0})

    assert b._extra_symbols == {"XMR"}
    balance_module.binance_lists_spot_pair.assert_not_called()


def test_prices_come_from_hyperliquid_mids(monkeypatch, on_hyperliquid):
    monkeypatch.setattr(prices_module.requests, "post", lambda url, json, timeout: FakeResponse(
        {"BTC": "100000.5", "kSHIB": "0.012", "@142": "100001.0"}))

    assert prices_module.fetch_prices(["BTC", "SHIB", "USDC"]) == pytest.approx(
        {"BTC": 100_000.5, "SHIB": 0.000012, "USDC": 1.0})
    with pytest.raises(prices_module.PriceFetchError, match="XMR"):
        prices_module.fetch_prices(["XMR"])


# --- portfolio wiring ------------------------------------------------------

@pytest.fixture
def hl_portfolio(monkeypatch, on_hyperliquid):
    monkeypatch.setattr(constants, "SHORTS_ENABLED", True)
    monkeypatch.setattr(pf, "HYPERLIQUID_ACCOUNT_ADDRESS", "0xabc")
    monkeypatch.setattr(pf, "HYPERLIQUID_PRIVATE_KEY", "0xkey")
    monkeypatch.setattr(pf, "BINANCE_API_KEY", "")
    monkeypatch.setattr(pf, "MANUAL_ASSETS", {"SUI"})
    monkeypatch.setattr(pf, "get_latest_signal_id", lambda: 1)
    monkeypatch.setattr(pf, "get_latest_allocations",
                        lambda: {"BTC": 20.0, "SUI": 20.0, "USDC": 50.0, "SOL": -10.0})
    monkeypatch.setattr(pf, "create_binance", MagicMock(side_effect=AssertionError("Binance used")))
    recorded = []
    monkeypatch.setattr(pf, "record_trade", lambda **kw: recorded.append(kw))

    p = pf.Portfolio.__new__(pf.Portfolio)
    p.holdings = {"BTC": 0.0, "SUI": 0.0, "USDC": 10_000.0}
    p.targets = {"BTC": 20.0, "SUI": 20.0, "USDC": 60.0}
    p.venues = {"hyperliquid": dict(p.holdings)}
    p.balance = MagicMock()
    p.balance.degraded = set()
    p.recorded = recorded
    monkeypatch.setattr(p, "update_portfolio", lambda: None)
    prices = {"BTC": 100_000.0, "SUI": 2.0, "USDC": 1.0}
    monkeypatch.setattr(p, "fetch_live_data", lambda: (
        prices, {s: a * prices[s] for s, a in p.holdings.items()}, 10_000.0))
    monkeypatch.setattr(pf, "REBALANCE_RESERVE_PCT", 0.0)
    return p


def test_rebalance_trades_longs_and_shorts_on_hyperliquid(hl_portfolio, monkeypatch):
    ex = FakeHyperliquid()
    monkeypatch.setattr(pf, "create_hyperliquid", lambda *a: ex)

    out = hl_portfolio.execute_rebalance(dry_run=False)

    assert [(o[0], o[1]) for o in ex.orders] == [
        ("BTC/USDC", "buy"), ("SUI/USDC:USDC", "buy"), ("SOL/USDC:USDC", "sell")]
    assert "✅ BUY `0.02` BTC/USDC" in out
    assert "✅ BUY `1000.0` SUI/USDC:USDC" in out, "SUI is manual on Binance, a perp here"
    assert "✅ SHORT `10.0` SOL/USDC:USDC" in out
    assert len(hl_portfolio.recorded) == 3


def test_a_non_unified_account_trades_nothing(hl_portfolio, monkeypatch):
    ex = FakeHyperliquid(mode="default")
    monkeypatch.setattr(pf, "create_hyperliquid", lambda *a: ex)

    out = hl_portfolio.execute_rebalance(dry_run=False)

    assert ex.orders == []
    assert "Hyperliquid account is not Unified" in out


def test_unfunded_buys_ask_for_a_deposit(hl_portfolio, monkeypatch):
    monkeypatch.setattr(pf, "create_hyperliquid", lambda *a: FakeHyperliquid(free_usdc=0.0))

    out = hl_portfolio.execute_rebalance(dry_run=True)

    assert "🚧 Buys and shorts — not all opened, Hyperliquid is $5000.00 USDC short (deposit USDC there)" in out


def test_missing_hyperliquid_credentials_refuse_to_trade(hl_portfolio, monkeypatch):
    monkeypatch.setattr(pf, "HYPERLIQUID_PRIVATE_KEY", "")

    assert hl_portfolio.execute_rebalance(dry_run=True) == (
        "⚠️ Hyperliquid credentials not set — cannot execute trades.")


def test_the_api_wallet_key_is_redacted_from_replies(monkeypatch):
    import utils.command_handlers as ch
    monkeypatch.setattr(constants, "HYPERLIQUID_PRIVATE_KEY", "0xdeadbeefcafe")

    assert "0xdeadbeefcafe" not in ch._redact("signing failed for 0xdeadbeefcafe")
