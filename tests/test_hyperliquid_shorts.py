"""Signal shorts traded as Hyperliquid perps: sizing, ordering, margin and reporting."""
from unittest.mock import MagicMock

import pytest

import constants
import data.hyperliquid_book as book
import data.shorts as shorts_module
import data.trading as trading
import portfolio as pf
import utils.command_handlers as ch
from data.balance import Balance
from data.hyperliquid_book import execute_legs, perp_symbol
from data.shorts import plan_shorts


def _market(mid, min_cost=10.0):
    return {"info": {"midPx": str(mid), "markPx": str(mid)}, "limits": {"cost": {"min": min_cost}}}


class FakePerpExchange:
    def __init__(self, markets=None, positions=None, free_usdc=10_000.0):
        self.markets = markets or {
            "SOL/USDC:USDC": _market(100.0),
            "ETH/USDC:USDC": _market(2_000.0),
            "KSHIB/USDC:USDC": _market(0.01),
        }
        self.positions = positions or []
        self.free_usdc = free_usdc
        self.orders = []
        self.leverage = []
        self.trades = []

    def fetch_positions(self):
        return self.positions

    walletAddress = "0xabc"

    def public_post_info(self, request):
        """A unified account: spot USDC is the whole balance, `hold` what perps tie up."""
        return {
            "userAbstraction": "unifiedAccount",
            "clearinghouseState": {"withdrawable": "0.0"},
            "spotClearinghouseState": {"balances": [
                {"coin": "USDC", "total": str(self.free_usdc + 500.0), "hold": "500.0"}]},
        }[request["type"]]

    def amount_to_precision(self, symbol, amount):
        return f"{float(amount):.4f}"

    def set_leverage(self, leverage, symbol, params):
        self.leverage.append((symbol, leverage, params))

    def milliseconds(self):
        return 1_000

    def create_order(self, symbol, type, side, amount, price, params):
        self.orders.append((symbol, side, amount, price, params))
        return {"id": f"o{len(self.orders)}", "status": "closed", "filled": amount,
                "average": price, "fee": None}

    def fetch_my_trades(self, symbol, since=None):
        return self.trades


def _short(symbol, contracts):
    return {"symbol": symbol, "side": "short", "contracts": contracts}


def _legs(legs):
    return {leg.symbol: (leg.contracts, leg.price) for leg in legs}


def execute_shorts(exchange, shorts, book_usd, dry_run):
    covers, opens, skipped = plan_shorts(exchange, shorts, book_usd)
    results, unfunded = execute_legs(exchange, covers, opens, dry_run)
    return skipped + results, unfunded


@pytest.fixture(autouse=True)
def one_x(monkeypatch):
    monkeypatch.setattr(book, "SHORT_LEVERAGE", 1)
    monkeypatch.setattr(book, "MIN_TRADE_USD", 1.0)
    monkeypatch.setattr(shorts_module, "MIN_TRADE_USD", 1.0)


# --- symbols and planning --------------------------------------------------

def test_perp_symbol_finds_coins_listed_per_thousand():
    ex = FakePerpExchange()
    assert perp_symbol(ex, "SOL") == "SOL/USDC:USDC"
    assert perp_symbol(ex, "SHIB") == "KSHIB/USDC:USDC"
    assert perp_symbol(ex, "XMR") is None


def test_plan_sizes_each_short_against_the_whole_book():
    covers, opens, skipped = plan_shorts(FakePerpExchange(), {"SOL": 2.0, "SHIB": 1.0}, 10_000.0)
    opens = _legs(opens)

    assert covers == []
    assert skipped == []
    assert opens["SOL/USDC:USDC"] == pytest.approx((2.0, 100.0))       # $200 at $100
    assert opens["KSHIB/USDC:USDC"] == pytest.approx((10_000.0, 0.01))  # $100 at $0.01 per kSHIB


def test_plan_covers_a_coin_the_signal_dropped_in_full():
    ex = FakePerpExchange(positions=[_short("ETH/USDC:USDC", 0.003)])  # $6, under the $10 minimum

    covers, opens, _ = plan_shorts(ex, {"SOL": 2.0}, 10_000.0)

    assert _legs(covers) == {"ETH/USDC:USDC": (0.003, 2_000.0)}
    assert covers[0].reduce_only
    assert covers[0].short
    assert set(_legs(opens)) == {"SOL/USDC:USDC"}


def test_plan_trims_an_oversized_short_and_leaves_dust_alone():
    ex = FakePerpExchange(positions=[_short("SOL/USDC:USDC", 3.0), _short("ETH/USDC:USDC", 0.1)])

    # SOL: $300 held, $200 wanted → cover 1. ETH: $200 held, $205 wanted → $5 is dust.
    covers, opens, skipped = plan_shorts(ex, {"SOL": 2.0, "ETH": 2.05}, 10_000.0)

    assert _legs(covers) == {"SOL/USDC:USDC": pytest.approx((1.0, 100.0))}
    assert opens == []
    assert skipped[0]["dust"]
    assert skipped[0]["symbol"] == "ETH/USDC:USDC"


def test_plan_skips_a_coin_with_no_perp_and_ignores_longs():
    ex = FakePerpExchange(positions=[{"symbol": "SOL/USDC:USDC", "side": "long", "contracts": 5.0}])

    covers, opens, skipped = plan_shorts(ex, {"XMR": 1.0}, 10_000.0)

    assert covers == []
    assert opens == []
    assert skipped == [{"symbol": "XMR", "side": "sell", "amount": 0.0, "perp": True, "short": True,
                        "reduce_only": False, "skipped": True}]


# --- execution -------------------------------------------------------------

def test_covers_go_before_opens_and_are_reduce_only():
    ex = FakePerpExchange(positions=[_short("ETH/USDC:USDC", 0.1)])

    results, unfunded = execute_shorts(ex, {"SOL": 2.0}, 10_000.0, dry_run=False)

    assert [(o[0], o[1], o[4]) for o in ex.orders] == [
        ("ETH/USDC:USDC", "buy", {"reduceOnly": True}),
        ("SOL/USDC:USDC", "sell", {}),
    ]
    assert ex.leverage == [("SOL/USDC:USDC", 1, {"marginMode": "cross"})]
    assert unfunded == 0.0
    assert [r["reduce_only"] for r in results] == [True, False]
    assert all(r["perp"] for r in results)


def test_opens_scale_down_to_the_free_margin_and_report_the_rest():
    ex = FakePerpExchange(free_usdc=101.0)  # 1x: $100 of shorts after the slippage headroom

    results, unfunded = execute_shorts(ex, {"SOL": 2.0, "ETH": 2.0}, 10_000.0, dry_run=False)

    opened = sum(o[2] * o[3] for o in ex.orders)
    assert opened == pytest.approx(100.0 - 0.01, abs=0.5)
    assert unfunded == pytest.approx(400.0 - opened, abs=0.5)
    assert all(r["side"] == "sell" for r in results)


def test_no_margin_opens_nothing_and_reports_it_all_unfunded():
    ex = FakePerpExchange(free_usdc=0.0)

    results, unfunded = execute_shorts(ex, {"SOL": 2.0}, 10_000.0, dry_run=False)

    assert ex.orders == []
    assert results == []
    assert unfunded == pytest.approx(200.0)


def test_dry_run_places_nothing_and_sets_no_leverage():
    ex = FakePerpExchange(positions=[_short("ETH/USDC:USDC", 0.1)])

    results, _ = execute_shorts(ex, {"SOL": 2.0}, 10_000.0, dry_run=True)

    assert ex.orders == []
    assert ex.leverage == []
    assert [r["dry_run"] for r in results] == [True, True]


def test_dry_run_counts_the_margin_covers_would_release():
    ex = FakePerpExchange(positions=[_short("ETH/USDC:USDC", 0.2)], free_usdc=0.0)

    _, unfunded = execute_shorts(ex, {"SOL": 2.0}, 10_000.0, dry_run=True)

    assert unfunded == 0.0  # the $400 ETH cover funds the $200 SOL short


def test_a_short_whose_leverage_cannot_be_set_is_not_opened():
    ex = FakePerpExchange()
    ex.set_leverage = MagicMock(side_effect=RuntimeError("nope"))

    results, _ = execute_shorts(ex, {"SOL": 2.0}, 10_000.0, dry_run=False)

    assert ex.orders == []
    assert "leverage not set" in results[0]["error"]


def test_a_rejected_order_becomes_an_error_leg():
    ex = FakePerpExchange()
    ex.create_order = MagicMock(side_effect=RuntimeError("Order could not immediately match"))

    results, _ = execute_shorts(ex, {"SOL": 2.0}, 10_000.0, dry_run=False)

    assert results[0]["error"] == "Order could not immediately match"
    assert results[0]["perp"]


def test_perp_order_takes_its_fee_from_the_fills():
    ex = FakePerpExchange()
    ex.trades = [{"order": "o1", "fee": {"cost": 0.04, "currency": "USDC"}},
                 {"order": "o1", "fee": {"cost": 0.02, "currency": "USDC"}},
                 {"order": "other", "fee": {"cost": 9.0, "currency": "USDC"}}]

    order = trading.place_hyperliquid_order(ex, "SOL/USDC:USDC", "sell", 2.0, 100.0, False, dry_run=False)

    assert order["fee_amount"] == pytest.approx(0.06)
    assert order["fee_currency"] == "USDC"
    assert order["cost"] == pytest.approx(200.0)


# --- portfolio wiring ------------------------------------------------------

@pytest.fixture
def shorts_portfolio(monkeypatch):
    monkeypatch.setattr(constants, "SHORTS_ENABLED", True)
    monkeypatch.setattr(pf, "HYPERLIQUID_ACCOUNT_ADDRESS", "0xabc")
    monkeypatch.setattr(pf, "HYPERLIQUID_PRIVATE_KEY", "0xkey")
    monkeypatch.setattr(pf, "BINANCE_API_KEY", "key")
    monkeypatch.setattr(pf, "BINANCE_API_SECRET", "secret")
    monkeypatch.setattr(pf, "get_latest_signal_id", lambda: 1)
    monkeypatch.setattr(pf, "get_latest_allocations", lambda: {"USDC": 90.0, "SOL": -10.0})
    recorded = []
    monkeypatch.setattr(pf, "record_trade", lambda **kw: recorded.append(kw))

    p = pf.Portfolio.__new__(pf.Portfolio)
    p.holdings = {"USDC": 10_000.0}
    p.targets = {"USDC": 100.0}
    p.venues = {"binance": {"USDC": 9_000.0}, "hyperliquid": {"USDC": 1_000.0}}
    p.balance = MagicMock()
    p.balance.degraded = set()
    p.recorded = recorded
    monkeypatch.setattr(p, "update_portfolio", lambda: None)
    monkeypatch.setattr(p, "fetch_live_data", lambda: ({"USDC": 1.0}, {"USDC": 10_000.0}, 10_000.0))
    monkeypatch.setattr(pf, "REBALANCE_RESERVE_PCT", 0.0)
    return p


def test_rebalance_trades_shorts_with_no_spot_legs(shorts_portfolio, monkeypatch):
    ex = FakePerpExchange()
    monkeypatch.setattr(pf, "create_hyperliquid", lambda *a: ex)

    out = shorts_portfolio.execute_rebalance(dry_run=False)

    assert "Rebalance LIVE" in out
    assert "✅ SHORT `10.0` SOL/USDC:USDC" in out
    assert ex.orders[0][:3] == ("SOL/USDC:USDC", "sell", 10.0)
    row = shorts_portfolio.recorded[0]
    assert (row["symbol"], row["side"], row["status"]) == ("SOL/USDC:USDC", "sell", "filled")
    assert row["usd_value"] == pytest.approx(1_000.0)


def test_unfunded_shorts_name_where_the_usdc_sits(shorts_portfolio, monkeypatch):
    monkeypatch.setattr(pf, "create_hyperliquid", lambda *a: FakePerpExchange(free_usdc=0.0))

    out = shorts_portfolio.execute_rebalance(dry_run=False)

    assert "🚧 Shorts — $1000.00 not opened, Hyperliquid lacks the USDC (its USDC sits on binance)" in out
    assert shorts_portfolio.recorded == []


def test_unreadable_signal_leaves_the_shorts_alone(shorts_portfolio, monkeypatch):
    def broken():
        raise RuntimeError("db down")
    monkeypatch.setattr(pf, "get_latest_allocations", broken)
    connect = MagicMock()
    monkeypatch.setattr(pf, "create_hyperliquid", connect)

    out = shorts_portfolio.execute_rebalance(dry_run=False)

    connect.assert_not_called()
    assert "Could not read the latest signal — shorts left as they are" in out


def test_missing_credentials_leave_the_shorts_alone(shorts_portfolio, monkeypatch):
    monkeypatch.setattr(pf, "HYPERLIQUID_PRIVATE_KEY", "")
    connect = MagicMock()
    monkeypatch.setattr(pf, "create_hyperliquid", connect)

    out = shorts_portfolio.execute_rebalance(dry_run=True)

    connect.assert_not_called()
    assert "Hyperliquid credentials not set" in out


def test_balanced_shorts_report_nothing_to_trade(shorts_portfolio, monkeypatch):
    ex = FakePerpExchange(positions=[_short("SOL/USDC:USDC", 10.0)])
    monkeypatch.setattr(pf, "create_hyperliquid", lambda *a: ex)

    assert shorts_portfolio.execute_rebalance(dry_run=False) == "✅ Portfolio is balanced — no trades needed."


def test_shorts_off_never_touches_hyperliquid(shorts_portfolio, monkeypatch):
    monkeypatch.setattr(constants, "SHORTS_ENABLED", False)
    connect = MagicMock()
    monkeypatch.setattr(pf, "create_hyperliquid", connect)

    shorts_portfolio.execute_rebalance(dry_run=False)

    connect.assert_not_called()


def test_cover_and_skip_lines():
    assert pf._format_trade_line({"symbol": "SOL/USDC:USDC", "side": "buy", "amount": 1.0,
                                  "perp": True, "short": True, "dry_run": True}) == "📋 COVER `1.0` SOL/USDC:USDC"
    assert pf._format_trade_line({"symbol": "SUI/USDC:USDC", "side": "buy", "amount": 1.0,
                                  "perp": True, "dry_run": True}) == "📋 BUY `1.0` SUI/USDC:USDC"
    assert pf._format_trade_line({"symbol": "XMR", "side": "sell", "amount": 0.0, "perp": True,
                                  "short": True, "skipped": True}) == "⏭️ SKIP XMR short — no Hyperliquid perp, held in USDC"


# --- balances --------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _hl_balance(monkeypatch, address="0xabc", fail=False, mode="unifiedAccount"):
    import data.balance as balance_module
    monkeypatch.setattr(balance_module, "HYPERLIQUID_ACCOUNT_ADDRESS", address)

    def post(url, json, timeout):
        if fail:
            raise RuntimeError("down")
        return FakeResponse({
            "userAbstraction": mode,
            "clearinghouseState": {"marginSummary": {"accountValue": "1200.5"}, "withdrawable": "0.0"},
            "spotClearinghouseState": {"balances": [{"coin": "USDC", "total": "1250.5", "hold": "1200.5"},
                                                    {"coin": "HYPE", "total": "3.0", "hold": "0.0"}]},
        }[json["type"]])
    monkeypatch.setattr(balance_module.requests, "post", post)
    b = Balance.__new__(Balance)
    b._degraded = set()
    return b


def test_unified_account_usdc_is_the_spot_balance_alone(monkeypatch):
    """Its spot USDC already holds the perp equity; adding the perp side counts it twice."""
    b = _hl_balance(monkeypatch)
    assert b._hyperliquid_usdc() == pytest.approx(1250.5)
    assert b.degraded == set()


def test_separate_balances_add_perp_equity_to_spot_usdc(monkeypatch):
    b = _hl_balance(monkeypatch, mode="disabled")
    assert b._hyperliquid_usdc() == pytest.approx(2451.0)


def test_portfolio_margin_is_refused(monkeypatch):
    b = _hl_balance(monkeypatch, mode="portfolioMargin")
    assert b._hyperliquid_usdc() == 0.0
    assert b.degraded == {"hyperliquid"}


def test_free_margin_by_account_mode():
    from data.hyperliquid_account import free_margin
    perp = {"withdrawable": "40.0"}
    spot = {"balances": [{"coin": "USDC", "total": "100.0", "hold": "70.0"}]}
    assert free_margin("unifiedAccount", perp, spot) == pytest.approx(30.0)
    assert free_margin("default", perp, spot) == pytest.approx(40.0)


def test_hyperliquid_read_failure_marks_the_venue_degraded(monkeypatch):
    b = _hl_balance(monkeypatch, fail=True)
    assert b._hyperliquid_usdc() == 0.0
    assert b.degraded == {"hyperliquid"}


def test_hyperliquid_without_an_address_is_degraded(monkeypatch):
    b = _hl_balance(monkeypatch, address="")
    assert b._hyperliquid_usdc() == 0.0
    assert b.degraded == {"hyperliquid"}


# --- signal polling --------------------------------------------------------

def test_a_signal_that_only_moves_shorts_counts_as_changed(monkeypatch):
    monkeypatch.setattr(constants, "SHORTS_ENABLED", True)
    monkeypatch.setattr(ch, "get_latest_allocations", lambda: {"USDC": 90.0, "SOL": -10.0})

    assert ch._shorts_changed({"USDC": 90.0, "ETH": -10.0})
    assert not ch._shorts_changed({"USDC": 90.0, "SOL": -10.0})


def test_shorts_never_force_a_rebalance_when_disabled(monkeypatch):
    monkeypatch.setattr(constants, "SHORTS_ENABLED", False)
    monkeypatch.setattr(ch, "get_latest_allocations", lambda: {"USDC": 90.0, "SOL": -10.0})

    assert not ch._shorts_changed({"USDC": 90.0, "ETH": -10.0})
