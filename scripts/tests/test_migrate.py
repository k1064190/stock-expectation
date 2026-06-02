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
):
    conn.execute(
        "INSERT INTO predictions (id, created_at, ticker, market, direction, "
        "timeframe, source, entry_price, status, outcome_return) "
        "VALUES (?, ?, ?, 'US', ?, ?, ?, ?, ?, ?)",
        (id_, created, ticker, direction, timeframe, source, entry, status, ret),
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


def test_duplicates_removed_keeping_earliest():
    conn = _conn()
    # Three identical-key rows (same ticker/dir/tf/source/day/entry).
    _insert(conn, "d1", "QQQ", "BULL", "1W", 1.0)
    _insert(conn, "d2", "QQQ", "BULL", "1W", 1.0)
    _insert(conn, "d3", "QQQ", "BULL", "1W", 1.0)
    # Different timeframe — NOT a duplicate.
    _insert(conn, "k1", "QQQ", "BULL", "1M", 1.0)
    conn.commit()

    result = apply_migration(conn, "2026-06-02T00:00:00+00:00")

    assert result["duplicates_removed"] == 2
    remaining = {r[0] for r in conn.execute("SELECT id FROM predictions").fetchall()}
    assert "d1" in remaining  # earliest kept
    assert "k1" in remaining  # different timeframe kept
    assert len(remaining) == 2


def test_duplicate_key_excludes_entry_price():
    """Same logical key but different entry_price is still a duplicate (the key
    matches the live insert guard, which excludes entry_price)."""
    conn = _conn()
    _insert(conn, "e1", "TSLA", "BULL", "1W", 1.0, entry=200.0)
    _insert(conn, "e2", "TSLA", "BULL", "1W", 1.0, entry=205.0)  # drifted entry
    conn.commit()

    result = apply_migration(conn, "2026-06-02T00:00:00+00:00")

    assert result["duplicates_removed"] == 1
    remaining = {r[0] for r in conn.execute("SELECT id FROM predictions").fetchall()}
    assert remaining == {"e1"}


def test_migration_is_not_rerunnable():
    conn = _conn()
    _insert(conn, "b1", "INTC", "BEAR", "1W", -12.0)
    conn.commit()
    apply_migration(conn, "2026-06-02T00:00:00+00:00")
    with pytest.raises(RuntimeError, match="already applied"):
        apply_migration(conn, "2026-06-02T00:00:00+00:00")
