"""Tests for the V2 migration (bin/migrate_v2.py).

Validates that the migration:
- Preserves row count and row values.
- Adds the ``analysis_group_id`` column, NULL for legacy rows.
- Extends the timeframe CHECK to accept 6M and 1Y.
- Still rejects values outside the enum.
- Is idempotent (second run is a no-op).
"""

import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest


# Load migrate_v2 as a module from bin/ (which has a hyphenated name and
# isn't on sys.path).
PROJECT_ROOT = Path(__file__).parent.parent
MIGRATE_PATH = PROJECT_ROOT / "bin" / "migrate_v2.py"
_spec = importlib.util.spec_from_file_location("migrate_v2", MIGRATE_PATH)
migrate_v2 = importlib.util.module_from_spec(_spec)
sys.modules["migrate_v2"] = migrate_v2
_spec.loader.exec_module(migrate_v2)


# Legacy schema — CHECK limited to 1W/2W/1M/3M, no analysis_group_id column.
LEGACY_SCHEMA = """
CREATE TABLE predictions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    market TEXT NOT NULL CHECK(market IN ('US', 'KR')),
    direction TEXT NOT NULL CHECK(direction IN ('BULL', 'BEAR', 'NEUTRAL')),
    confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
    timeframe TEXT NOT NULL CHECK(timeframe IN ('1W', '2W', '1M', '3M')),
    reasoning TEXT NOT NULL,
    entry_price REAL NOT NULL,
    signals_used TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'LIVE' CHECK(source IN ('LIVE', 'BACKTEST', 'INTERACTIVE')),
    target_price REAL,
    stop_price REAL,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN', 'HIT', 'MISS', 'EXPIRED', 'CANCELLED')),
    outcome_price REAL,
    outcome_date TEXT,
    outcome_return REAL
);
"""


@pytest.fixture
def legacy_db():
    """Create a temp SQLite DB with the legacy (pre-V2) schema and a few rows."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)

    conn = sqlite3.connect(str(db_path))
    conn.executescript(LEGACY_SCHEMA)
    conn.executemany(
        """INSERT INTO predictions
           (id, created_at, ticker, market, direction, confidence, timeframe,
            reasoning, entry_price, signals_used, source, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                "a",
                "2026-01-01",
                "NVDA",
                "US",
                "BULL",
                0.7,
                "1W",
                "t",
                100.0,
                "[]",
                "LIVE",
                "OPEN",
            ),
            (
                "b",
                "2026-01-02",
                "AAPL",
                "US",
                "BEAR",
                0.6,
                "1M",
                "t",
                150.0,
                "[]",
                "LIVE",
                "HIT",
            ),
            (
                "c",
                "2026-01-03",
                "005930",
                "KR",
                "BULL",
                0.65,
                "3M",
                "t",
                70000.0,
                "[]",
                "LIVE",
                "OPEN",
            ),
        ],
    )
    conn.commit()
    conn.close()

    yield db_path

    db_path.unlink(missing_ok=True)
    Path(str(db_path) + "-wal").unlink(missing_ok=True)
    Path(str(db_path) + "-shm").unlink(missing_ok=True)


def test_dry_run_does_not_modify(legacy_db, capsys):
    """--confirm=False prints a plan and leaves the DB untouched."""
    rc = migrate_v2.migrate(legacy_db, confirm=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out

    conn = sqlite3.connect(str(legacy_db))
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(predictions)").fetchall()]
        assert "analysis_group_id" not in cols
        assert conn.execute("SELECT count(*) FROM predictions").fetchone()[0] == 3
    finally:
        conn.close()


def test_migration_preserves_rows_and_adds_column(legacy_db):
    """--confirm migrates rows verbatim and populates legacy analysis_group_id as NULL."""
    rc = migrate_v2.migrate(legacy_db, confirm=True)
    assert rc == 0

    conn = sqlite3.connect(str(legacy_db))
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(predictions)").fetchall()]
        assert "analysis_group_id" in cols

        total, with_group = conn.execute(
            "SELECT count(*), count(analysis_group_id) FROM predictions"
        ).fetchone()
        assert total == 3
        assert with_group == 0  # All legacy rows are NULL for the new column.

        # Row values preserved.
        row_a = conn.execute(
            "SELECT ticker, confidence, timeframe, status FROM predictions WHERE id = 'a'"
        ).fetchone()
        assert row_a == ("NVDA", 0.7, "1W", "OPEN")
    finally:
        conn.close()


def test_migration_accepts_6m_and_1y(legacy_db):
    """After migration, inserting 6M/1Y succeeds."""
    migrate_v2.migrate(legacy_db, confirm=True)

    conn = sqlite3.connect(str(legacy_db))
    try:
        conn.execute(
            """INSERT INTO predictions
               (id, created_at, ticker, market, direction, confidence, timeframe,
                reasoning, entry_price, signals_used, source, status)
               VALUES ('d', '2026-01-04', 'MU', 'US', 'BEAR', 0.65, '1Y',
                       'cycle', 426.56, '[]', 'LIVE', 'OPEN')"""
        )
        conn.execute(
            """INSERT INTO predictions
               (id, created_at, ticker, market, direction, confidence, timeframe,
                reasoning, entry_price, signals_used, source, status)
               VALUES ('e', '2026-01-05', 'MU', 'US', 'NEUTRAL', 0.55, '6M',
                       'long', 426.56, '[]', 'LIVE', 'OPEN')"""
        )
        conn.commit()
        assert conn.execute("SELECT count(*) FROM predictions").fetchone()[0] == 5
    finally:
        conn.close()


def test_migration_still_rejects_invalid_timeframe(legacy_db):
    """CHECK constraint still rejects timeframe values outside the enum."""
    migrate_v2.migrate(legacy_db, confirm=True)

    conn = sqlite3.connect(str(legacy_db))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO predictions
                   (id, created_at, ticker, market, direction, confidence, timeframe,
                    reasoning, entry_price, signals_used, source, status)
                   VALUES ('bad', '2026-01-01', 'AAPL', 'US', 'BULL', 0.5,
                           '5Y', 't', 100.0, '[]', 'LIVE', 'OPEN')"""
            )
    finally:
        conn.close()


def test_migration_idempotent(legacy_db, capsys):
    """Second run is a no-op (schema already current)."""
    migrate_v2.migrate(legacy_db, confirm=True)
    capsys.readouterr()  # discard first run's output

    rc = migrate_v2.migrate(legacy_db, confirm=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "already current" in out.lower() or "no-op" in out.lower()


def test_migration_accepts_analysis_group_id_after(legacy_db):
    """After migration, analysis_group_id is writable."""
    migrate_v2.migrate(legacy_db, confirm=True)

    conn = sqlite3.connect(str(legacy_db))
    try:
        conn.execute(
            """INSERT INTO predictions
               (id, created_at, ticker, market, direction, confidence, timeframe,
                reasoning, entry_price, signals_used, source, status,
                analysis_group_id)
               VALUES ('g', '2026-01-06', 'MU', 'US', 'BULL', 0.72, '1W',
                       'short', 426.56, '[]', 'LIVE', 'OPEN',
                       'group-uuid-abc')"""
        )
        conn.commit()
        val = conn.execute(
            "SELECT analysis_group_id FROM predictions WHERE id = 'g'"
        ).fetchone()[0]
        assert val == "group-uuid-abc"
    finally:
        conn.close()
