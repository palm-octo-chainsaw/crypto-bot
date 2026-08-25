"""Tests for the handler paths that answer without touching an exchange."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from utils import command_handlers as ch


@pytest.fixture
def fake_application(fake_context):
    return SimpleNamespace(bot=fake_context.bot)


@pytest.mark.asyncio
async def test_post_init_announces_the_bot_is_up(monkeypatch, fake_application):
    monkeypatch.setattr(ch, "CHAT_ID", "42")

    await ch.post_init(fake_application)

    assert any("Bot online" in msg for msg in fake_application.bot.sent)
    assert fake_application.bot.commands, "command menu should be registered on startup"


@pytest.mark.asyncio
async def test_post_stop_announces_shutdown(monkeypatch, fake_application):
    monkeypatch.setattr(ch, "CHAT_ID", "42")

    await ch.post_stop(fake_application)

    assert any("Bot stopped" in msg for msg in fake_application.bot.sent)


@pytest.mark.asyncio
async def test_set_target_without_arguments_explains_usage(fake_update, fake_context):
    await ch.set_target(fake_update, fake_context)

    assert fake_update.message.replies == ["⚠️ Usage: /set_target SYMBOL PERCENT"]


@pytest.mark.asyncio
async def test_rebalance_live_warns_before_trading(monkeypatch, fake_update, fake_context):
    fake_context.args = ["live"]
    monkeypatch.setattr(ch.portfolio, "execute_rebalance", lambda dry_run: "done")

    await ch.rebalance(fake_update, fake_context)

    assert "LIVE MODE" in fake_update.message.replies[0]
    assert "done" in fake_update.message.replies[1]


@pytest.mark.asyncio
async def test_fetch_signal_refuses_while_credentials_are_flagged(monkeypatch, fake_update, fake_context):
    monkeypatch.setattr(ch, "_credentials_invalid", True)

    await ch.fetch_signal(fake_update, fake_context)

    assert "credentials are invalid" in fake_update.message.replies[0]


@pytest.mark.asyncio
async def test_fetch_signal_reports_remaining_cooldown(monkeypatch, fake_update, fake_context):
    until = datetime.now(timezone.utc) + timedelta(minutes=25)
    monkeypatch.setattr(ch, "_rate_limit_until", until)

    await ch.fetch_signal(fake_update, fake_context)

    assert "cooldown active — 24 min remaining" in fake_update.message.replies[0]


@pytest.mark.asyncio
async def test_fetch_signal_reports_an_empty_scrape(monkeypatch, fake_update, fake_context):
    async def empty_scrape():
        return {}, None
    monkeypatch.setattr(ch, "scrape_signal", empty_scrape)

    await ch.fetch_signal(fake_update, fake_context)

    assert fake_update.message.replies[-1] == "⚠️ No allocations found in signal."
