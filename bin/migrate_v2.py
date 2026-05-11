#!/usr/bin/env python3
"""V2 migration: extend timeframe CHECK to include 6M/1Y, add analysis_group_id.

SQLite cannot ALTER an existing CHECK constraint, so we migrate by creating a
new table with the updated schema, copying rows (setting analysis_group_id to
NULL for legacy rows), and swapping tables inside a single transaction.

The migration is idempotent at the "schema-is-current" level: if the table
already has ``analysis_group_id``, we skip. Dry-run (default) prints what
would happen. Run again with ``--confirm`` to commit.

Usage:
    uv run python bin/migrate_v2.py                    # dry run against data/predictions.db
    uv run python bin/migrate_v2.py --confirm          # commit
    uv run python bin/migrate_v2.py --db path/to.db    # alternate DB
"""

import argparse
import sqlite3
import sys
from pathlib import Path


NEW_SCHEMA = """
CREATE TABLE predictions_new (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    market TEXT NOT NULL CHECK(market IN ('US', 'KR')),
    direction TEXT NOT NULL CHECK(direction IN ('BULL', 'BEAR', 'NEUTRAL')),
    confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
    timeframe TEXT NOT NULL CHECK(timeframe IN ('1W','2W','1M','3M','6M','1Y')),
    reasoning TEXT NOT NULL,
    entry_price REAL NOT NULL,
    signals_used TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'LIVE' CHECK(source IN ('LIVE','BACKTEST','INTERACTIVE')),
    target_price REAL,
    stop_price REAL,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','HIT','MISS','EXPIRED','CANCELLED')),
    outcome_price REAL,
    outcome_date TEXT,
    outcome_return REAL,
    analysis_group_id TEXT
);
"""


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if the given column exists on the given table."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def migrate(db_path: Path, confirm: bool) -> int:
    """Migrate a predictions SQLite DB to the V2 schema.

    Args:
        db_path: Path to the SQLite database file.
        confirm: When False (default), prints what would happen and exits
            without writing. When True, performs the migration transaction.

    Returns:
        0 on success, 1 on any row-count mismatch or unexpected state.
    """
    if not db_path.exists():
        print(f"ERROR: DB not found at {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        before = conn.execute("SELECT count(*) FROM predictions").fetchone()[0]

        if _has_column(conn, "predictions", "analysis_group_id"):
            print(
                f"Schema already current: analysis_group_id present on "
                f"predictions ({before} rows). No-op."
            )
            return 0

        if not confirm:
            print(
                f"DRY RUN: would migrate {before} rows in {db_path} to V2 schema "
                f"(add analysis_group_id, extend timeframe CHECK to 6M/1Y).\n"
                f"Re-run with --confirm to apply."
            )
            return 0

        conn.executescript(
            f"""
            BEGIN;
            ALTER TABLE predictions RENAME TO predictions_old;
            {NEW_SCHEMA}
            INSERT INTO predictions_new
              SELECT id, created_at, ticker, market, direction, confidence, timeframe,
                     reasoning, entry_price, signals_used, source, target_price, stop_price,
                     status, outcome_price, outcome_date, outcome_return, NULL
              FROM predictions_old;
            DROP TABLE predictions_old;
            ALTER TABLE predictions_new RENAME TO predictions;
            CREATE INDEX IF NOT EXISTS idx_predictions_status ON predictions(status);
            CREATE INDEX IF NOT EXISTS idx_predictions_market ON predictions(market);
            CREATE INDEX IF NOT EXISTS idx_predictions_ticker ON predictions(ticker);
            CREATE INDEX IF NOT EXISTS idx_predictions_created ON predictions(created_at);
            CREATE INDEX IF NOT EXISTS idx_predictions_group ON predictions(analysis_group_id);
            COMMIT;
            """
        )

        after = conn.execute("SELECT count(*) FROM predictions").fetchone()[0]
        if before != after:
            print(
                f"ERROR: row count mismatch — before={before}, after={after}",
                file=sys.stderr,
            )
            return 1

        print(f"Migrated {before} -> {after} rows in {db_path}.")
        return 0
    finally:
        conn.close()


def main() -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="data/predictions.db", help="Path to SQLite DB")
    ap.add_argument("--confirm", action="store_true", help="Actually perform migration")
    args = ap.parse_args()
    return migrate(Path(args.db), args.confirm)


if __name__ == "__main__":
    sys.exit(main())
