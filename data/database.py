"""SQLite database for tracking signals, portfolio snapshots, and trades."""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "portfolio.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            allocations TEXT NOT NULL,
            total_pct REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            signal_id INTEGER,
            total_value_usd REAL NOT NULL,
            balances TEXT NOT NULL,
            prices TEXT NOT NULL,
            values_usd TEXT NOT NULL,
            targets TEXT NOT NULL,
            FOREIGN KEY (signal_id) REFERENCES signals(id)
        );

        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            signal_id INTEGER,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            amount REAL NOT NULL,
            price REAL,
            usd_value REAL,
            status TEXT NOT NULL,
            order_id TEXT,
            dry_run INTEGER NOT NULL,
            fee_amount REAL,
            fee_currency TEXT,
            fee_rate REAL,
            FOREIGN KEY (signal_id) REFERENCES signals(id)
        );
    """)
    # Migrate existing databases that lack the fee columns.
    for col, col_type in [("fee_amount", "REAL"), ("fee_currency", "TEXT"), ("fee_rate", "REAL")]:
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # Column already exists
    conn.close()
    logger.info("Database initialized at %s", DB_PATH)


def record_signal(allocations: dict) -> int:
    conn = get_connection()
    total_pct = sum(allocations.values())
    cursor = conn.execute(
        "INSERT INTO signals (timestamp, allocations, total_pct) VALUES (?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), json.dumps(allocations), total_pct),
    )
    signal_id = cursor.lastrowid
    conn.commit()
    conn.close()
    logger.info("Recorded signal id=%d with %d assets totaling %.1f%%", signal_id, len(allocations), total_pct)
    return signal_id


def record_snapshot(signal_id: int | None, total_value_usd: float,
                    balances: dict, prices: dict, values_usd: dict, targets: dict) -> None:
    conn = get_connection()
    conn.execute(
        """INSERT INTO snapshots
           (timestamp, signal_id, total_value_usd, balances, prices, values_usd, targets)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            signal_id,
            total_value_usd,
            json.dumps(balances),
            json.dumps(prices),
            json.dumps(values_usd),
            json.dumps(targets),
        ),
    )
    conn.commit()
    conn.close()
    logger.info("Recorded portfolio snapshot: $%.2f USD", total_value_usd)


def record_trade(signal_id: int | None, symbol: str, side: str, amount: float,
                 price: float | None, usd_value: float | None, status: str,
                 order_id: str | None, dry_run: bool,
                 fee_amount: float | None = None, fee_currency: str | None = None,
                 fee_rate: float | None = None) -> None:
    conn = get_connection()
    conn.execute(
        """INSERT INTO trades
           (timestamp, signal_id, symbol, side, amount, price, usd_value,
            status, order_id, dry_run, fee_amount, fee_currency, fee_rate)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            signal_id,
            symbol,
            side,
            amount,
            price,
            usd_value,
            status,
            order_id,
            1 if dry_run else 0,
            fee_amount,
            fee_currency,
            fee_rate,
        ),
    )
    conn.commit()
    conn.close()


def get_latest_signal_id() -> int | None:
    conn = get_connection()
    cursor = conn.execute("SELECT id FROM signals ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def get_latest_allocations() -> dict | None:
    conn = get_connection()
    cursor = conn.execute("SELECT allocations FROM signals ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    return json.loads(row[0]) if row else None
