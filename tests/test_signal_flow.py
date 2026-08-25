"""Rate-limit, credential and failure paths of /fetch_signal and the poll job."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from utils import command_handlers as ch
from data.prices import PriceRateLimitError
from data.scraper import TRWInvalidCredentialsError, TRWRateLimitError


def _make_update():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def _replies(update):
    return [call.args[0] for call in update.message.reply_text.call_args_list]


def _scraper_raising(error):
    async def fake_scrape():
        raise error
    return fake_scrape


def _scraper_returning(allocations, signal_time):
    async def fake_scrape():
        return allocations, signal_time
    return fake_scrape


@pytest.fixture
def stub_portfolio(monkeypatch):
    fake = MagicMock()
    fake.targets = {"BTC": 50.0, "USDC": 50.0}
    fake.send_rebalance = True
    fake.listener.return_value = "check summary"
    fake.execute_rebalance.return_value = "rebalance result"
    monkeypatch.setattr(ch, "portfolio", fake)
    return fake


@pytest.fixture
def new_signal(monkeypatch):
    """A scrape that differs from what the database holds."""
    allocations = {"BTC": 70.0, "USDC": 30.0}
    monkeypatch.setattr(ch, "scrape_signal", _scraper_returning(allocations, "2026-04-30 10:00"))
    monkeypatch.setattr(ch, "get_latest_allocations", lambda: {"BTC": 50.0, "USDC": 50.0})
    monkeypatch.setattr(ch, "get_latest_message_timestamp", lambda: "2026-04-29 10:00")
    monkeypatch.setattr(ch, "record_signal", lambda allocs, message_timestamp=None: 1)
    monkeypatch.setattr(ch, "_apply_allocations", lambda allocs: None)
    return allocations


def test_set_rate_limit_cooldown_uses_retry_after_plus_a_minute(monkeypatch):
    now = datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)
    error = TRWRateLimitError("slow down", retry_after_minutes=12)

    duration = ch._set_rate_limit_cooldown(now, error)

    assert duration == timedelta(minutes=13)
    assert ch._rate_limit_until == now + timedelta(minutes=13)


def test_set_rate_limit_cooldown_falls_back_to_default(monkeypatch):
    now = datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)

    duration = ch._set_rate_limit_cooldown(now, TRWRateLimitError("slow down"))

    assert duration == ch.RATE_LIMIT_COOLDOWN


def test_cooldown_remaining_is_none_once_expired(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(ch, "_rate_limit_until", now - timedelta(minutes=1))
    assert ch._cooldown_remaining(now) is None


@pytest.mark.asyncio
async def test_fetch_signal_refuses_while_credentials_are_invalid(monkeypatch, fake_context):
    monkeypatch.setattr(ch, "_credentials_invalid", True)
    monkeypatch.setattr(ch, "scrape_signal", _scraper_raising(AssertionError("must not scrape")))
    update = _make_update()

    await ch.fetch_signal(update, fake_context)

    assert "credentials are invalid" in _replies(update)[0]


@pytest.mark.asyncio
async def test_fetch_signal_refuses_during_cooldown(monkeypatch, fake_context):
    monkeypatch.setattr(ch, "_rate_limit_until",
                        datetime.now(timezone.utc) + timedelta(minutes=30))
    monkeypatch.setattr(ch, "scrape_signal", _scraper_raising(AssertionError("must not scrape")))
    update = _make_update()

    await ch.fetch_signal(update, fake_context)

    assert "cooldown active" in _replies(update)[0]
    assert "29 min remaining" in _replies(update)[0]


@pytest.mark.asyncio
async def test_fetch_signal_starts_cooldown_when_rate_limited(monkeypatch, fake_context):
    monkeypatch.setattr(ch, "scrape_signal",
                        _scraper_raising(TRWRateLimitError("429", retry_after_minutes=9)))
    update = _make_update()

    await ch.fetch_signal(update, fake_context)

    assert "paused for 10 min" in _replies(update)[-1]
    assert ch._rate_limit_until is not None


@pytest.mark.asyncio
async def test_fetch_signal_pauses_on_invalid_credentials(monkeypatch, fake_context):
    monkeypatch.setattr(ch, "scrape_signal",
                        _scraper_raising(TRWInvalidCredentialsError("login rejected")))
    update = _make_update()

    await ch.fetch_signal(update, fake_context)

    assert "rejected credentials" in _replies(update)[-1]
    assert ch._credentials_invalid is True


@pytest.mark.asyncio
async def test_fetch_signal_reports_unexpected_scrape_error(monkeypatch, fake_context):
    monkeypatch.setattr(ch, "scrape_signal", _scraper_raising(RuntimeError("selenium died")))
    update = _make_update()

    await ch.fetch_signal(update, fake_context)

    assert "Error fetching signal" in _replies(update)[-1]


@pytest.mark.asyncio
async def test_fetch_signal_reports_empty_allocations(monkeypatch, fake_context):
    monkeypatch.setattr(ch, "scrape_signal", _scraper_returning({}, "2026-04-30 10:00"))
    update = _make_update()

    await ch.fetch_signal(update, fake_context)

    assert "No allocations found" in _replies(update)[-1]


@pytest.mark.asyncio
async def test_fetch_signal_reports_unchanged_signal(monkeypatch, fake_context):
    allocations = {"BTC": 50.0, "USDC": 50.0}
    monkeypatch.setattr(ch, "scrape_signal", _scraper_returning(allocations, "2026-04-30 10:00"))
    monkeypatch.setattr(ch, "get_latest_allocations", lambda: dict(allocations))
    monkeypatch.setattr(ch, "get_latest_message_timestamp", lambda: "2026-04-30 10:00")
    update = _make_update()

    await ch.fetch_signal(update, fake_context)

    assert "Signal unchanged" in _replies(update)[-1]
    assert "same timestamp" in _replies(update)[-1]


@pytest.mark.asyncio
async def test_fetch_signal_applies_new_signal(monkeypatch, fake_context, stub_portfolio, new_signal):
    applied = {}
    monkeypatch.setattr(ch, "_apply_allocations", lambda allocs: applied.update(allocs=allocs))
    recorded = {}
    monkeypatch.setattr(ch, "record_signal",
                        lambda allocs, message_timestamp=None: recorded.update(ts=message_timestamp))
    update = _make_update()

    await ch.fetch_signal(update, fake_context)

    assert applied["allocs"] == new_signal
    assert recorded["ts"] == "2026-04-30 10:00"
    out = _replies(update)[-1]
    assert "Targets updated" in out and "BTC: 70.0%" in out


@pytest.mark.asyncio
async def test_poll_signal_skips_without_chat_id(monkeypatch, fake_context):
    monkeypatch.setattr(ch, "CHAT_ID", None)
    monkeypatch.setattr(ch, "scrape_signal", _scraper_raising(AssertionError("must not scrape")))

    await ch.poll_signal(fake_context)

    assert ch._last_poll_status == "skipped (no CHAT_ID)"


@pytest.mark.asyncio
async def test_poll_signal_skips_while_credentials_are_invalid(monkeypatch, fake_context):
    monkeypatch.setattr(ch, "_credentials_invalid", True)
    monkeypatch.setattr(ch, "scrape_signal", _scraper_raising(AssertionError("must not scrape")))

    await ch.poll_signal(fake_context)

    assert ch._last_poll_status == "paused (invalid credentials)"


@pytest.mark.asyncio
async def test_poll_signal_skips_during_cooldown(monkeypatch, fake_context):
    monkeypatch.setattr(ch, "_rate_limit_until",
                        datetime.now(timezone.utc) + timedelta(minutes=20))
    monkeypatch.setattr(ch, "scrape_signal", _scraper_raising(AssertionError("must not scrape")))

    await ch.poll_signal(fake_context)

    assert "rate-limit cooldown" in ch._last_poll_status
    assert fake_context.bot.sent == []


@pytest.mark.asyncio
async def test_poll_signal_announces_rate_limit(monkeypatch, fake_context):
    monkeypatch.setattr(ch, "scrape_signal",
                        _scraper_raising(TRWRateLimitError("429", retry_after_minutes=19)))

    await ch.poll_signal(fake_context)

    assert "rate-limited" in ch._last_poll_status
    assert "pausing scrape for 20 min" in fake_context.bot.sent[0]


@pytest.mark.asyncio
async def test_poll_signal_announces_invalid_credentials(monkeypatch, fake_context):
    monkeypatch.setattr(ch, "scrape_signal",
                        _scraper_raising(TRWInvalidCredentialsError("login rejected")))

    await ch.poll_signal(fake_context)

    assert ch._credentials_invalid is True
    assert "rejected credentials" in fake_context.bot.sent[0]


@pytest.mark.asyncio
async def test_poll_signal_alerts_only_on_the_third_consecutive_failure(monkeypatch, fake_context):
    monkeypatch.setattr(ch, "scrape_signal", _scraper_raising(RuntimeError("selenium died")))

    await ch.poll_signal(fake_context)
    await ch.poll_signal(fake_context)
    assert fake_context.bot.sent == []

    await ch.poll_signal(fake_context)

    assert ch._scrape_failure_count == ch.SCRAPE_FAILURE_ALERT_THRESHOLD
    assert "scrape failing" in fake_context.bot.sent[0]
    assert ch._last_poll_status == "scrape failed: RuntimeError"


@pytest.mark.asyncio
async def test_poll_signal_announces_recovery_after_failures(monkeypatch, fake_context, stub_portfolio):
    monkeypatch.setattr(ch, "_scrape_failure_count", ch.SCRAPE_FAILURE_ALERT_THRESHOLD)
    monkeypatch.setattr(ch, "scrape_signal", _scraper_returning({}, None))

    await ch.poll_signal(fake_context)

    assert "recovered" in fake_context.bot.sent[0]
    assert ch._scrape_failure_count == 0
    assert ch._last_poll_status == "no allocations parsed"


@pytest.mark.asyncio
async def test_poll_signal_skips_rebalance_within_drift_threshold(
        monkeypatch, fake_context, stub_portfolio, new_signal):
    stub_portfolio.send_rebalance = False

    await ch.poll_signal(fake_context)

    stub_portfolio.execute_rebalance.assert_not_called()
    assert "within drift threshold" in ch._last_poll_status
    assert "within 3% drift" in fake_context.bot.sent[-1]


@pytest.mark.asyncio
async def test_poll_signal_alerts_when_prices_are_rate_limited(
        monkeypatch, fake_context, stub_portfolio, new_signal):
    monkeypatch.setattr(ch, "_price_rate_limit_alerted_at", None)
    stub_portfolio.listener.side_effect = PriceRateLimitError("coingecko 429")

    await ch.poll_signal(fake_context)

    assert ch._last_poll_status == "price API rate-limited"
    assert "CoinGecko rate-limited" in fake_context.bot.sent[-1]


@pytest.mark.asyncio
async def test_poll_signal_reports_failed_rebalance(
        monkeypatch, fake_context, stub_portfolio, new_signal):
    stub_portfolio.execute_rebalance.side_effect = RuntimeError("binance down")

    await ch.poll_signal(fake_context)

    assert ch._last_poll_status == "rebalance failed"
    assert "Auto-rebalance failed" in fake_context.bot.sent[-1]


@pytest.mark.asyncio
async def test_poll_signal_sends_rebalance_result(
        monkeypatch, fake_context, stub_portfolio, new_signal):
    await ch.poll_signal(fake_context)

    stub_portfolio.execute_rebalance.assert_called_once_with(dry_run=False)
    assert "rebalance result" in fake_context.bot.sent[-1]
    assert ch._last_poll_status == "new signal detected"
