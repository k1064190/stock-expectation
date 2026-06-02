"""One-time migration: fix BEAR outcome_return sign + drop same-day duplicates.

Two corrections applied to ``data/predictions.db``:

1. **BEAR sign fix.** Before Stage 2, ``outcome_tracker`` stored the raw price
   change as ``outcome_return`` regardless of direction, so a winning BEAR call
   (price fell) was recorded with a negative return. We now define
   ``outcome_return`` as the position return (BEAR drop = positive), so every
   existing BEAR row's sign is flipped to match.
2. **Same-day duplicate removal.** Re-runs of the scheduler logged identical
   predictions multiple times. We keep the earliest row (MIN(rowid)) per
   (ticker, market, direction, timeframe, source, created date, entry_price)
   and delete the rest.

IMPORTANT — run exactly once, immediately after deploying the Stage 2 store /
outcome-tracker changes and *before* the outcome tracker re-scores any BEAR
predictions under the new (already-correct) sign. A ``_migrations`` marker row
makes a second run abort, but it cannot detect rows newly scored under the new
logic, so timing matters. The sign flip is NOT idempotent.

Usage:
    uv run python scripts/migrate_bear_returns_dedup.py [--db PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MIGRATION_NAME = "bear_returns_dedup"
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "predictions.db"


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _migrations "
        "(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )


def _already_applied(conn: sqlite3.Connection) -> bool:
    _ensure_migrations_table(conn)
    row = conn.execute(
        "SELECT 1 FROM _migrations WHERE name = ?", (MIGRATION_NAME,)
    ).fetchone()
    return row is not None


def apply_migration(conn: sqlite3.Connection, applied_at: str) -> dict:
    """Apply the sign fix + dedup to an open connection.

    Args:
        conn: SQLite connection to migrate.
        applied_at: ISO timestamp recorded in the ``_migrations`` marker.

    Returns:
        Dict with counts: ``bear_fixed`` (rows sign-flipped) and
        ``duplicates_removed`` (rows deleted).

    Raises:
        RuntimeError: If the migration was already applied to this database.
    """
    if _already_applied(conn):
        raise RuntimeError(
            f"migration '{MIGRATION_NAME}' already applied to this database"
        )

    # All steps run in one transaction so a mid-run failure rolls back cleanly
    # (a partial run that deleted duplicates but did not flip signs would be
    # unsafe to retry).
    try:
        conn.execute("BEGIN")
        # 1a. Collapse OPEN duplicates, keeping the earliest, on the live-guard
        #     key (ticker, market, direction, timeframe, source, created date).
        #     entry_price is excluded so re-runs at a drifted price still count
        #     as the same OPEN call — matching insert_prediction and the partial
        #     UNIQUE index.
        cur = conn.execute(
            """DELETE FROM predictions
               WHERE status = 'OPEN' AND rowid NOT IN (
                   SELECT MIN(rowid) FROM predictions WHERE status = 'OPEN'
                   GROUP BY ticker, market, direction, timeframe, source,
                            date(created_at)
               )"""
        )
        open_removed = cur.rowcount

        # 1b. Collapse only EXACT-duplicate *resolved* rows (same key + entry +
        #     status + outcome). This removes re-run spam while preserving a
        #     legitimate closed-then-reopened sequence the insert guard allows
        #     (those differ in entry/outcome, so they are not exact duplicates).
        cur = conn.execute(
            """DELETE FROM predictions
               WHERE status != 'OPEN' AND rowid NOT IN (
                   SELECT MIN(rowid) FROM predictions WHERE status != 'OPEN'
                   GROUP BY ticker, market, direction, timeframe, source,
                            date(created_at), entry_price, status,
                            outcome_date, outcome_return
               )"""
        )
        resolved_removed = cur.rowcount
        duplicates_removed = open_removed + resolved_removed

        # 2. Flip the sign of every BEAR outcome_return (raw price change ->
        #    position return).
        cur = conn.execute(
            """UPDATE predictions
               SET outcome_return = ROUND(-outcome_return, 2)
               WHERE direction = 'BEAR' AND outcome_return IS NOT NULL"""
        )
        bear_fixed = cur.rowcount

        conn.execute(
            "INSERT INTO _migrations (name, applied_at) VALUES (?, ?)",
            (MIGRATION_NAME, applied_at),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"bear_fixed": bear_fixed, "duplicates_removed": duplicates_removed}


def _backup(db_path: Path) -> Path:
    """Create a consistent backup via SQLite's online backup API.

    Uses ``Connection.backup`` rather than a file copy so any pages still in the
    WAL are included in the snapshot (a plain copy of the main .db file can miss
    uncommitted WAL pages).
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.bak.{ts}_pre_bear_dedup")
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(backup_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report row counts that would change without modifying the DB.",
    )
    args = parser.parse_args()

    if not args.db.exists():
        print(f"error: database not found: {args.db}")
        return 1

    conn = sqlite3.connect(args.db)
    try:
        if _already_applied(conn):
            print(f"migration '{MIGRATION_NAME}' already applied — nothing to do")
            return 0

        before = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        bear_total = conn.execute(
            "SELECT COUNT(*) FROM predictions "
            "WHERE direction = 'BEAR' AND outcome_return IS NOT NULL"
        ).fetchone()[0]

        if args.dry_run:
            print(
                f"[dry-run] rows={before}, BEAR-with-return={bear_total} "
                "(would flip sign); duplicates left unchanged"
            )
            return 0

        backup_path = _backup(args.db)
        print(f"backup written: {backup_path}")

        result = apply_migration(conn, datetime.now(timezone.utc).isoformat())
        after = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        print(
            f"done: rows {before} -> {after} "
            f"(removed {result['duplicates_removed']} duplicates); "
            f"flipped {result['bear_fixed']} BEAR returns"
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
