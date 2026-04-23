"""Unit tests for the _credentials_invalid sticky flag in utils/command_handlers."""
import pytest

from data.scraper import TRWInvalidCredentialsError
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


@pytest.mark.asyncio
async def test_poll_signal_sets_flag_on_invalid_credentials(monkeypatch):
    async def fake_scrape():
        raise TRWInvalidCredentialsError("TRW login rejected: Invalid credentials.")
    monkeypatch.setattr(ch, "scrape_signal", fake_scrape)

    ctx = FakeContext()
    await ch.poll_signal(ctx)

    assert ch._credentials_invalid is True
    assert ch._last_poll_status == "paused (invalid credentials)"
    assert any("rejected credentials" in msg for msg in ctx.bot.sent)


@pytest.mark.asyncio
async def test_poll_signal_skips_when_flag_set(monkeypatch):
    monkeypatch.setattr(ch, "_credentials_invalid", True)

    called = False
    async def fake_scrape():
        nonlocal called
        called = True
        return {}, None
    monkeypatch.setattr(ch, "scrape_signal", fake_scrape)

    ctx = FakeContext()
    await ch.poll_signal(ctx)

    assert called is False, "scraper should not be invoked when credentials flagged invalid"
    assert ch._last_poll_status == "paused (invalid credentials)"
    assert ctx.bot.sent == [], "no repeat notifications while paused"


@pytest.mark.asyncio
async def test_poll_signal_notifies_only_once(monkeypatch):
    calls = {"n": 0}
    async def fake_scrape():
        calls["n"] += 1
        raise TRWInvalidCredentialsError("TRW login rejected: Invalid credentials.")
    monkeypatch.setattr(ch, "scrape_signal", fake_scrape)

    ctx = FakeContext()
    await ch.poll_signal(ctx)
    await ch.poll_signal(ctx)  # second poll should be short-circuited by flag

    assert calls["n"] == 1
    assert sum("rejected credentials" in m for m in ctx.bot.sent) == 1
