"""Tests for signal-edit detection: same timestamp + different allocations should re-apply."""
import pytest

from utils import command_handlers as ch


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append(text)


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr(ch, "_credentials_invalid", False)
    monkeypatch.setattr(ch, "_rate_limit_until", None)
    monkeypatch.setattr(ch, "_scrape_failure_count", 0)
    monkeypatch.setattr(ch, "_poll_failure_count", 0)
    monkeypatch.setattr(ch, "_poll_success_count", 0)
    monkeypatch.setattr(ch, "_last_poll_time", None)
    monkeypatch.setattr(ch, "_last_poll_status", "")
    monkeypatch.setattr(ch, "CHAT_ID", "123")


def test_allocations_match_identical():
    assert ch._allocations_match({"BTC": 50.0, "ETH": 50.0}, {"BTC": 50.0, "ETH": 50.0})


def test_allocations_match_within_tolerance():
    assert ch._allocations_match({"BTC": 50.0}, {"BTC": 50.005})


def test_allocations_differ_when_value_changes():
    assert not ch._allocations_match(
        {"BTC": 50.0, "ETH": 33.3, "USDC": 16.7},
        {"BTC": 66.6, "ETH": 33.3, "USDC": 0.0},
    )


def test_allocations_match_treats_missing_key_as_zero():
    # Adding/removing an explicit zero key is semantically the same allocation.
    assert ch._allocations_match({"BTC": 100.0}, {"BTC": 100.0, "ETH": 0.0})


def test_allocations_differ_when_new_nonzero_key_appears():
    assert not ch._allocations_match({"BTC": 100.0}, {"BTC": 50.0, "ETH": 50.0})


def test_allocations_match_none_handling():
    assert ch._allocations_match(None, None)
    assert not ch._allocations_match(None, {"BTC": 100.0})
    assert not ch._allocations_match({"BTC": 100.0}, None)


@pytest.mark.asyncio
async def test_poll_signal_reapplies_on_edited_signal(monkeypatch):
    """Same timestamp + changed allocations → treat as new signal."""
    new_allocs = {"BTC": 66.6, "ETH": 33.3, "USDC": 0.0}
    old_allocs = {"BTC": 50.0, "ETH": 33.3, "USDC": 16.7}
    timestamp = "2026-04-30 00:10"

    async def fake_scrape():
        return new_allocs, timestamp
    monkeypatch.setattr(ch, "scrape_signal", fake_scrape)
    monkeypatch.setattr(ch, "get_latest_message_timestamp", lambda: timestamp)
    monkeypatch.setattr(ch, "get_latest_allocations", lambda: old_allocs)

    recorded = {}
    def fake_record(allocs, message_timestamp=None):
        recorded["allocs"] = allocs
        recorded["ts"] = message_timestamp
        return 1
    monkeypatch.setattr(ch, "record_signal", fake_record)

    applied = {}
    def fake_apply(allocs):
        applied["allocs"] = allocs
    monkeypatch.setattr(ch, "_apply_allocations", fake_apply)

    rebalanced = {"called": False}
    def fake_rebalance(dry_run=False):
        rebalanced["called"] = True
        return "ok"
    monkeypatch.setattr(ch.portfolio, "execute_rebalance", fake_rebalance)

    ctx = FakeContext()
    await ch.poll_signal(ctx)

    assert recorded.get("allocs") == new_allocs
    assert applied.get("allocs") == new_allocs
    assert rebalanced["called"] is True


@pytest.mark.asyncio
async def test_poll_signal_skips_when_unchanged(monkeypatch):
    """Same timestamp + same allocations → no re-apply."""
    allocs = {"BTC": 50.0, "ETH": 33.3, "USDC": 16.7}
    timestamp = "2026-04-30 00:10"

    async def fake_scrape():
        return allocs, timestamp
    monkeypatch.setattr(ch, "scrape_signal", fake_scrape)
    monkeypatch.setattr(ch, "get_latest_message_timestamp", lambda: timestamp)
    monkeypatch.setattr(ch, "get_latest_allocations", lambda: dict(allocs))

    def boom(*a, **kw):
        raise AssertionError("should not be called when allocations unchanged")
    monkeypatch.setattr(ch, "record_signal", boom)
    monkeypatch.setattr(ch, "_apply_allocations", boom)
    monkeypatch.setattr(ch.portfolio, "execute_rebalance", boom)

    ctx = FakeContext()
    await ch.poll_signal(ctx)

    assert "unchanged" in ch._last_poll_status
