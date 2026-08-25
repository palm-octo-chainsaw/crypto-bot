"""Tests for Telegram send retries and the liveness heartbeat."""
import time

import pytest
from telegram.error import BadRequest, NetworkError

from utils import command_handlers as ch


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    async def instant(_seconds):
        return None
    monkeypatch.setattr(ch.asyncio, "sleep", instant)


@pytest.mark.asyncio
async def test_send_retries_until_it_succeeds():
    attempts = []

    async def flaky(text):
        attempts.append(text)
        if len(attempts) < 3:
            raise NetworkError("httpx.ConnectError: [Errno -3] Temporary failure in name resolution")
        return "sent"

    assert await ch._send_with_retry(flaky, "hello") == "sent"
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_send_gives_up_after_configured_attempts():
    attempts = []

    async def always_failing(text):
        attempts.append(text)
        raise NetworkError("name resolution failed")

    with pytest.raises(NetworkError):
        await ch._send_with_retry(always_failing, "hello")
    assert len(attempts) == ch.SEND_ATTEMPTS


@pytest.mark.asyncio
async def test_bad_request_is_not_retried():
    """BadRequest subclasses NetworkError, but resending the same bad message
    fails the same way — retrying only delays the error."""
    attempts = []

    async def bad_markdown(text):
        attempts.append(text)
        raise BadRequest("Can't parse entities")

    with pytest.raises(BadRequest):
        await ch._send_with_retry(bad_markdown, "*unclosed")
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_heartbeat_writes_current_timestamp(tmp_path, monkeypatch, fake_context):
    beat = tmp_path / "heartbeat"
    monkeypatch.setattr(ch, "HEARTBEAT_FILE", str(beat))

    await ch.heartbeat(fake_context)

    assert abs(int(beat.read_text()) - int(time.time())) < 5


@pytest.mark.asyncio
async def test_heartbeat_survives_unwritable_path(tmp_path, monkeypatch, fake_context):
    monkeypatch.setattr(ch, "HEARTBEAT_FILE", str(tmp_path / "missing-dir" / "heartbeat"))

    await ch.heartbeat(fake_context)  # must not raise into the job queue
