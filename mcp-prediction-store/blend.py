"""Learned-blend confidence model (accuracy stage 4a — offline only).

Raw model confidence carries no discrimination (permutation test) and the
isotonic recalibrator collapses everything to the ~0.25 base rate. This module
learns P(HIT) from the per-pillar ``components`` persisted on every prediction
(algo / news / llm_context scores, overextension & regime gates, market,
timeframe) with a hand-rolled L2 logistic regression — no external ML
dependency, matching the hand-rolled isotonic PAV in ``metrics.py``.

Validation is walk-forward (train strictly before, test strictly after) so the
May→July regime drift cannot leak. ``evaluate`` compares the blend against the
raw-confidence and train-fold isotonic baselines on Brier + AUC; the model is
only considered a winner (``blend_wins``) when it beats the isotonic baseline
on Brier AND shows real ranking power (AUC > 0.55).

Nothing in this module touches live confidence — stage 4b wires the winner in
behind a kill switch.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from metrics import _isotonic_nondecreasing

# Feature layout (fixed order). Numeric pillars first, then one-hot gates and
# context. Missing/malformed values become 0.0 — a neutral contribution.
FEATURE_NAMES = [
    "algo",
    "news",
    "llm_context",
    "return_1m",
    "overext_elevated",
    "overext_extreme",
    "regime_neutral",
    "regime_risk_off",
    "market_kr",
    "tf_1m",
    "tf_6m",
]

# Training hyperparameters. L2 is applied per-step WITHOUT the /n average
# (constant strength as data grows); 0.05 with lr 0.1 ≈ 0.5% weight decay per
# step — meaningful shrinkage on the small (~150-row, ~18% positive) sample.
L2_DEFAULT = 0.05
EPOCHS_DEFAULT = 600
LR_DEFAULT = 0.1
MIN_ROWS_DEFAULT = 100  # matches the capstone-readiness threshold
MODEL_VERSION = 1


def _num(v) -> float:
    """Coerce a components value to a finite float, else 0.0."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0.0
    return float(v) if math.isfinite(v) else 0.0


def extract_features(components: dict, market: str, timeframe: str) -> list[float]:
    """Build the fixed-order feature vector for one prediction.

    Args:
        components: Parsed ``predictions.components`` dict (may be partial).
        market: "US" or "KR".
        timeframe: "1W" / "1M" / "6M" (1W is the one-hot base).

    Returns:
        Floats aligned with ``FEATURE_NAMES``; unknown/malformed fields are 0.0.
    """
    overext = components.get("overextension")
    regime = components.get("regime")
    return [
        _num(components.get("algo")),
        _num(components.get("news")),
        _num(components.get("llm_context")),
        _num(components.get("return_1m")),
        1.0 if overext == "ELEVATED" else 0.0,
        1.0 if overext == "EXTREME" else 0.0,
        1.0 if regime == "NEUTRAL" else 0.0,
        1.0 if regime == "RISK_OFF" else 0.0,
        1.0 if market == "KR" else 0.0,
        1.0 if timeframe == "1M" else 0.0,
        1.0 if timeframe == "6M" else 0.0,
    ]


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def fit_logistic(
    X: list[list[float]],
    y: list[int],
    l2: float = L2_DEFAULT,
    epochs: int = EPOCHS_DEFAULT,
    lr: float = LR_DEFAULT,
) -> tuple[list[float], float]:
    """L2-regularized logistic regression via full-batch gradient descent.

    Args:
        X: Feature rows (already scaled to comparable magnitudes).
        y: Binary labels (1 = HIT).
        l2: Ridge penalty on weights (bias unpenalized).
        epochs: Gradient steps (deterministic, no early stopping).
        lr: Learning rate.

    Returns:
        (weights, bias).
    """
    n = len(X)
    d = len(X[0]) if X else 0
    w = [0.0] * d
    b = 0.0
    for _ in range(epochs):
        gw = [0.0] * d
        gb = 0.0
        for xi, yi in zip(X, y):
            p = _sigmoid(sum(wj * xj for wj, xj in zip(w, xi)) + b)
            err = p - yi
            for j in range(d):
                gw[j] += err * xi[j]
            gb += err
        for j in range(d):
            # Penalty NOT divided by n — dividing would let effective
            # regularization decay as the dataset grows (review finding).
            w[j] -= lr * (gw[j] / n + l2 * w[j])
        b -= lr * gb / n
    return w, b


def predict_proba(x: list[float], w: list[float], b: float) -> float:
    """P(HIT) for one feature row under (w, b)."""
    return _sigmoid(sum(wj * xj for wj, xj in zip(w, x)) + b)


def _standardize(
    X: list[list[float]],
) -> tuple[list[list[float]], list[float], list[float]]:
    """Column-wise (x - mean) / std; std 0 → 1 (constant column stays 0)."""
    d = len(X[0])
    n = len(X)
    means = [sum(row[j] for row in X) / n for j in range(d)]
    stds = []
    for j in range(d):
        var = sum((row[j] - means[j]) ** 2 for row in X) / n
        stds.append(math.sqrt(var) or 1.0)
    Xs = [[(row[j] - means[j]) / stds[j] for j in range(d)] for row in X]
    return Xs, means, stds


def _apply_scaling(
    x: list[float], means: list[float], stds: list[float]
) -> list[float]:
    return [(v - m) / s for v, m, s in zip(x, means, stds)]


def brier(preds: list[float], ys: list[int]) -> float:
    return sum((p - t) ** 2 for p, t in zip(preds, ys)) / len(ys)


def auc(preds: list[float], ys: list[int]) -> Optional[float]:
    """Rank-based AUC (probability a random HIT outranks a random MISS).

    Returns:
        AUC in [0, 1], or None when only one class is present.
    """
    pos = [p for p, t in zip(preds, ys) if t == 1]
    neg = [p for p, t in zip(preds, ys) if t == 0]
    if not pos or not neg:
        return None
    wins = 0.0
    for pp in pos:
        for pn in neg:
            if pp > pn:
                wins += 1.0
            elif pp == pn:
                wins += 0.5
    return wins / (len(pos) * len(neg))


def walk_forward_splits(
    n: int, n_folds: int = 3, min_train: int = 50
) -> list[tuple[int, int, int]]:
    """Index splits over time-sorted rows: train [0:train_end), test [start:end).

    Args:
        n: Total rows (already sorted by outcome time).
        n_folds: Desired number of expanding-window folds.
        min_train: Minimum training rows for the first fold.

    Returns:
        List of (train_end, test_start, test_end); empty when n is too small
        to give every fold a non-empty test block after ``min_train``.
    """
    if n < min_train + n_folds:
        return []
    test_total = n - min_train
    fold_size = test_total // n_folds
    if fold_size == 0:
        return []
    splits = []
    for k in range(n_folds):
        test_start = min_train + k * fold_size
        test_end = n if k == n_folds - 1 else test_start + fold_size
        splits.append((test_start, test_start, test_end))
    return splits


def _fetch_rows(conn: sqlite3.Connection) -> list[dict]:
    """Closed LIVE/INTERACTIVE rows with components carrying an algo pillar.

    Sorted by ``created_at`` (prediction time) — the anchor for leak-free
    walk-forward folds. ``outcome_date`` rides along so each fold can restrict
    its training set to rows whose outcomes were already KNOWN when the fold's
    test predictions were made.
    """
    rows = conn.execute(
        "SELECT market, timeframe, status, components, "
        "COALESCE(raw_confidence, confidence) AS raw_conf, "
        "created_at, outcome_date "
        "FROM predictions "
        "WHERE status IN ('HIT', 'MISS') AND components IS NOT NULL "
        "ORDER BY created_at ASC"
    ).fetchall()
    out = []
    for r in rows:
        try:
            comp = json.loads(r["components"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(comp, dict) or "algo" not in comp:
            continue
        out.append(
            {
                "x": extract_features(comp, r["market"], r["timeframe"]),
                "y": 1 if r["status"] == "HIT" else 0,
                "raw_conf": float(r["raw_conf"] or 0.5),
                "created_at": r["created_at"] or "",
                "outcome_date": r["outcome_date"] or "",
            }
        )
    return out


def _isotonic_baseline(
    train_confs: list[float],
    train_ys: list[int],
    test_confs: list[float],
    buckets: int = 5,
) -> list[float]:
    """Train-fold isotonic map over raw confidence, applied to the test fold.

    Mirrors the production recalibrator's shape (bucketed confidence → observed
    hit rate, PAV-projected monotone) but fit only on the fold's training rows
    so the baseline has no lookahead either.
    """
    lo, hi = min(train_confs), max(train_confs)
    span = (hi - lo) or 1.0
    slots: dict[int, list[int]] = {}
    for c, t in zip(train_confs, train_ys):
        k = min(int((c - lo) / span * buckets), buckets - 1)
        slots.setdefault(k, []).append(t)
    points = []
    for k in sorted(slots):
        ys = slots[k]
        center = lo + (k + 0.5) * span / buckets
        points.append((center, sum(ys) / len(ys), len(ys)))
    fitted = _isotonic_nondecreasing(points)
    from metrics import apply_recalibration

    return [apply_recalibration(c, fitted) for c in test_confs]


def evaluate(
    conn: sqlite3.Connection,
    min_rows: int = MIN_ROWS_DEFAULT,
    n_folds: int = 3,
    min_train: int = 50,
) -> dict:
    """Walk-forward comparison: blend vs raw confidence vs isotonic baseline.

    Leak-free by prediction time: rows are folded by ``created_at``, and each
    fold trains ONLY on rows whose ``outcome_date`` precedes the fold's first
    test ``created_at`` — i.e. outcomes that were actually known when the test
    predictions were made. Folds whose known-outcome pool is smaller than
    ``min_train`` are skipped.

    Args:
        conn: SQLite connection.
        min_rows: Minimum components-tagged closed rows to attempt CV.
        n_folds: Expanding-window folds (by created_at order).
        min_train: Minimum known-outcome training rows per fold.

    Returns:
        Dict with n_rows, folds, per-model {brier, auc} (pooled) plus
        ``auc_fold_mean`` per model (regime shifts between folds can distort
        pooled AUC), ``blend_wins`` (blend beats isotonic on pooled Brier AND
        pooled blend AUC > 0.55), and status.
    """
    rows = _fetch_rows(conn)
    n = len(rows)
    result = {
        "n_rows": n,
        "folds": 0,
        "blend": None,
        "raw": None,
        "isotonic": None,
        "blend_wins": False,
        "status": "ok",
    }
    if n < min_rows:
        result["status"] = f"insufficient rows ({n} < {min_rows})"
        return result
    splits = walk_forward_splits(n, n_folds=n_folds, min_train=min_train)
    if not splits:
        result["status"] = "insufficient rows for walk-forward folds"
        return result

    preds_b: list[float] = []
    preds_r: list[float] = []
    preds_i: list[float] = []
    ys: list[int] = []
    fold_aucs: dict[str, list[float]] = {"blend": [], "raw": [], "isotonic": []}
    folds_run = 0
    for _, test_start, test_end in splits:
        test = rows[test_start:test_end]
        if not test:
            continue
        # Known-outcome training pool at the moment the fold's first test
        # prediction was created — never rows still open at that time.
        cutoff = test[0]["created_at"]
        train = [r for r in rows if r["outcome_date"] and r["outcome_date"] < cutoff]
        if len(train) < min_train:
            continue
        folds_run += 1
        Xs, means, stds = _standardize([r["x"] for r in train])
        w, b = fit_logistic(Xs, [r["y"] for r in train])
        f_b = [predict_proba(_apply_scaling(r["x"], means, stds), w, b) for r in test]
        f_r = [r["raw_conf"] for r in test]
        f_i = _isotonic_baseline(
            [r["raw_conf"] for r in train],
            [r["y"] for r in train],
            f_r,
        )
        f_y = [r["y"] for r in test]
        preds_b.extend(f_b)
        preds_r.extend(f_r)
        preds_i.extend(f_i)
        ys.extend(f_y)
        for name, fp in (("blend", f_b), ("raw", f_r), ("isotonic", f_i)):
            fa = auc(fp, f_y)
            if fa is not None:
                fold_aucs[name].append(fa)

    result["folds"] = folds_run
    if not ys:
        result["status"] = "no folds had enough known outcomes to train on"
        return result

    def _fold_mean(name: str):
        vals = fold_aucs[name]
        return round(sum(vals) / len(vals), 4) if vals else None

    result["blend"] = {
        "brier": round(brier(preds_b, ys), 4),
        "auc": auc(preds_b, ys),
        "auc_fold_mean": _fold_mean("blend"),
    }
    result["raw"] = {
        "brier": round(brier(preds_r, ys), 4),
        "auc": auc(preds_r, ys),
        "auc_fold_mean": _fold_mean("raw"),
    }
    result["isotonic"] = {
        "brier": round(brier(preds_i, ys), 4),
        "auc": auc(preds_i, ys),
        "auc_fold_mean": _fold_mean("isotonic"),
    }
    blend_auc = result["blend"]["auc"]
    result["blend_wins"] = bool(
        result["blend"]["brier"] < result["isotonic"]["brier"]
        and blend_auc is not None
        and blend_auc > 0.55
    )
    return result


def train_full(
    conn: sqlite3.Connection, min_rows: int = MIN_ROWS_DEFAULT
) -> Optional[dict]:
    """Train on ALL components-tagged closed rows; return a model dict.

    Returns:
        Model dict (weights, bias, scaling, metadata) or None below min_rows.
        Callers should persist it only when ``evaluate`` reports a CV win —
        that policy lives in stage 4b, not here.
    """
    rows = _fetch_rows(conn)
    if len(rows) < min_rows:
        return None
    Xs, means, stds = _standardize([r["x"] for r in rows])
    w, b = fit_logistic(Xs, [r["y"] for r in rows])
    return {
        "version": MODEL_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "weights": w,
        "bias": b,
        "means": means,
        "stds": stds,
        "n_train": len(rows),
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }


def predict_with_model(model: dict, x: list[float]) -> float:
    """P(HIT) for a raw (unscaled) feature vector under a saved model."""
    xs = _apply_scaling(x, model["means"], model["stds"])
    return predict_proba(xs, model["weights"], model["bias"])


def save_model(model: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model, indent=2))


def load_model(path: Path) -> Optional[dict]:
    """Load a saved model; None when absent/corrupt/wrong version."""
    try:
        model = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(model, dict)
        or model.get("version") != MODEL_VERSION
        or model.get("feature_names") != FEATURE_NAMES
    ):
        return None
    return model
