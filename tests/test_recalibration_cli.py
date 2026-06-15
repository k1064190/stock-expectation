"""Tests for deterministic confidence recalibration in ``predict create``.

Covers ``stock_cli._recalibrated_confidence``: the minimum-sample gate (below
which recalibration is a no-op) and the actual isotonic mapping (an
overconfident history pulls a raw confidence down toward observed accuracy).
"""

import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# stock_cli.py lives at the project root and sets up the mcp-* sys.path inserts
# at import time; load it by file path so the test doesn't depend on cwd.
_spec = importlib.util.spec_from_file_location(
    "stock_cli", PROJECT_ROOT / "stock_cli.py"
)
stock_cli = importlib.util.module_from_spec(_spec)
sys.modules["stock_cli"] = stock_cli
_spec.loader.exec_module(stock_cli)


@pytest.fixture
def db_path():
    """Path to a fresh temp predictions.db file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        yield Path(f.name)


@pytest.fixture
def db(db_path):
    """Temp predictions.db created via the production schema builder."""
    conn = stock_cli.get_connection(db_path)
    yield conn
    conn.close()


def _seed_closed(conn, *, n, win_fraction, confidence, source="LIVE"):
    """Insert ``n`` closed rows with the given hit fraction and confidence."""
    wins = int(round(n * win_fraction))
    for i in range(n):
        status = "HIT" if i < wins else "MISS"
        conn.execute(
            """INSERT INTO predictions
               (id, created_at, ticker, market, direction, confidence, timeframe,
                reasoning, entry_price, signals_used, source, status, outcome_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"{source}{i}",
                "2026-05-01T00:00:00+00:00",
                "TEST",
                "US",
                "BULL",
                confidence,
                "1W",
                "seed",
                100.0,
                "[]",
                source,
                status,
                "2026-05-02T00:00:00+00:00",
            ),
        )
    conn.commit()


def test_recalibration_noop_below_min_sample(db):
    _seed_closed(db, n=MIN_BELOW, win_fraction=0.3, confidence=0.65)
    out, applied = stock_cli._recalibrated_confidence(db, 0.65, "LIVE")
    assert applied is False
    assert out == 0.65


def test_recalibration_pulls_down_overconfident(db):
    # 60 closed LIVE rows, pinned at 0.65 confidence but only ~30% actually hit.
    _seed_closed(db, n=60, win_fraction=0.3, confidence=0.65)
    out, applied = stock_cli._recalibrated_confidence(db, 0.65, "LIVE")
    assert applied is True
    assert out < 0.65
    assert out == pytest.approx(0.30, abs=0.08)


def test_recalibration_is_source_scoped(db):
    # Plenty of INTERACTIVE history, but the LIVE source is below threshold,
    # so a LIVE prediction must NOT borrow the INTERACTIVE curve.
    _seed_closed(db, n=60, win_fraction=0.3, confidence=0.65, source="INTERACTIVE")
    out, applied = stock_cli._recalibrated_confidence(db, 0.65, "LIVE")
    assert applied is False
    assert out == 0.65


def test_recalibration_noop_when_map_empty_after_gate(db):
    # 30 closed rows (gate passes) but all below 0.5, where calibration buckets
    # start — get_calibration_report returns nothing, so the map is empty.
    _seed_closed(db, n=30, win_fraction=0.3, confidence=0.30)
    out, applied = stock_cli._recalibrated_confidence(db, 0.65, "LIVE")
    assert applied is False
    assert out == 0.65


def test_map_trains_on_raw_not_recalibrated(db):
    """Storing recalibrated values must NOT corrupt the calibration curve."""
    from metrics import build_recalibration_map

    _seed_closed(db, n=60, win_fraction=0.3, confidence=0.65)
    # A closed prediction that was recalibrated: stored confidence 0.30, but its
    # raw_confidence is 0.65. The curve must use the raw 0.65, not the 0.30.
    db.execute(
        """INSERT INTO predictions
           (id, created_at, ticker, market, direction, confidence, raw_confidence,
            timeframe, reasoning, entry_price, signals_used, source, status, outcome_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "recal1",
            "2026-05-03T00:00:00+00:00",
            "X",
            "US",
            "BULL",
            0.30,
            0.65,
            "1W",
            "r",
            100.0,
            "[]",
            "LIVE",
            "MISS",
            "2026-05-04T00:00:00+00:00",
        ),
    )
    db.commit()
    anchors = build_recalibration_map(db, source="LIVE")
    # No spurious low anchor from the stored 0.30 — the map stays in the 0.6 band.
    assert anchors
    assert min(x for x, _ in anchors) >= 0.55


def test_cli_create_recalibrate_persists_raw(db, db_path, monkeypatch):
    """End-to-end predict create --recalibrate: raw kept, confidence calibrated."""
    _seed_closed(db, n=60, win_fraction=0.3, confidence=0.65)
    real_get = stock_cli.get_connection
    monkeypatch.setattr(stock_cli, "get_connection", lambda *a, **k: real_get(db_path))
    args = types.SimpleNamespace(
        ticker="aapl",
        market="US",
        direction="BULL",
        confidence=0.72,
        timeframe="1W",
        entry_price=100.0,
        reasoning="t",
        signals="technical,news",
        source="LIVE",
        target_price=None,
        stop_price=None,
        analysis_group_id=None,
        components=None,
        recalibrate=True,
    )
    rc = stock_cli.cmd_predict_create(args)
    assert rc == 0
    row = db.execute(
        "SELECT confidence, raw_confidence FROM predictions WHERE ticker = 'AAPL'"
    ).fetchone()
    assert row["raw_confidence"] == 0.72
    assert row["confidence"] < 0.72  # recalibrated downward


def _create_args(**over):
    """A predict-create args namespace with valid defaults, overridable."""
    base = dict(
        ticker="aapl",
        market="US",
        direction="BULL",
        confidence=0.65,
        timeframe="1W",
        entry_price=100.0,
        reasoning="t",
        signals="",
        source="INTERACTIVE",
        target_price=None,
        stop_price=None,
        analysis_group_id=None,
        components=None,
        recalibrate=False,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def test_cli_rejects_malformed_components():
    # Bad JSON is rejected before any DB access (returns 1, no insert).
    assert stock_cli.cmd_predict_create(_create_args(components="not-json")) == 1


def test_cli_rejects_non_object_components():
    assert stock_cli.cmd_predict_create(_create_args(components="[1,2,3]")) == 1


def test_cli_rejects_nan_components():
    assert stock_cli.cmd_predict_create(_create_args(components='{"news": NaN}')) == 1


# One below the module threshold, expressed via the constant so the test tracks
# any future change to MIN_CLOSED_FOR_RECAL.
MIN_BELOW = stock_cli.MIN_CLOSED_FOR_RECAL - 1
