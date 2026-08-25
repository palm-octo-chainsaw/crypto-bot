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


@pytest.mark.asyncio
async def test_post_init_registers_commands_without_a_chat_id(monkeypatch, fake_application):
    """No CHAT_ID configured: still publish the command menu, just skip the announcement."""
    monkeypatch.setattr(ch, "CHAT_ID", None)

    await ch.post_init(fake_application)

    assert fake_application.bot.commands
    assert fake_application.bot.sent == []


@pytest.mark.asyncio
async def test_post_stop_is_silent_without_a_chat_id(monkeypatch, fake_application):
    monkeypatch.setattr(ch, "CHAT_ID", None)

    await ch.post_stop(fake_application)

    assert fake_application.bot.sent == []


@pytest.mark.asyncio
async def test_rebalance_dry_run_skips_the_live_warning(monkeypatch, fake_update, fake_context):
    fake_context.args = []
    monkeypatch.setattr(ch.portfolio, "execute_rebalance", lambda dry_run: f"dry_run={dry_run}")

    await ch.rebalance(fake_update, fake_context)

    assert len(fake_update.message.replies) == 1
    assert "dry_run=True" in fake_update.message.replies[0]


def test_signal_section_omits_a_missing_timestamp(monkeypatch):
    monkeypatch.setattr(ch, "get_latest_allocations", lambda: {"BTC": 100.0})
    monkeypatch.setattr(ch, "get_latest_message_timestamp", lambda: None)

    out = "\n".join(ch._format_signal_section())

    assert "BTC: 100.0%" in out
    assert "Posted:" not in out
