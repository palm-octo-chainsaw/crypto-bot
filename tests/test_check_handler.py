"""Tests for /check handler error handling."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from utils import command_handlers as ch


@pytest.mark.asyncio
async def test_check_replies_with_listener_output(monkeypatch, fake_context):
    monkeypatch.setattr(ch.portfolio, "listener", lambda: "summary text")
    update = MagicMock()
    update.message.reply_text = AsyncMock()

    await ch.check(update, fake_context)

    update.message.reply_text.assert_awaited_once()
    args, kwargs = update.message.reply_text.call_args
    assert args[0] == "summary text"
    assert kwargs.get("parse_mode") == "Markdown"


@pytest.mark.asyncio
async def test_check_replies_with_generic_error_when_listener_raises(monkeypatch, fake_context):
    def boom():
        raise RuntimeError("price fetch failed")
    monkeypatch.setattr(ch.portfolio, "listener", boom)
    update = MagicMock()
    update.message.reply_text = AsyncMock()

    await ch.check(update, fake_context)

    update.message.reply_text.assert_awaited_once()
    sent = update.message.reply_text.call_args[0][0]
    assert "Something went wrong" in sent
