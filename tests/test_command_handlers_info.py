"""Unit tests for the /info command formatting in utils/command_handlers."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from utils import command_handlers as ch


@pytest.fixture
def fixed_started(monkeypatch):
    started = datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(ch, "_started_at", started)
    return started


@pytest.fixture
def stub_portfolio(monkeypatch):
    fake = MagicMock()
    fake.balance.binance_client = MagicMock()
    fake.balance.binance_client.ping.return_value = {}
    fake.balance.w3.is_connected.return_value = True
    fake.update_portfolio = MagicMock()
    fake.fetch_live_data.return_value = ({}, {}, 1234.56)
    fake.portfolio = {"BTC": 0.5, "ETH": 1.0, "USDC": 100.0}
    monkeypatch.setattr(ch, "portfolio", fake)
    return fake


def test_uptime_formatting():
    assert ch._format_uptime(timedelta(minutes=5)) == "5m"
    assert ch._format_uptime(timedelta(hours=2, minutes=3)) == "2h 3m"
    assert ch._format_uptime(timedelta(days=1, hours=4, minutes=10)) == "1d 4h 10m"


def test_version_falls_back_when_env_missing(monkeypatch, fixed_started):
    monkeypatch.delenv("APP_VERSION", raising=False)
    now = fixed_started + timedelta(hours=2)
    section = "\n".join(ch._format_version_section(now))
    assert "Version: `unknown`" in section
    assert "Uptime: 2h 0m" in section


def test_version_uses_env_when_set(monkeypatch, fixed_started):
    monkeypatch.setenv("APP_VERSION", "v1.2.3")
    now = fixed_started + timedelta(minutes=15)
    section = "\n".join(ch._format_version_section(now))
    assert "Version: `v1.2.3`" in section


def test_poller_section_shows_paused_when_credentials_invalid(monkeypatch):
    monkeypatch.setattr(ch, "_credentials_invalid", True)
    monkeypatch.setattr(ch, "_last_poll_status", "paused (invalid credentials)")
    out = "\n".join(ch._format_poller_section(datetime.now(timezone.utc)))
    assert "Paused" in out and "invalid" in out


def test_poller_section_shows_cooldown(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(ch, "_rate_limit_until", now + timedelta(minutes=20))
    out = "\n".join(ch._format_poller_section(now))
    assert "Cooldown:" in out and "20 min" in out


def test_signal_section_empty_when_no_db(monkeypatch):
    monkeypatch.setattr(ch, "get_latest_allocations", lambda: None)
    monkeypatch.setattr(ch, "get_latest_message_timestamp", lambda: None)
    out = "\n".join(ch._format_signal_section())
    assert "none recorded" in out


def test_signal_section_renders_allocations(monkeypatch):
    monkeypatch.setattr(ch, "get_latest_allocations", lambda: {"BTC": 66.6, "ETH": 33.3})
    monkeypatch.setattr(ch, "get_latest_message_timestamp", lambda: "2026-04-30T08:00:00Z")
    out = "\n".join(ch._format_signal_section())
    assert "BTC: 66.6%" in out
    assert "ETH: 33.3%" in out
    assert "2026-04-30T08:00:00Z" in out


def test_portfolio_section_handles_exchange_failure(monkeypatch):
    fake = MagicMock()
    fake.update_portfolio.side_effect = RuntimeError("binance down")
    monkeypatch.setattr(ch, "portfolio", fake)
    out = "\n".join(ch._format_portfolio_section())
    assert "unavailable" in out and "binance down" in out


def test_connectivity_binance_down(monkeypatch, stub_portfolio):
    stub_portfolio.balance.binance_client.ping.side_effect = RuntimeError("conn refused")
    out = "\n".join(ch._format_connectivity_section())
    assert "Binance: ❌" in out
    assert "Arbitrum: ✅" in out


def test_connectivity_no_binance_credentials(monkeypatch, stub_portfolio):
    stub_portfolio.balance.binance_client = None
    out = "\n".join(ch._format_connectivity_section())
    assert "Binance: ⚠️ no credentials" in out


def test_trades_section_empty(monkeypatch):
    monkeypatch.setattr(ch, "get_recent_trades", lambda limit=5: [])
    out = "\n".join(ch._format_trades_section())
    assert "none recorded" in out


def test_trades_section_renders_rows(monkeypatch):
    rows = [
        {"timestamp": "2026-04-30T11:00:00+00:00", "symbol": "BTC/USDC", "side": "buy",
         "amount": 0.012345, "usd_value": 600.0, "status": "closed", "dry_run": False},
        {"timestamp": "2026-04-30T11:05:00+00:00", "symbol": "ETH/USDC", "side": "sell",
         "amount": 0.5, "usd_value": None, "status": "ok", "dry_run": True},
    ]
    monkeypatch.setattr(ch, "get_recent_trades", lambda limit=5: rows)
    out = "\n".join(ch._format_trades_section())
    assert "buy BTC/USDC" in out
    assert "$600.00" in out
    assert "(dry)" in out
    assert "—" in out  # null usd_value placeholder


def test_format_info_combines_all_sections(monkeypatch, fixed_started, stub_portfolio):
    monkeypatch.setenv("APP_VERSION", "v1.2.3")
    monkeypatch.setattr(ch, "get_latest_allocations", lambda: {"BTC": 50.0})
    monkeypatch.setattr(ch, "get_latest_message_timestamp", lambda: "ts")
    monkeypatch.setattr(ch, "get_recent_trades", lambda limit=5: [])
    out = ch._format_info()
    for header in ("Version & Uptime", "Poller", "Latest Signal", "Portfolio",
                   "Connectivity", "Recent Trades"):
        assert header in out
