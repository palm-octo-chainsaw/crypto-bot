"""Discord webhook posting of new signals."""
import asyncio
import logging
from unittest.mock import MagicMock

import pytest
import requests

import constants
from utils import command_handlers as ch
from utils import discord
from tests.test_signal_flow import new_signal, stub_portfolio  # noqa: F401  (fixtures)

WEBHOOK = "https://discord.com/api/webhooks/1/secret-token"


def _response(status, body=None):
    response = MagicMock()
    response.status_code = status
    response.ok = 200 <= status < 300
    response.json.return_value = body or {}
    return response


@pytest.fixture
def webhook(monkeypatch):
    monkeypatch.setattr(constants, "DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr(discord.time, "sleep", lambda s: None)


@pytest.fixture
def posts(monkeypatch):
    """Record every requests.post call; answer with the queued responses."""
    calls = []
    queue = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))
        result = queue.pop(0) if queue else _response(204)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(discord.requests, "post", fake_post)
    return calls, queue


def test_payload_blocks_mentions_and_lists_each_asset():
    payload = discord.build_signal_payload({"BTC": 60.0, "USDC": 40.0}, "2026-10-03 09:00")
    embed = payload["embeds"][0]
    assert payload["allowed_mentions"] == {"parse": []}
    assert [(f["name"], f["value"]) for f in embed["fields"]] == [("BTC", "60.0%"), ("USDC", "40.0%")]
    assert "2026-10-03 09:00" in embed["description"]
    assert embed["footer"]["text"] == "Total: 100.0%"


def test_post_signal_without_webhook_does_nothing(posts):
    calls, _ = posts
    assert asyncio.run(discord.post_signal({"BTC": 100.0}, None)) is None
    assert calls == []


def test_post_signal_success(webhook, posts):
    calls, _ = posts
    assert asyncio.run(discord.post_signal({"BTC": 100.0}, "t")) is True
    assert len(calls) == 1
    assert calls[0][0] == WEBHOOK


def test_post_signal_retries_server_errors_then_gives_up(webhook, posts):
    calls, queue = posts
    queue.extend([_response(500), _response(502), _response(503)])
    assert asyncio.run(discord.post_signal({"BTC": 100.0}, None)) is False
    assert len(calls) == discord.POST_ATTEMPTS


def test_post_signal_waits_retry_after_on_429(webhook, posts, monkeypatch):
    calls, queue = posts
    sleeps = []
    monkeypatch.setattr(discord.time, "sleep", sleeps.append)
    queue.extend([_response(429, {"retry_after": 1.5}), _response(204)])
    assert asyncio.run(discord.post_signal({"BTC": 100.0}, None)) is True
    assert sleeps == [1.5]
    assert len(calls) == 2


def test_post_signal_caps_retry_after(webhook, posts, monkeypatch):
    _, queue = posts
    sleeps = []
    monkeypatch.setattr(discord.time, "sleep", sleeps.append)
    queue.extend([_response(429, {"retry_after": 3600}), _response(204)])
    asyncio.run(discord.post_signal({"BTC": 100.0}, None))
    assert sleeps == [discord.MAX_RETRY_AFTER_SECONDS]


def test_post_signal_does_not_retry_client_errors(webhook, posts):
    calls, queue = posts
    queue.append(_response(404))
    assert asyncio.run(discord.post_signal({"BTC": 100.0}, None)) is False
    assert len(calls) == 1


def test_network_error_never_logs_webhook(webhook, posts, caplog):
    _, queue = posts
    error = requests.ConnectionError(f"Max retries exceeded with url: {WEBHOOK}")
    queue.extend([error, error, error])
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(discord.post_signal({"BTC": 100.0}, None)) is False
    assert "secret-token" not in caplog.text


def test_webhook_is_redacted_from_chat_errors(webhook):
    assert WEBHOOK in ch._secret_values()


def _run_poll(fake_context):
    async def run():
        await ch.poll_signal(fake_context)
        await asyncio.gather(*ch._discord_tasks)
    asyncio.run(run())


@pytest.fixture
def fake_post_signal(monkeypatch):
    calls = []
    result = {"value": True}

    async def fake(allocations, signal_time):
        calls.append((allocations, signal_time))
        return result["value"]

    monkeypatch.setattr(ch.discord, "post_signal", fake)
    return calls, result


def test_new_signal_is_posted_to_discord(webhook, new_signal, stub_portfolio, fake_context, fake_post_signal):
    calls, _ = fake_post_signal
    _run_poll(fake_context)
    assert calls == [(new_signal, "2026-04-30 10:00")]
    assert ch._discord_status.startswith("posted")
    stub_portfolio.execute_rebalance.assert_called_once()


def test_discord_failure_warns_and_still_rebalances(
    webhook, new_signal, stub_portfolio, fake_context, fake_post_signal,
):
    _, result = fake_post_signal
    result["value"] = False
    _run_poll(fake_context)
    stub_portfolio.execute_rebalance.assert_called_once()
    assert any("Couldn't post the new signal to Discord" in m for m in fake_context.bot.sent)
    assert ch._discord_status.startswith("failed")


def test_unchanged_signal_is_not_posted(webhook, new_signal, stub_portfolio, fake_context, fake_post_signal, monkeypatch):
    calls, _ = fake_post_signal
    monkeypatch.setattr(ch, "_is_same_signal", lambda allocations, signal_time: True)
    _run_poll(fake_context)
    assert calls == []


def test_no_webhook_means_no_post(new_signal, stub_portfolio, fake_context, fake_post_signal):
    calls, _ = fake_post_signal
    _run_poll(fake_context)
    assert calls == []
    stub_portfolio.execute_rebalance.assert_called_once()


def test_manual_fetch_posts_new_signal(webhook, new_signal, fake_update, fake_context, fake_post_signal, monkeypatch):
    calls, _ = fake_post_signal

    async def run():
        await ch.fetch_signal(fake_update, fake_context)
        await asyncio.gather(*ch._discord_tasks)
    asyncio.run(run())
    assert calls == [(new_signal, "2026-04-30 10:00")]
