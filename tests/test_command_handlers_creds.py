"""Unit tests for the _credentials_invalid sticky flag in utils/command_handlers."""
import pytest

from data.scraper import TRWInvalidCredentialsError
from utils import command_handlers as ch


@pytest.mark.asyncio
async def test_poll_signal_sets_flag_on_invalid_credentials(monkeypatch, fake_context):
    async def fake_scrape():
        raise TRWInvalidCredentialsError("TRW login rejected: Invalid credentials.")
    monkeypatch.setattr(ch, "scrape_signal", fake_scrape)

    await ch.poll_signal(fake_context)

    assert ch._credentials_invalid is True
    assert ch._last_poll_status == "paused (invalid credentials)"
    assert any("rejected credentials" in msg for msg in fake_context.bot.sent)


@pytest.mark.asyncio
async def test_poll_signal_skips_when_flag_set(monkeypatch, fake_context):
    monkeypatch.setattr(ch, "_credentials_invalid", True)

    called = False
    async def fake_scrape():
        nonlocal called
        called = True
        return {}, None
    monkeypatch.setattr(ch, "scrape_signal", fake_scrape)

    await ch.poll_signal(fake_context)

    assert called is False, "scraper should not be invoked when credentials flagged invalid"
    assert ch._last_poll_status == "paused (invalid credentials)"
    assert fake_context.bot.sent == [], "no repeat notifications while paused"


@pytest.mark.asyncio
async def test_poll_signal_notifies_only_once(monkeypatch, fake_context):
    calls = {"n": 0}
    async def fake_scrape():
        calls["n"] += 1
        raise TRWInvalidCredentialsError("TRW login rejected: Invalid credentials.")
    monkeypatch.setattr(ch, "scrape_signal", fake_scrape)

    await ch.poll_signal(fake_context)
    await ch.poll_signal(fake_context)  # second poll should be short-circuited by flag

    assert calls["n"] == 1
    assert sum("rejected credentials" in m for m in fake_context.bot.sent) == 1
