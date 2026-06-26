"""Integration tests for the Postgres data layer.

Skipped unless DATABASE_URL points at a reachable Postgres. Each test runs
against an isolated schema so it never touches real data. Run locally with:

    DATABASE_URL=postgresql://postgres@localhost:5432/postgres pytest tests/test_database_integration.py
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

DATABASE_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set")


@pytest.fixture
def db(monkeypatch):
    """Provision an isolated schema, point the module at it, and drop it after."""
    import psycopg

    schema = "test_" + uuid.uuid4().hex[:12]
    base = psycopg.connect(DATABASE_URL, autocommit=True)
    base.execute(f'CREATE SCHEMA "{schema}"')

    # Route every connection the module opens into the throwaway schema.
    import data.database as database

    def connect_in_schema():
        conn = psycopg.connect(DATABASE_URL)
        conn.execute(f'SET search_path TO "{schema}"')
        return conn

    monkeypatch.setattr(database, "get_connection", connect_in_schema)
    database.init_db()
    try:
        yield database
    finally:
        base.execute(f'DROP SCHEMA "{schema}" CASCADE')
        base.close()


def test_init_db_is_idempotent(db):
    db.init_db()  # second call must not raise (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS)


def test_record_signal_returns_id(db):
    sid = db.record_signal({"BTC": 60.0, "ETH": 40.0}, message_timestamp="2026-06-26 00:12")
    assert isinstance(sid, int) and sid > 0
    assert db.get_latest_signal_id() == sid
    assert db.get_latest_allocations() == {"BTC": 60.0, "ETH": 40.0}
    assert db.get_latest_message_timestamp() == "2026-06-26 00:12"


def test_snapshot_at_or_before_picks_nearest_older(db):
    now = datetime.now(timezone.utc)
    # Insert two snapshots at known times via the real recorder, then backdate them.
    db.record_snapshot(None, 9000.0, {}, {}, {}, {})
    db.record_snapshot(None, 10000.0, {}, {}, {}, {})
    conn = db.get_connection()
    old = (now - timedelta(days=10)).isoformat()
    new = (now - timedelta(days=1)).isoformat()
    conn.execute("UPDATE snapshots SET timestamp = %s WHERE total_value_usd = 9000.0", (old,))
    conn.execute("UPDATE snapshots SET timestamp = %s WHERE total_value_usd = 10000.0", (new,))
    conn.commit()
    conn.close()

    # A 7d-ago cutoff must resolve to the 10-day-old snapshot, not the 1-day-old one.
    snap = db.get_snapshot_at_or_before(now - timedelta(days=7))
    assert snap["total_value_usd"] == 9000.0
    assert db.get_earliest_snapshot()["total_value_usd"] == 9000.0


def test_record_trade_and_recent(db):
    sid = db.record_signal({"BTC": 100.0})
    db.record_trade(sid, "BTC", "buy", 0.01, 60000.0, 600.0, "filled", "oid-1", dry_run=False)
    db.record_trade(sid, "ETH", "sell", 0.5, 3000.0, 1500.0, "filled", "oid-2", dry_run=True)
    trades = db.get_recent_trades(limit=5)
    assert len(trades) == 2
    assert trades[0]["symbol"] == "ETH" and trades[0]["dry_run"] is True
    assert trades[1]["symbol"] == "BTC" and trades[1]["dry_run"] is False
