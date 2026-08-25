"""Tests for /status and the plain read-only commands in utils/command_handlers."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from utils import command_handlers as ch


def _make_update():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def _replies(update):
    return [call.args[0] for call in update.message.reply_text.call_args_list]


@pytest.fixture
def stub_portfolio(monkeypatch):
    fake = MagicMock()
    fake.fetch_live_data.return_value = ({}, {}, 1234.56)
    fake.portfolio = {"BTC": 0.5, "USDC": 100.0}
    fake.targets = {"BTC": 60.0, "USDC": 40.0}
    monkeypatch.setattr(ch, "portfolio", fake)
    return fake


@pytest.mark.asyncio
async def test_status_reports_stopped_puller_and_counters(fake_context, monkeypatch):
    monkeypatch.setattr(ch, "_poll_success_count", 7)
    monkeypatch.setattr(ch, "_poll_failure_count", 2)
    monkeypatch.setattr(ch, "_scrape_failure_count", 1)
    monkeypatch.setattr(ch, "_last_poll_status", "unchanged")
    update = _make_update()

    await ch.status(update, fake_context)

    out = _replies(update)[0]
    assert "🛑 stopped" in out
    assert "Last poll: never" in out
    assert "Successes: 7" in out
    assert "Failures: 2 (consecutive: 1)" in out


@pytest.mark.asyncio
async def test_status_reports_running_puller_and_last_poll(fake_context, monkeypatch):
    fake_context.job_queue.run_repeating(
        ch.poll_signal, interval=ch.SIGNAL_POLL_INTERVAL_SECONDS, first=10,
        name=ch.SIGNAL_POLL_JOB_NAME,
    )
    monkeypatch.setattr(ch, "_last_poll_time", datetime(2026, 4, 30, 9, 15, tzinfo=timezone.utc))
    update = _make_update()

    await ch.status(update, fake_context)

    out = _replies(update)[0]
    assert "🟢 running" in out
    assert "Last poll: 2026-04-30 09:15:00 UTC" in out


@pytest.mark.asyncio
async def test_status_shows_credentials_paused_and_cooldown(fake_context, monkeypatch):
    monkeypatch.setattr(ch, "_credentials_invalid", True)
    monkeypatch.setattr(ch, "_rate_limit_until",
                        datetime.now(timezone.utc) + timedelta(minutes=25))
    update = _make_update()

    await ch.status(update, fake_context)

    out = _replies(update)[0]
    assert "credentials invalid" in out
    assert "rate-limit cooldown" in out and "24 min remaining" in out


def test_get_poll_jobs_without_job_queue():
    """PTB leaves job_queue None when the JobQueue extra isn't installed."""
    context = MagicMock()
    context.job_queue = None
    assert ch._get_poll_jobs(context) == ()


def test_signal_section_degrades_when_database_unreachable(monkeypatch):
    def boom():
        raise RuntimeError("no db")
    monkeypatch.setattr(ch, "get_latest_allocations", boom)

    out = "\n".join(ch._format_signal_section())
    assert "unavailable" in out and "no db" in out


def test_connectivity_arbitrum_error(monkeypatch, stub_portfolio):
    stub_portfolio.balance.w3.is_connected.side_effect = RuntimeError("rpc down")
    assert "Arbitrum: ❌ (rpc down)" in ch._ping_arbitrum()


def test_connectivity_arbitrum_disconnected(monkeypatch, stub_portfolio):
    stub_portfolio.balance.w3.is_connected.return_value = False
    assert ch._ping_arbitrum() == "Arbitrum: ❌"


def test_trades_section_degrades_when_query_fails(monkeypatch):
    def boom(limit=5):
        raise RuntimeError("relation trades does not exist")
    monkeypatch.setattr(ch, "get_recent_trades", boom)

    out = "\n".join(ch._format_trades_section())
    assert "unavailable" in out and "relation trades" in out


def test_performance_line_reports_gain(monkeypatch):
    monkeypatch.setattr(ch, "get_snapshot_at_or_before", lambda when: {"total_value_usd": 1000.0})
    line = ch._format_performance_line("24h", timedelta(hours=24), 1250.0,
                                       datetime.now(timezone.utc))
    assert line == "24h: +$250.00 (+25.00%) 📈"


def test_performance_line_reports_loss(monkeypatch):
    monkeypatch.setattr(ch, "get_snapshot_at_or_before", lambda when: {"total_value_usd": 1000.0})
    line = ch._format_performance_line("7d", timedelta(days=7), 800.0,
                                       datetime.now(timezone.utc))
    assert line == "7d: -$200.00 (-20.00%) 📉"


def test_performance_line_all_window_uses_earliest_snapshot(monkeypatch):
    monkeypatch.setattr(ch, "get_earliest_snapshot", lambda: {"total_value_usd": 500.0})
    line = ch._format_performance_line("all", None, 750.0, datetime.now(timezone.utc))
    assert line == "all: +$250.00 (+50.00%) 📈"


def test_performance_line_without_history(monkeypatch):
    monkeypatch.setattr(ch, "get_snapshot_at_or_before", lambda when: None)
    assert ch._format_performance_line("30d", timedelta(days=30), 1000.0,
                                       datetime.now(timezone.utc)) == "30d: insufficient history"


def test_performance_line_rejects_nan_baseline(monkeypatch):
    """A NaN baseline slips through every comparison and would render as -$nan."""
    monkeypatch.setattr(ch, "get_snapshot_at_or_before", lambda when: {"total_value_usd": float("nan")})
    assert "insufficient history" in ch._format_performance_line(
        "24h", timedelta(hours=24), 1000.0, datetime.now(timezone.utc))


def test_format_performance_all_windows(monkeypatch, stub_portfolio):
    monkeypatch.setattr(ch, "get_snapshot_at_or_before", lambda when: {"total_value_usd": 1000.0})
    monkeypatch.setattr(ch, "get_earliest_snapshot", lambda: {"total_value_usd": 1000.0})

    out = ch._format_performance(None)

    assert "Total: $1,234.56 USD" in out
    for label in ch.PERFORMANCE_WINDOW_KEYS:
        assert f"{label}: " in out


def test_format_performance_single_window(monkeypatch, stub_portfolio):
    monkeypatch.setattr(ch, "get_snapshot_at_or_before", lambda when: {"total_value_usd": 1000.0})

    out = ch._format_performance("24h")

    assert "24h: " in out
    assert "7d: " not in out


def test_format_performance_rejects_unknown_window(stub_portfolio):
    assert ch._format_performance("1y") == ch.PERFORMANCE_USAGE


@pytest.mark.asyncio
async def test_performance_command_replies(monkeypatch, fake_context, stub_portfolio):
    monkeypatch.setattr(ch, "get_snapshot_at_or_before", lambda when: {"total_value_usd": 1000.0})
    fake_context.args = ["24h"]
    update = _make_update()

    await ch.performance(update, fake_context)

    assert "24h: +$234.56" in _replies(update)[0]


@pytest.mark.asyncio
async def test_performance_command_reports_generic_error(monkeypatch, fake_context, stub_portfolio):
    stub_portfolio.update_portfolio.side_effect = RuntimeError("binance down")
    fake_context.args = []
    update = _make_update()

    await ch.performance(update, fake_context)

    assert _replies(update)[0] == ch.GENERIC_ERROR_REPLY


@pytest.mark.asyncio
async def test_info_command_replies(monkeypatch, fake_context):
    monkeypatch.setattr(ch, "_format_info", lambda: "info body")
    update = _make_update()

    await ch.info(update, fake_context)

    assert "info body" in _replies(update)[0]


@pytest.mark.asyncio
async def test_info_command_reports_generic_error(monkeypatch, fake_context):
    def boom():
        raise RuntimeError("nope")
    monkeypatch.setattr(ch, "_format_info", boom)
    update = _make_update()

    await ch.info(update, fake_context)

    assert _replies(update)[0] == ch.GENERIC_ERROR_REPLY


@pytest.mark.asyncio
async def test_get_targets_lists_targets_and_total(fake_context, stub_portfolio):
    update = _make_update()

    await ch.get_targets(update, fake_context)

    out = _replies(update)[0]
    assert "BTC: 60.0%" in out and "USDC: 40.0%" in out
    assert "Total: 100.0%" in out


@pytest.mark.asyncio
async def test_set_target_writes_targets_file(monkeypatch, fake_context, stub_portfolio):
    written = {}
    monkeypatch.setattr(ch, "write_json", lambda path, data: written.update(path=path, data=data))
    stub_portfolio.get_targets.return_value = {"BTC": 40.0}
    fake_context.args = ["btc", "40"]
    update = _make_update()

    await ch.set_target(update, fake_context)

    stub_portfolio.set_target.assert_called_once_with("BTC", 40.0)
    assert written["path"] == ch.TARGETS_FILE
    assert "Target for BTC set to 40.0%" in _replies(update)[0]


@pytest.mark.asyncio
async def test_set_target_rejects_out_of_range_percent(fake_context, stub_portfolio):
    fake_context.args = ["BTC", "140"]
    update = _make_update()

    await ch.set_target(update, fake_context)

    assert "Usage: /set_target" in _replies(update)[0]
    stub_portfolio.set_target.assert_not_called()


@pytest.mark.asyncio
async def test_set_target_rejects_missing_arguments(fake_context, stub_portfolio):
    fake_context.args = ["BTC"]
    update = _make_update()

    await ch.set_target(update, fake_context)

    assert "Usage: /set_target" in _replies(update)[0]


@pytest.mark.asyncio
async def test_get_total_reports_value(fake_context, stub_portfolio):
    update = _make_update()

    await ch.get_total(update, fake_context)

    assert "$1,234.56 USD" in _replies(update)[0]


@pytest.mark.asyncio
async def test_get_total_reports_generic_error(fake_context, stub_portfolio):
    stub_portfolio.fetch_live_data.side_effect = RuntimeError("prices down")
    update = _make_update()

    await ch.get_total(update, fake_context)

    assert _replies(update)[0] == ch.GENERIC_ERROR_REPLY


@pytest.mark.asyncio
async def test_get_spot_balance_lists_holdings(fake_context, stub_portfolio):
    update = _make_update()

    await ch.get_spot_balance(update, fake_context)

    out = _replies(update)[0]
    assert "BTC: 0.5" in out and "USDC: 100.0" in out


@pytest.mark.asyncio
async def test_get_spot_balance_reports_generic_error(fake_context, stub_portfolio):
    stub_portfolio.update_portfolio.side_effect = RuntimeError("binance down")
    update = _make_update()

    await ch.get_spot_balance(update, fake_context)

    assert _replies(update)[0] == ch.GENERIC_ERROR_REPLY


@pytest.mark.asyncio
async def test_get_leverage_balance_lists_positions(fake_context, stub_portfolio):
    stub_portfolio.balance.get_leverage_balance.return_value = {"BTC": 0.25}
    update = _make_update()

    await ch.get_leverage_balance(update, fake_context)

    assert "BTC: 0.25" in _replies(update)[0]


@pytest.mark.asyncio
async def test_get_leverage_balance_reports_generic_error(fake_context, stub_portfolio):
    stub_portfolio.balance.get_leverage_balance.side_effect = RuntimeError("no margin account")
    update = _make_update()

    await ch.get_leverage_balance(update, fake_context)

    assert _replies(update)[0] == ch.GENERIC_ERROR_REPLY


def test_apply_allocations_zeroes_targets_absent_from_the_signal(monkeypatch, stub_portfolio):
    stub_portfolio.targets = {"BTC": 60.0, "ETH": 40.0}
    written = {}
    monkeypatch.setattr(ch, "write_json", lambda path, data: written.update(path=path, data=data))

    ch._apply_allocations({"BTC": 100.0})

    assert stub_portfolio.targets == {"BTC": 100.0, "ETH": 0.0}
    assert written["data"] == stub_portfolio.targets
