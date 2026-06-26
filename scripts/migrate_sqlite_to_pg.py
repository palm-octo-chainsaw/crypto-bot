"""One-time migration of SQLite portfolio data into PostgreSQL.

Copies signals, snapshots, and trades from a SQLite portfolio.db into the
Postgres database addressed by DATABASE_URL, preserving primary-key ids (so
the snapshots.signal_id / trades.signal_id foreign keys stay valid) and then
resetting the identity sequences so future inserts don't collide.

Idempotent: rows that already exist (same id) are skipped via ON CONFLICT.

Usage:
    DATABASE_URL=postgresql://user:pass@host:5432/cryptobot \
        python scripts/migrate_sqlite_to_pg.py path/to/portfolio.db
"""
import os
import sqlite3
import sys

# Allow running directly (`python scripts/migrate_sqlite_to_pg.py ...`) by
# putting the repo root on the path so `data` resolves.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.database import get_connection, init_db

TABLES = {
    "signals": ["id", "timestamp", "allocations", "total_pct", "message_timestamp"],
    "snapshots": ["id", "timestamp", "signal_id", "total_value_usd",
                  "balances", "prices", "values_usd", "targets"],
    "trades": ["id", "timestamp", "signal_id", "symbol", "side", "amount", "price",
               "usd_value", "status", "order_id", "dry_run", "fee_amount",
               "fee_currency", "fee_rate"],
}


def migrate(sqlite_path: str) -> dict[str, int]:
    init_db()  # ensure the Postgres schema exists

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    dst = get_connection()
    counts: dict[str, int] = {}

    # Insert in FK order: signals first, then snapshots and trades.
    for table, cols in TABLES.items():
        rows = src.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY id").fetchall()
        placeholders = ", ".join(["%s"] * len(cols))
        for r in rows:
            dst.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT (id) DO NOTHING",
                tuple(r[c] for c in cols),
            )
        counts[table] = len(rows)

    # Advance each identity sequence past the migrated ids.
    for table in TABLES:
        dst.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), MAX(id)) "
            f"FROM {table} WHERE id IS NOT NULL"
        )

    dst.commit()
    dst.close()
    src.close()
    return counts


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/migrate_sqlite_to_pg.py path/to/portfolio.db")
    result = migrate(sys.argv[1])
    print("Migrated rows:", result)
