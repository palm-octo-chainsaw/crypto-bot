"""Tests for CoinGecko rate-limit Telegram alerting in poll_signal."""
from datetime import timedelta

import pytest

from data.prices import PriceRateLimitError
from utils import command_handlers as ch


@pytest.fixture
def stub_scrape(monkeypatch):
    async def fake_scrape():
        return {"BTC": 100.0}, "2026-05-06 12:00"
    monkeypatch.setattr(ch, "scrape_signal", fake_scrape)
    monkeypatch.setattr(ch, "get_latest_message_timestamp", lambda: "2026-05-06 11:00")
    monkeypatch.setattr(ch, "get_latest_allocations", lambda: {"BTC": 100.0})
    monkeypatch.setattr(ch, "record_signal", lambda *a, **k: 1)
    monkeypatch.setattr(ch, "_apply_allocations", lambda allocs: None)


@pytest.mark.asyncio
async def test_poll_signal_alerts_on_price_rate_limit(monkeypatch, fake_context, stub_scrape):
    monkeypatch.setattr(ch, "_price_rate_limit_alerted_at", None)

    def raising_listener():
        raise PriceRateLimitError("429")
    monkeypatch.setattr(ch.portfolio, "listener", raising_listener)

    await ch.poll_signal(fake_context)

    assert any("CoinGecko rate-limited" in msg for msg in fake_context.bot.sent)
    assert ch._last_poll_status == "price API rate-limited"


@pytest.mark.asyncio
async def test_poll_signal_alert_throttled(monkeypatch, fake_context, stub_scrape):
    monkeypatch.setattr(ch, "_price_rate_limit_alerted_at", None)

    def raising_listener():
        raise PriceRateLimitError("429")
    monkeypatch.setattr(ch.portfolio, "listener", raising_listener)

    await ch.poll_signal(fake_context)
    await ch.poll_signal(fake_context)

    coingecko_alerts = [m for m in fake_context.bot.sent if "CoinGecko rate-limited" in m]
    assert len(coingecko_alerts) == 1


@pytest.mark.asyncio
async def test_poll_signal_alert_fires_again_after_cooldown(monkeypatch, fake_context, stub_scrape):
    from datetime import datetime, timezone
    past = datetime.now(timezone.utc) - ch.PRICE_RATE_LIMIT_ALERT_COOLDOWN - timedelta(minutes=1)
    monkeypatch.setattr(ch, "_price_rate_limit_alerted_at", past)

    def raising_listener():
        raise PriceRateLimitError("429")
    monkeypatch.setattr(ch.portfolio, "listener", raising_listener)

    await ch.poll_signal(fake_context)

    assert any("CoinGecko rate-limited" in m for m in fake_context.bot.sent)


@pytest.mark.asyncio
async def test_poll_signal_alerts_when_execute_rebalance_rate_limited(monkeypatch, fake_context, stub_scrape):
    """If listener succeeds but execute_rebalance hits 429, still alert."""
    monkeypatch.setattr(ch, "_price_rate_limit_alerted_at", None)

    def ok_listener():
        ch.portfolio.send_rebalance = True
        return "drift summary"
    monkeypatch.setattr(ch.portfolio, "listener", ok_listener)

    def raising_rebalance(dry_run=False):
        raise PriceRateLimitError("429")
    monkeypatch.setattr(ch.portfolio, "execute_rebalance", raising_rebalance)

    await ch.poll_signal(fake_context)

    assert any("CoinGecko rate-limited" in m for m in fake_context.bot.sent)
    assert ch._last_poll_status == "price API rate-limited"
