"""Tests for the learned-blend confidence model (accuracy stage 4a).

Hand-rolled L2 logistic regression over per-pillar ``components`` features,
walk-forward CV against raw-confidence and isotonic baselines, JSON model
persistence. Offline only — no live confidence change in this stage.
"""

import json
import math
import random
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp-prediction-store"))

import blend  # noqa: E402
from models import get_connection  # noqa: E402


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------


def test_extract_features_full_row():
    comp = {
        "algo": 6.5,
        "news": 1.0,
        "llm_context": -1.5,
        "return_1m": 0.08,
        "overextension": "ELEVATED",
        "regime": "NEUTRAL",
    }
    x = blend.extract_features(comp, market="KR", timeframe="1M")
    assert len(x) == len(blend.FEATURE_NAMES)
    named = dict(zip(blend.FEATURE_NAMES, x))
    assert named["algo"] == 6.5
    assert named["overext_elevated"] == 1.0
    assert named["overext_extreme"] == 0.0
    assert named["regime_neutral"] == 1.0
    assert named["market_kr"] == 1.0
    assert named["tf_1m"] == 1.0
    assert named["tf_6m"] == 0.0


def test_extract_features_missing_fields_default_zero():
    x = blend.extract_features({}, market="US", timeframe="1W")
    assert all(v == 0.0 for v in x)


def test_extract_features_ignores_bad_types():
    comp = {"algo": "seven", "return_1m": None, "overextension": 3}
    x = blend.extract_features(comp, market="US", timeframe="1W")
    assert all(v == 0.0 for v in x)


# ---------------------------------------------------------------------------
# Logistic regression core
# ---------------------------------------------------------------------------


def _synthetic(n=400, seed=7):
    """Labels driven by one strong feature; others noise."""
    rng = random.Random(seed)
    X, y = [], []
    for _ in range(n):
        a = rng.uniform(-2, 2)
        noise = rng.uniform(-1, 1)
        p = 1.0 / (1.0 + math.exp(-(2.0 * a)))
        X.append([a, noise])
        y.append(1 if rng.random() < p else 0)
    return X, y


def test_logistic_fit_recovers_signal():
    X, y = _synthetic()
    w, b = blend.fit_logistic(X, y, l2=0.01, epochs=800, lr=0.1)
    assert w[0] > 0.8  # signal feature clearly positive
    assert abs(w[1]) < abs(w[0]) / 2  # noise feature much smaller

    preds = [blend.predict_proba(x, w, b) for x in X]
    brier = sum((p - t) ** 2 for p, t in zip(preds, y)) / len(y)
    base = sum(y) / len(y)
    brier_base = sum((base - t) ** 2 for t in y) / len(y)
    assert brier < brier_base  # beats predicting the base rate


def test_strong_l2_shrinks_weights():
    X, y = _synthetic()
    w_soft, _ = blend.fit_logistic(X, y, l2=0.01, epochs=400, lr=0.1)
    w_hard, _ = blend.fit_logistic(X, y, l2=10.0, epochs=400, lr=0.1)
    assert abs(w_hard[0]) < abs(w_soft[0])


def test_auc_known_values():
    # Perfect separation → 1.0; anti-separation → 0.0; ties → 0.5.
    assert blend.auc([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0]) == 1.0
    assert blend.auc([0.1, 0.2, 0.8, 0.9], [1, 1, 0, 0]) == 0.0
    assert blend.auc([0.5, 0.5, 0.5, 0.5], [1, 1, 0, 0]) == 0.5
    assert blend.auc([0.9], [1]) is None  # one class only


# ---------------------------------------------------------------------------
# Walk-forward CV (no leakage)
# ---------------------------------------------------------------------------


def test_walk_forward_splits_are_time_ordered():
    rows = [{"t": f"2026-06-{d:02d}"} for d in range(1, 31)]
    splits = blend.walk_forward_splits(len(rows), n_folds=3, min_train=10)
    assert splits, "expected at least one fold"
    for train_end, test_start, test_end in splits:
        assert train_end == test_start  # test begins where train ends
        assert test_end > test_start
        assert train_end >= 10  # min_train respected
    # folds cover strictly later data as they advance
    starts = [s for _, s, _ in splits]
    assert starts == sorted(starts)


def test_walk_forward_too_few_rows():
    assert blend.walk_forward_splits(20, n_folds=3, min_train=50) == []


# ---------------------------------------------------------------------------
# evaluate() end-to-end on a fixture DB
# ---------------------------------------------------------------------------


@pytest.fixture
def conn():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    c = get_connection(path)
    yield c
    c.close()
    path.unlink(missing_ok=True)


def _row(conn, i, status, algo, created_day, outcome_day, raw_conf=0.6):
    comp = json.dumps(
        {"algo": algo, "news": 0.0, "llm_context": 0.0, "overextension": "NONE"}
    )
    conn.execute(
        """INSERT INTO predictions
           (id, created_at, ticker, market, direction, confidence, raw_confidence,
            components, timeframe, reasoning, entry_price, signals_used, source,
            status, outcome_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"b{i}",
            f"2026-05-{created_day:02d}T00:00:00+00:00",
            "T",
            "US",
            "BULL",
            raw_conf,
            raw_conf,
            comp,
            "1W",
            "r",
            100.0,
            "[]",
            "LIVE",
            status,
            f"2026-05-{outcome_day:02d}T12:00:00+00:00",
        ),
    )
    conn.commit()


def _seed_learnable(conn, n=160):
    """algo>=6 wins 70%, algo<=3 wins 15% — a learnable, stationary pattern.

    ~6 predictions created per day, each resolving 3 days later, so later
    walk-forward folds have a growing pool of already-known outcomes.
    """
    rng = random.Random(3)
    for i in range(n):
        hi = i % 2 == 0
        algo = 7.0 if hi else 2.0
        win = rng.random() < (0.70 if hi else 0.15)
        created = min(i // 6 + 1, 26)
        _row(conn, i, "HIT" if win else "MISS", algo, created, created + 3)


def test_evaluate_learnable_pattern_beats_baselines(conn):
    _seed_learnable(conn)
    result = blend.evaluate(conn, min_rows=100, min_train=30)

    assert result["n_rows"] == 160
    assert result["folds"] >= 2
    assert result["blend"]["brier"] < result["raw"]["brier"]
    assert result["blend"]["auc"] > 0.65
    assert result["blend"]["auc_fold_mean"] > 0.65
    assert isinstance(result["blend_wins"], bool)
    assert result["blend_wins"] is True


def test_evaluate_trains_only_on_known_outcomes(conn):
    """Rows still open at a fold's first test created_at never train that fold.

    All outcomes resolve AFTER every prediction was created → no fold can have
    a known-outcome training pool → evaluate reports zero usable folds instead
    of silently leaking future outcomes.
    """
    for i in range(120):
        _row(conn, 500 + i, "HIT" if i % 3 == 0 else "MISS", 5.0, 1, 28)

    result = blend.evaluate(conn, min_rows=100, min_train=30)

    assert result["folds"] == 0
    assert "known outcomes" in result["status"]


def test_evaluate_below_min_rows(conn):
    _seed_learnable(conn, n=40)
    result = blend.evaluate(conn, min_rows=100)
    assert result["blend_wins"] is False
    assert "insufficient" in result["status"]


# ---------------------------------------------------------------------------
# Model persistence
# ---------------------------------------------------------------------------


def test_train_save_load_roundtrip(conn, tmp_path):
    _seed_learnable(conn)
    model_path = tmp_path / "blend_model.json"
    model = blend.train_full(conn, min_rows=100)
    assert model is not None
    blend.save_model(model, model_path)

    loaded = blend.load_model(model_path)
    assert loaded["feature_names"] == blend.FEATURE_NAMES
    x = blend.extract_features(
        {"algo": 7.0, "overextension": "NONE"}, market="US", timeframe="1W"
    )
    p1 = blend.predict_with_model(model, x)
    p2 = blend.predict_with_model(loaded, x)
    assert p1 == pytest.approx(p2)
    assert 0.0 < p1 < 1.0
    # high-algo should score above low-algo under the learned pattern
    x_low = blend.extract_features(
        {"algo": 2.0, "overextension": "NONE"}, market="US", timeframe="1W"
    )
    assert p1 > blend.predict_with_model(model, x_low)


def test_load_model_missing_file(tmp_path):
    assert blend.load_model(tmp_path / "nope.json") is None
