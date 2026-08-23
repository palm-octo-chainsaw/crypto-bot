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
    old = now - timedelta(days=10)
    new = now - timedelta(days=1)
    conn.execute("UPDATE snapshots SET timestamp = %s WHERE total_value_usd = 9000.0", (old,))
    conn.execute("UPDATE snapshots SET timestamp = %s WHERE total_value_usd = 10000.0", (new,))
    conn.commit()
    conn.close()

    # A 7d-ago cutoff must resolve to the 10-day-old snapshot, not the 1-day-old one.
    snap = db.get_snapshot_at_or_before(now - timedelta(days=7))
    assert snap["total_value_usd"] == 9000.0
    assert db.get_earliest_snapshot()["total_value_usd"] == 9000.0


def test_timestamps_are_stored_as_timestamptz(db):
    """Range queries must compare real timestamps, not ISO strings byte by byte."""
    db.record_snapshot(None, 9000.0, {}, {}, {}, {})
    conn = db.get_connection()
    types = dict(conn.execute(
        """SELECT table_name, data_type FROM information_schema.columns
           WHERE column_name = 'timestamp'
             AND table_name IN ('signals', 'snapshots', 'trades')
             AND table_schema = current_schema()"""
    ).fetchall())
    conn.close()
    assert types == {
        "signals": "timestamp with time zone",
        "snapshots": "timestamp with time zone",
        "trades": "timestamp with time zone",
    }
    assert isinstance(db.get_latest_snapshot()["timestamp"], datetime)


def test_init_db_converts_legacy_text_timestamps(db):
    """The prod schema came from SQLite with TEXT timestamps; init_db must promote it."""
    conn = db.get_connection()
    conn.execute("ALTER TABLE snapshots ALTER COLUMN timestamp TYPE TEXT")
    conn.execute(
        "INSERT INTO snapshots (timestamp, total_value_usd, balances, prices, values_usd, targets) "
        "VALUES ('2026-08-16T00:32:19.160505+00:00', 9000.0, '{}', '{}', '{}', '{}')"
    )
    conn.commit()
    conn.close()

    db.init_db()

    snap = db.get_latest_snapshot()
    assert snap["timestamp"] == datetime(2026, 8, 16, 0, 32, 19, 160505, tzinfo=timezone.utc)
    # The converted rows must be reachable by a real range query, not a string compare.
    cutoff = datetime(2026, 8, 16, 0, 44, tzinfo=timezone.utc)
    assert db.get_snapshot_at_or_before(cutoff)["total_value_usd"] == 9000.0


def test_record_snapshot_rejects_collapsed_total(db):
    """A failed venue read reports ~$0; that must not become the next baseline."""
    db.record_snapshot(None, 10000.0, {}, {}, {}, {})
    db.record_snapshot(None, 2.64, {}, {}, {}, {})  # every exchange returned nothing
    assert db.get_latest_snapshot()["total_value_usd"] == 10000.0


def test_record_snapshot_allows_collapse_once_baseline_is_stale(db):
    """A real collapse must not wedge snapshots forever behind an old high."""
    db.record_snapshot(None, 10000.0, {}, {}, {}, {})
    conn = db.get_connection()
    conn.execute(
        "UPDATE snapshots SET timestamp = %s",
        (datetime.now(timezone.utc) - timedelta(days=5),),
    )
    conn.commit()
    conn.close()

    db.record_snapshot(None, 100.0, {}, {}, {}, {})
    assert db.get_latest_snapshot()["total_value_usd"] == 100.0


def test_record_snapshot_allows_large_gains(db):
    """The gate is one-sided: deposits and rallies must still be recorded."""
    db.record_snapshot(None, 1000.0, {}, {}, {}, {})
    db.record_snapshot(None, 50000.0, {}, {}, {}, {})
    assert db.get_latest_snapshot()["total_value_usd"] == 50000.0


def test_record_trade_and_recent(db):
    sid = db.record_signal({"BTC": 100.0})
    db.record_trade(sid, "BTC", "buy", 0.01, 60000.0, 600.0, "filled", "oid-1", dry_run=False)
    db.record_trade(sid, "ETH", "sell", 0.5, 3000.0, 1500.0, "filled", "oid-2", dry_run=True)
    trades = db.get_recent_trades(limit=5)
    assert len(trades) == 2
    assert trades[0]["symbol"] == "ETH" and trades[0]["dry_run"] is True
    assert trades[1]["symbol"] == "BTC" and trades[1]["dry_run"] is False


def test_sqlite_migration_preserves_ids_and_advances_sequences(db, tmp_path, monkeypatch):
    """End-to-end check of scripts/migrate_sqlite_to_pg against real Postgres."""
    import sqlite3

    # Build a minimal SQLite DB mirroring the pre-migration schema, with
    # non-contiguous ids to prove the FK references and sequence reset.
    sqlite_path = str(tmp_path / "portfolio.db")
    s = sqlite3.connect(sqlite_path)
    s.executescript(
        """
        CREATE TABLE signals (id INTEGER PRIMARY KEY, timestamp TEXT, allocations TEXT,
                              total_pct REAL, message_timestamp TEXT);
        CREATE TABLE snapshots (id INTEGER PRIMARY KEY, timestamp TEXT, signal_id INTEGER,
                                total_value_usd REAL, balances TEXT, prices TEXT,
                                values_usd TEXT, targets TEXT);
        CREATE TABLE trades (id INTEGER PRIMARY KEY, timestamp TEXT, signal_id INTEGER,
                             symbol TEXT, side TEXT, amount REAL, price REAL, usd_value REAL,
                             status TEXT, order_id TEXT, dry_run INTEGER, fee_amount REAL,
                             fee_currency TEXT, fee_rate REAL);
        """
    )
    s.execute("INSERT INTO signals VALUES (5,'2026-06-01T00:00:00+00:00','{\"BTC\": 100.0}',100.0,'2026-06-01 00:00')")
    s.execute("INSERT INTO snapshots VALUES (9,'2026-06-01T00:01:00+00:00',5,10000.0,'{}','{}','{}','{}')")
    s.execute("INSERT INTO trades VALUES (3,'2026-06-01T00:02:00+00:00',5,'BTC','buy',0.1,60000.0,6000.0,'filled','o1',0,NULL,NULL,NULL)")
    s.commit()
    s.close()

    # Point the migration script at the test schema (it binds its own names).
    import scripts.migrate_sqlite_to_pg as mig
    monkeypatch.setattr(mig, "get_connection", db.get_connection)
    monkeypatch.setattr(mig, "init_db", db.init_db)

    counts = mig.migrate(sqlite_path)
    assert counts == {"signals": 1, "snapshots": 1, "trades": 1}

    # Ids preserved; FK intact; sequence advanced so the next insert is id 6.
    assert db.get_latest_signal_id() == 5
    assert db.get_snapshot_at_or_before(datetime.now(timezone.utc))["total_value_usd"] == 10000.0
    assert db.get_recent_trades()[0]["symbol"] == "BTC"
    assert db.record_signal({"ETH": 100.0}) == 6

    # Re-running is idempotent (ON CONFLICT DO NOTHING) — no duplicates/errors.
    assert mig.migrate(sqlite_path) == {"signals": 1, "snapshots": 1, "trades": 1}
