"""Tests for the one-time BEAR-sign + dedup migration."""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from migrate_bear_returns_dedup import apply_migration  # noqa: E402

_SCHEMA = """
CREATE TABLE predictions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    market TEXT NOT NULL,
    direction TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    source TEXT NOT NULL,
    entry_price REAL NOT NULL,
    status TEXT NOT NULL,
    outcome_date TEXT,
    outcome_return REAL
)
"""


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _insert(
    conn,
    id_,
    ticker,
    direction,
    timeframe,
    ret,
    *,
    status="HIT",
    created="2026-05-18T00:00:00+00:00",
    source="LIVE",
    entry=100.0,
    outcome_date="2026-05-25T00:00:00+00:00",
):
    conn.execute(
        "INSERT INTO predictions (id, created_at, ticker, market, direction, "
        "timeframe, source, entry_price, status, outcome_date, outcome_return) "
        "VALUES (?, ?, ?, 'US', ?, ?, ?, ?, ?, ?, ?)",
        (
            id_,
            created,
            ticker,
            direction,
            timeframe,
            source,
            entry,
            status,
            outcome_date,
            ret,
        ),
    )


def test_bear_sign_is_flipped():
    conn = _conn()
    _insert(conn, "b1", "INTC", "BEAR", "1W", -12.0)  # winning short stored neg
    _insert(conn, "b2", "AMD", "BEAR", "1M", 6.0)  # losing short stored pos
    conn.commit()

    apply_migration(conn, "2026-06-02T00:00:00+00:00")

    assert (
        conn.execute("SELECT outcome_return FROM predictions WHERE id='b1'").fetchone()[
            0
        ]
        == 12.0
    )
    assert (
        conn.execute("SELECT outcome_return FROM predictions WHERE id='b2'").fetchone()[
            0
        ]
        == -6.0
    )


def test_bull_return_unchanged():
    conn = _conn()
    _insert(conn, "u1", "NVDA", "BULL", "1W", 5.0)
    conn.commit()
    apply_migration(conn, "2026-06-02T00:00:00+00:00")
    assert (
        conn.execute("SELECT outcome_return FROM predictions WHERE id='u1'").fetchone()[
            0
        ]
        == 5.0
    )


def test_open_duplicates_removed_by_6_field_key():
    """OPEN spam collapses on the 6-field key (entry_price excluded), matching
    the live insert guard; a different timeframe is kept."""
    conn = _conn()
    # Three OPEN rows, same 6-key, even at drifted entry prices.
    _insert(conn, "d1", "QQQ", "BULL", "1W", None, status="OPEN", entry=100.0)
    _insert(conn, "d2", "QQQ", "BULL", "1W", None, status="OPEN", entry=101.0)
    _insert(conn, "d3", "QQQ", "BULL", "1W", None, status="OPEN", entry=102.0)
    # Different timeframe — NOT a duplicate.
    _insert(conn, "k1", "QQQ", "BULL", "1M", None, status="OPEN")
    conn.commit()

    result = apply_migration(conn, "2026-06-02T00:00:00+00:00")

    assert result["duplicates_removed"] == 2
    remaining = {r[0] for r in conn.execute("SELECT id FROM predictions").fetchall()}
    assert remaining == {"d1", "k1"}  # earliest OPEN + different timeframe


def test_resolved_exact_duplicates_removed():
    """Identical resolved rows (same entry + outcome) are re-run spam and
    collapse to the earliest."""
    conn = _conn()
    _insert(conn, "r1", "AMD", "BULL", "1W", 5.0, status="HIT", entry=100.0)
    _insert(conn, "r2", "AMD", "BULL", "1W", 5.0, status="HIT", entry=100.0)
    _insert(conn, "r3", "AMD", "BULL", "1W", 5.0, status="HIT", entry=100.0)
    conn.commit()

    result = apply_migration(conn, "2026-06-02T00:00:00+00:00")

    assert result["duplicates_removed"] == 2
    remaining = {r[0] for r in conn.execute("SELECT id FROM predictions").fetchall()}
    assert remaining == {"r1"}


def test_resolved_spam_differing_only_by_outcome_date_collapses():
    """Re-run spam that was closed separately differs only by outcome_date;
    it must still collapse (outcome_date is excluded from the resolved key)."""
    conn = _conn()
    _insert(
        conn,
        "s1",
        "AMD",
        "BULL",
        "1W",
        5.0,
        status="HIT",
        entry=100.0,
        outcome_date="2026-05-25T01:00:00+00:00",
    )
    _insert(
        conn,
        "s2",
        "AMD",
        "BULL",
        "1W",
        5.0,
        status="HIT",
        entry=100.0,
        outcome_date="2026-05-25T02:30:00+00:00",  # closed at a different time
    )
    conn.commit()

    result = apply_migration(conn, "2026-06-02T00:00:00+00:00")

    assert result["duplicates_removed"] == 1
    remaining = {r[0] for r in conn.execute("SELECT id FROM predictions").fetchall()}
    assert remaining == {"s1"}


def test_closed_then_reopened_is_preserved():
    """A legitimate closed row + a later OPEN row on the same day/key are both
    kept (the insert guard allows reopening after close)."""
    conn = _conn()
    _insert(conn, "c1", "MU", "BULL", "1W", 5.0, status="HIT", entry=100.0)
    _insert(conn, "c2", "MU", "BULL", "1W", None, status="OPEN", entry=104.0)
    conn.commit()

    result = apply_migration(conn, "2026-06-02T00:00:00+00:00")

    assert result["duplicates_removed"] == 0
    remaining = {r[0] for r in conn.execute("SELECT id FROM predictions").fetchall()}
    assert remaining == {"c1", "c2"}


def test_distinct_resolved_predictions_preserved():
    """Two resolved rows with the same 6-key but different entry/outcome are
    distinct predictions, not exact duplicates — both kept."""
    conn = _conn()
    _insert(conn, "p1", "TSLA", "BULL", "1W", 5.0, status="HIT", entry=200.0)
    _insert(conn, "p2", "TSLA", "BULL", "1W", -3.0, status="MISS", entry=205.0)
    conn.commit()

    result = apply_migration(conn, "2026-06-02T00:00:00+00:00")

    assert result["duplicates_removed"] == 0
    remaining = {r[0] for r in conn.execute("SELECT id FROM predictions").fetchall()}
    assert remaining == {"p1", "p2"}


def test_migration_is_not_rerunnable():
    conn = _conn()
    _insert(conn, "b1", "INTC", "BEAR", "1W", -12.0)
    conn.commit()
    apply_migration(conn, "2026-06-02T00:00:00+00:00")
    with pytest.raises(RuntimeError, match="already applied"):
        apply_migration(conn, "2026-06-02T00:00:00+00:00")
