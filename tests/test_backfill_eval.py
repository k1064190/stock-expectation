"""Tests for the backfill A/B evaluation harness (scheduler/backfill_eval.py).

Validates the metric math, the time-ordered loader, the leakage-safe
recalibration split, the dead-signal contribution split, and the regime-window
suppression filter — all on small, controlled in-memory datasets.
"""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent

# backfill_eval lives in scheduler/ (not on sys.path); load it by file path.
_spec = importlib.util.spec_from_file_location(
    "backfill_eval", PROJECT_ROOT / "scheduler" / "backfill_eval.py"
)
backfill_eval = importlib.util.module_from_spec(_spec)
sys.modules["backfill_eval"] = backfill_eval
_spec.loader.exec_module(backfill_eval)


def _conn() -> sqlite3.Connection:
    """In-memory DB with the minimal columns the harness reads."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        """CREATE TABLE predictions (
            id TEXT, created_at TEXT, confidence REAL, status TEXT,
            outcome_return REAL, outcome_date TEXT, source TEXT,
            timeframe TEXT, direction TEXT, signals_used TEXT
        )"""
    )
    return c


def _insert(c, rows):
    """Insert dict rows; fills sensible defaults for unspecified columns."""
    for i, r in enumerate(rows):
        c.execute(
            """INSERT INTO predictions
               (id, created_at, confidence, status, outcome_return, outcome_date,
                source, timeframe, direction, signals_used)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r.get("id", f"id{i}"),
                r.get("created_at", "2026-05-01T00:00:00+00:00"),
                r["confidence"],
                r["status"],
                r.get("outcome_return"),
                r.get("outcome_date", f"2026-05-{(i % 27) + 1:02d}T00:00:00+00:00"),
                r.get("source", "LIVE"),
                r.get("timeframe", "1W"),
                r.get("direction", "BULL"),
                json.dumps(r.get("signals_used", ["technical"])),
            ),
        )
    c.commit()


def _row(c, **kw):
    """Convenience: build a single sqlite3.Row matching the harness schema."""
    _insert(c, [kw])
    return c.execute("SELECT * FROM predictions ORDER BY rowid DESC LIMIT 1").fetchone()


# --------------------------------------------------------------------------- #
# _metrics
# --------------------------------------------------------------------------- #
def test_metrics_basic_win_rate_and_brier():
    c = _conn()
    _insert(
        c,
        [
            {"confidence": 1.0, "status": "HIT"},
            {"confidence": 0.0, "status": "MISS"},
        ],
    )
    rows = backfill_eval.load_closed(c)
    m = backfill_eval._metrics(rows)
    assert m.n == 2
    assert m.wins == 1
    assert m.win_rate == 0.5
    assert m.brier == 0.0  # perfectly confident & correct both ways


def test_metrics_empty():
    m = backfill_eval._metrics([])
    assert m.n == 0
    assert m.win_rate is None
    assert m.brier is None


def test_metrics_conf_fn_applied_to_brier():
    c = _conn()
    r = _row(c, confidence=0.9, status="MISS")
    raw = backfill_eval._metrics([r])
    mapped = backfill_eval._metrics([r], conf_fn=lambda _: 0.1)
    assert raw.brier == pytest.approx(0.81)  # (0.9 - 0)^2
    assert mapped.brier == pytest.approx(0.01)  # (0.1 - 0)^2


# --------------------------------------------------------------------------- #
# load_closed
# --------------------------------------------------------------------------- #
def test_load_closed_filters_and_orders():
    c = _conn()
    _insert(
        c,
        [
            {"confidence": 0.6, "status": "OPEN", "outcome_date": None},  # excluded
            {"confidence": 0.6, "status": "EXPIRED"},  # excluded
            {
                "confidence": 0.6,
                "status": "HIT",
                "outcome_date": "2026-05-10T00:00:00+00:00",
            },
            {
                "confidence": 0.6,
                "status": "MISS",
                "outcome_date": "2026-05-05T00:00:00+00:00",
            },
        ],
    )
    rows = backfill_eval.load_closed(c)
    assert [r["status"] for r in rows] == ["MISS", "HIT"]  # ascending by outcome_date


def test_load_closed_source_filter():
    c = _conn()
    _insert(
        c,
        [
            {"confidence": 0.6, "status": "HIT", "source": "LIVE"},
            {"confidence": 0.6, "status": "HIT", "source": "INTERACTIVE"},
        ],
    )
    assert len(backfill_eval.load_closed(c, source="LIVE")) == 1


# --------------------------------------------------------------------------- #
# eval_recalibration — leakage-safe split
# --------------------------------------------------------------------------- #
def test_recalibration_improves_brier_on_overconfident_set():
    c = _conn()
    # Overconfident: confidence pinned at 0.65 but only ~30% actually hit.
    # The map (fit on train) should pull 0.65 toward ~0.30, lowering test Brier.
    rows = []
    for i in range(40):  # train portion (first 70%)
        rows.append(
            {
                "confidence": 0.65,
                "status": "HIT" if i % 10 < 3 else "MISS",
                "outcome_date": f"2026-05-{(i % 27) + 1:02d}T00:00:00+00:00",
                "id": f"tr{i:03d}",
            }
        )
    for i in range(20):  # test portion (recent)
        rows.append(
            {
                "confidence": 0.65,
                "status": "HIT" if i % 10 < 3 else "MISS",
                "outcome_date": f"2026-06-{(i % 27) + 1:02d}T00:00:00+00:00",
                "id": f"te{i:03d}",
            }
        )
    _insert(c, rows)
    closed = backfill_eval.load_closed(c)
    raw, recal, n_tr, n_te = backfill_eval.eval_recalibration(closed, oos_fraction=0.3)
    assert n_tr > 0 and n_te > 0
    assert recal.brier < raw.brier


def test_recalibration_handles_tiny_input():
    raw, recal, n_tr, n_te = backfill_eval.eval_recalibration([], oos_fraction=0.3)
    assert n_tr == 0 and n_te == 0


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.5])
def test_recalibration_rejects_invalid_oos_fraction(bad):
    with pytest.raises(ValueError):
        backfill_eval.eval_recalibration([], oos_fraction=bad)


# --------------------------------------------------------------------------- #
# eval_dead_signals
# --------------------------------------------------------------------------- #
def test_dead_signals_split():
    c = _conn()
    _insert(
        c,
        [
            {
                "confidence": 0.6,
                "status": "MISS",
                "signals_used": ["technical", "cycle"],
            },
            {"confidence": 0.6, "status": "HIT", "signals_used": ["technical", "news"]},
            {"confidence": 0.6, "status": "MISS", "signals_used": ["valuation"]},
        ],
    )
    rows = backfill_eval.load_closed(c)
    overall, with_dead, without = backfill_eval.eval_dead_signals(rows)
    assert overall.n == 3
    assert with_dead.n == 2  # cycle + valuation rows
    assert without.n == 1
    assert without.wins == 1


def test_dead_signals_empty_list_and_custom_set():
    c = _conn()
    _insert(
        c,
        [
            {"confidence": 0.6, "status": "HIT", "signals_used": []},  # no signals
            {"confidence": 0.6, "status": "MISS", "signals_used": ["news"]},
            {
                "confidence": 0.6,
                "status": "MISS",
                "signals_used": ["technical", "cycle"],
            },
        ],
    )
    rows = backfill_eval.load_closed(c)
    # Default dead set: only the cycle row counts as "with dead".
    _, with_default, without_default = backfill_eval.eval_dead_signals(rows)
    assert with_default.n == 1
    assert without_default.n == 2  # empty-signal row must not crash and counts here
    # Custom dead set targeting "news".
    _, with_news, without_news = backfill_eval.eval_dead_signals(rows, dead=("news",))
    assert with_news.n == 1
    assert without_news.n == 2


def test_build_report_smoke():
    c = _conn()
    _insert(
        c,
        [
            {"confidence": 0.65, "status": "HIT" if i % 3 == 0 else "MISS"}
            for i in range(20)
        ],
    )
    report = backfill_eval.build_report(
        c, source=None, oos_fraction=0.3, regime_window=None
    )
    assert "[A] Isotonic recalibration" in report
    assert "[B] Dead-signal contribution" in report


# --------------------------------------------------------------------------- #
# eval_regime_window
# --------------------------------------------------------------------------- #
def test_regime_window_filters_by_created_at_and_direction():
    c = _conn()
    _insert(
        c,
        [
            {
                "confidence": 0.6,
                "status": "MISS",
                "direction": "BULL",
                "created_at": "2026-06-03T00:00:00+00:00",
            },  # in window
            {
                "confidence": 0.6,
                "status": "HIT",
                "direction": "BULL",
                "created_at": "2026-05-20T00:00:00+00:00",
            },  # out of window
            {
                "confidence": 0.6,
                "status": "MISS",
                "direction": "BEAR",
                "created_at": "2026-06-03T00:00:00+00:00",
            },  # wrong direction
        ],
    )
    rows = backfill_eval.load_closed(c)
    m = backfill_eval.eval_regime_window(
        rows, "2026-06-01", "2026-06-07", direction="BULL"
    )
    assert m.n == 1
    assert m.wins == 0
