"""Tests for per-pillar component persistence + contribution readout (H1)."""

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp-prediction-store"))

from metrics import get_component_contribution  # noqa: E402
from models import (  # noqa: E402
    Prediction,
    get_connection,
    get_prediction,
    insert_prediction,
)


@pytest.fixture
def conn():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    c = get_connection(path)
    yield c
    c.close()


def _closed_row(conn, *, status, components, i):
    """Insert a closed (HIT/MISS) prediction carrying components JSON."""
    conn.execute(
        """INSERT INTO predictions
           (id, created_at, ticker, market, direction, confidence, components,
            timeframe, reasoning, entry_price, signals_used, source, status, outcome_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"id{i}",
            "2026-05-01T00:00:00+00:00",
            "T",
            "US",
            "BULL",
            0.6,
            components,
            "1W",
            "r",
            100.0,
            "[]",
            "LIVE",
            status,
            "2026-05-02T00:00:00+00:00",
        ),
    )
    conn.commit()


def test_components_roundtrip(conn):
    comps = {
        "algo": 7.0,
        "news": 1.0,
        "llm_context": -1.5,
        "overextension": "NONE",
        "regime": "RISK_ON",
    }
    pred = Prediction(
        ticker="NVDA",
        market="US",
        direction="BULL",
        confidence=0.6,
        timeframe="1W",
        reasoning="r",
        entry_price=100.0,
        source="INTERACTIVE",
        components=comps,
    )
    insert_prediction(conn, pred)
    fetched = get_prediction(conn, pred.id)
    assert fetched.components == comps


def test_components_none_roundtrips_as_none(conn):
    pred = Prediction(
        ticker="NVDA",
        market="US",
        direction="BULL",
        confidence=0.6,
        timeframe="1W",
        reasoning="r",
        entry_price=100.0,
        source="INTERACTIVE",
    )
    insert_prediction(conn, pred)
    assert get_prediction(conn, pred.id).components is None


def test_migration_adds_components_column(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(predictions)")}
    assert "components" in cols


def test_component_contribution_splits_numeric_and_categorical(conn):
    import json

    # 10 rows with positive news → mostly HIT; 10 with non-positive news → mostly MISS.
    for i in range(10):
        _closed_row(
            conn,
            status="HIT" if i < 8 else "MISS",
            components=json.dumps({"news": 2.0, "regime": "RISK_ON"}),
            i=i,
        )
    for i in range(10, 20):
        _closed_row(
            conn,
            status="HIT" if i < 12 else "MISS",
            components=json.dumps({"news": -1.0, "regime": "RISK_OFF"}),
            i=i,
        )
    out = get_component_contribution(conn, min_count=5)
    assert out["n_with_components"] == 20
    news = out["pillars"]["news"]
    assert news["positive"]["win_rate"] == 0.8
    assert news["negative"]["win_rate"] == 0.2
    # categorical regime split present
    assert "RISK_ON" in out["pillars"]["regime"]


def test_component_contribution_zero_is_own_bucket(conn):
    import json

    for i in range(8):
        _closed_row(conn, status="HIT", components=json.dumps({"news": 0.0}), i=i)
    out = get_component_contribution(conn, min_count=5)
    assert "zero" in out["pillars"]["news"]


def test_component_contribution_skips_bad_json(conn):
    # A row with non-dict / bad JSON must not be counted as valid.
    _closed_row(conn, status="HIT", components="not json", i=99)
    out = get_component_contribution(conn)
    assert out["n_with_components"] == 0


def test_component_contribution_empty_when_no_components(conn):
    out = get_component_contribution(conn)
    assert out == {"n_with_components": 0, "pillars": {}}


def test_full_swap_migration_preserves_additive_columns():
    """A drifted legacy DB (no analysis_group_id) that already carries
    raw_confidence + components must NOT lose them through the full-swap
    migration — the copy must use the old/new column intersection."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    # Legacy schema: pre-v2 timeframe CHECK, NO analysis_group_id (forces the
    # full-swap migration), but WITH the additive columns present.
    raw = sqlite3.connect(str(path))
    raw.executescript(
        """
        CREATE TABLE predictions (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL, ticker TEXT NOT NULL,
            market TEXT NOT NULL, direction TEXT NOT NULL, confidence REAL NOT NULL,
            raw_confidence REAL, components TEXT,
            timeframe TEXT NOT NULL CHECK(timeframe IN ('1W','2W','1M','3M')),
            reasoning TEXT NOT NULL, entry_price REAL NOT NULL,
            signals_used TEXT NOT NULL DEFAULT '[]',
            source TEXT NOT NULL DEFAULT 'LIVE',
            target_price REAL, stop_price REAL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            outcome_price REAL, outcome_date TEXT, outcome_return REAL
        );
        """
    )
    raw.execute(
        "INSERT INTO predictions (id, created_at, ticker, market, direction, "
        "confidence, raw_confidence, components, timeframe, reasoning, entry_price) "
        "VALUES ('x','2026-01-01','NVDA','US','BULL',0.25,0.61,'{\"algo\": 7.0}','1W','r',100.0)"
    )
    raw.commit()
    raw.close()

    conn = get_connection(path)  # triggers full-swap migration
    try:
        row = conn.execute(
            "SELECT raw_confidence, components FROM predictions WHERE id='x'"
        ).fetchone()
        assert row["raw_confidence"] == 0.61  # preserved, not dropped
        assert row["components"] == '{"algo": 7.0}'
    finally:
        conn.close()
