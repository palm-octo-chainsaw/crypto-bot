"""Tests for the /puller start|stop command."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from utils import command_handlers as ch


def _make_update():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def _seed_running_job(fake_context):
    return fake_context.job_queue.run_repeating(
        ch.poll_signal,
        interval=ch.SIGNAL_POLL_INTERVAL_SECONDS,
        first=10,
        name=ch.SIGNAL_POLL_JOB_NAME,
    )


@pytest.mark.asyncio
async def test_puller_stop_removes_running_job(fake_context):
    job = _seed_running_job(fake_context)
    fake_context.args = ["stop"]
    update = _make_update()

    await ch.puller(update, fake_context)

    assert job.removed is True
    assert fake_context.job_queue.get_jobs_by_name(ch.SIGNAL_POLL_JOB_NAME) == ()
    sent = update.message.reply_text.call_args[0][0]
    assert "stopped" in sent.lower()


@pytest.mark.asyncio
async def test_puller_stop_when_already_stopped(fake_context):
    fake_context.args = ["stop"]
    update = _make_update()

    await ch.puller(update, fake_context)

    sent = update.message.reply_text.call_args[0][0]
    assert "already stopped" in sent.lower()


@pytest.mark.asyncio
async def test_puller_start_schedules_job(fake_context):
    fake_context.args = ["start"]
    update = _make_update()

    await ch.puller(update, fake_context)

    jobs = fake_context.job_queue.get_jobs_by_name(ch.SIGNAL_POLL_JOB_NAME)
    assert len(jobs) == 1
    assert jobs[0].callback is ch.poll_signal
    sent = update.message.reply_text.call_args[0][0]
    assert "started" in sent.lower()


@pytest.mark.asyncio
async def test_puller_start_when_already_running(fake_context):
    _seed_running_job(fake_context)
    fake_context.args = ["start"]
    update = _make_update()

    await ch.puller(update, fake_context)

    # No duplicate job scheduled.
    assert len(fake_context.job_queue.get_jobs_by_name(ch.SIGNAL_POLL_JOB_NAME)) == 1
    sent = update.message.reply_text.call_args[0][0]
    assert "already running" in sent.lower()


@pytest.mark.asyncio
async def test_puller_no_arg_shows_usage(fake_context):
    fake_context.args = []
    update = _make_update()

    await ch.puller(update, fake_context)

    sent = update.message.reply_text.call_args[0][0]
    assert "/puller" in sent


@pytest.mark.asyncio
async def test_puller_start_then_stop_round_trip(fake_context):
    update = _make_update()

    fake_context.args = ["start"]
    await ch.puller(update, fake_context)
    assert len(fake_context.job_queue.get_jobs_by_name(ch.SIGNAL_POLL_JOB_NAME)) == 1

    fake_context.args = ["stop"]
    await ch.puller(update, fake_context)
    assert fake_context.job_queue.get_jobs_by_name(ch.SIGNAL_POLL_JOB_NAME) == ()
