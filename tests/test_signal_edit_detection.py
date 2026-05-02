"""Tests for signal-edit detection: same timestamp + different allocations should re-apply."""
import pytest

from utils import command_handlers as ch


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
    assert ch._allocations_match({"BTC": 100.0}, {"BTC": 100.0, "ETH": 0.0})


def test_allocations_differ_when_new_nonzero_key_appears():
    assert not ch._allocations_match({"BTC": 100.0}, {"BTC": 50.0, "ETH": 50.0})


def test_allocations_match_none_handling():
    assert ch._allocations_match(None, None)
    assert not ch._allocations_match(None, {"BTC": 100.0})
    assert not ch._allocations_match({"BTC": 100.0}, None)


@pytest.mark.asyncio
async def test_poll_signal_reapplies_on_edited_signal(monkeypatch, fake_context):
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
    monkeypatch.setattr(ch, "record_signal", lambda allocs, message_timestamp=None: recorded.update(allocs=allocs, ts=message_timestamp) or 1)

    applied = {}
    monkeypatch.setattr(ch, "_apply_allocations", lambda allocs: applied.update(allocs=allocs))

    def fake_listener():
        ch.portfolio.send_rebalance = True
        return "drift summary"
    monkeypatch.setattr(ch.portfolio, "listener", fake_listener)

    rebalanced = {"called": False}
    monkeypatch.setattr(ch.portfolio, "execute_rebalance", lambda dry_run=False: rebalanced.update(called=True) or "ok")

    await ch.poll_signal(fake_context)

    assert recorded.get("allocs") == new_allocs
    assert applied.get("allocs") == new_allocs
    assert rebalanced["called"] is True


@pytest.mark.asyncio
async def test_poll_signal_skips_when_unchanged(monkeypatch, fake_context):
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

    await ch.poll_signal(fake_context)

    assert "unchanged" in ch._last_poll_status


@pytest.mark.asyncio
async def test_poll_signal_skips_rebalance_when_within_drift(monkeypatch, fake_context):
    """New signal applied but portfolio already within 3% drift → no rebalance."""
    new_allocs = {"BTC": 51.0, "ETH": 33.0, "USDC": 16.0}
    timestamp = "2026-04-30 00:25"

    async def fake_scrape():
        return new_allocs, timestamp
    monkeypatch.setattr(ch, "scrape_signal", fake_scrape)
    monkeypatch.setattr(ch, "get_latest_message_timestamp", lambda: "2026-04-30 00:10")
    monkeypatch.setattr(ch, "get_latest_allocations", lambda: {"BTC": 50.0, "ETH": 33.3, "USDC": 16.7})
    monkeypatch.setattr(ch, "record_signal", lambda allocs, message_timestamp=None: 1)
    monkeypatch.setattr(ch, "_apply_allocations", lambda allocs: None)

    def fake_listener():
        ch.portfolio.send_rebalance = False
        return "within threshold"
    monkeypatch.setattr(ch.portfolio, "listener", fake_listener)

    def boom(*a, **kw):
        raise AssertionError("execute_rebalance should not run when drift is within threshold")
    monkeypatch.setattr(ch.portfolio, "execute_rebalance", boom)

    await ch.poll_signal(fake_context)

    assert "within drift threshold" in ch._last_poll_status
