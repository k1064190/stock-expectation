"""Tests for raw NewsSignal persistence + per-tag performance readout.

Stage 3 of the prediction-accuracy plan: the full ``NewsSignal`` (event tags,
sentiment, hard catalysts) is persisted under ``components.news_signal`` so
future calibration can learn *which* catalyst tags predict, instead of the
dead scalar news score.
"""

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp-prediction-store"))
sys.path.insert(0, str(PROJECT_ROOT / "mcp-market-data"))

import json  # noqa: E402

from metrics import get_component_contribution, get_news_tag_performance  # noqa: E402
from models import get_connection  # noqa: E402
from news_features import NewsSignal  # noqa: E402


# ---------------------------------------------------------------------------
# NewsSignal.to_components_dict
# ---------------------------------------------------------------------------


def test_to_components_dict_roundtrips_key_fields():
    sig = NewsSignal(
        unique_count=4,
        raw_count=6,
        mean_sentiment=0.12,
        recency_weighted_sentiment=0.21,
        event_tags=["earnings", "analyst"],
        has_positive_catalyst=True,
        has_negative_catalyst=False,
    )
    d = sig.to_components_dict()
    assert d == {
        "unique_count": 4,
        "mean_sentiment": 0.12,
        "recency_weighted_sentiment": 0.21,
        "event_tags": ["earnings", "analyst"],
        "has_positive_catalyst": True,
        "has_negative_catalyst": False,
    }
    json.dumps(d)  # JSON-serializable


def test_to_components_dict_handles_none_sentiment():
    sig = NewsSignal(
        unique_count=0,
        raw_count=0,
        mean_sentiment=None,
        recency_weighted_sentiment=None,
    )
    d = sig.to_components_dict()
    assert d["mean_sentiment"] is None
    assert d["event_tags"] == []


# ---------------------------------------------------------------------------
# get_news_tag_performance
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    c = get_connection(path)
    yield c
    c.close()


def _closed_row(conn, i, status, news_signal):
    components = json.dumps({"algo": 5.0, "news_signal": news_signal})
    conn.execute(
        """INSERT INTO predictions
           (id, created_at, ticker, market, direction, confidence, components,
            timeframe, reasoning, entry_price, signals_used, source, status, outcome_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"ns{i}",
            "2026-06-01T00:00:00+00:00",
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
            "2026-06-08T00:00:00+00:00",
        ),
    )
    conn.commit()


def test_tag_performance_aggregates_by_event_tag(conn):
    for i in range(6):
        _closed_row(conn, i, "HIT", {"event_tags": ["earnings"]})
    for i in range(6, 10):
        _closed_row(conn, i, "MISS", {"event_tags": ["earnings"]})
    for i in range(10, 13):
        _closed_row(conn, i, "MISS", {"event_tags": ["ma"]})

    out = get_news_tag_performance(conn, min_count=4)

    assert out["n_with_news_signal"] == 13
    assert out["tags"]["earnings"] == {"n": 10, "wins": 6, "win_rate": 0.6}
    assert "ma" not in out["tags"]  # below min_count


def test_tag_performance_catalyst_buckets(conn):
    for i in range(4):
        _closed_row(conn, i, "MISS", {"has_negative_catalyst": True})
    for i in range(4, 8):
        _closed_row(conn, i, "HIT", {"has_negative_catalyst": False})

    out = get_news_tag_performance(conn, min_count=4)

    assert out["catalysts"]["negative"] == {"n": 4, "wins": 0, "win_rate": 0.0}


def test_tag_performance_empty_db(conn):
    out = get_news_tag_performance(conn, min_count=4)
    assert out == {"n_with_news_signal": 0, "tags": {}, "catalysts": {}}


def test_component_contribution_skips_nested_news_signal(conn):
    """The nested news_signal dict must not pollute pillar buckets."""
    for i in range(10):
        _closed_row(conn, i, "HIT", {"event_tags": ["earnings"]})

    out = get_component_contribution(conn, min_count=8)

    assert "news_signal" not in out["pillars"]
    assert "algo" in out["pillars"]
