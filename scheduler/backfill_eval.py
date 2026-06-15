"""Backfill A/B evaluation harness for prediction-accuracy changes.

Replays proposed accuracy changes against the *closed* (HIT/MISS) prediction
history and reports before/after Brier, win rate, and confidence drift, so a
change can be judged on past data before it is wired into the live ``/expect``
flow.

Why a separate harness: the live calibration tooling
(``metrics.get_track_record`` / ``get_calibration_report``) answers "how are we
doing", not "would change X have helped". This script answers the latter, and
does so leakage-safely — the recalibration A/B fits the isotonic map on an
earlier (train) slice and scores Brier on a later (test) slice, because fitting
and scoring on the same rows reproduces exactly the in-sample optimism that the
LIVE-vs-INTERACTIVE gap (F4) is suspected to be.

Usage:
    uv run python scheduler/backfill_eval.py                  # full report
    uv run python scheduler/backfill_eval.py --source LIVE    # restrict to LIVE rows
    uv run python scheduler/backfill_eval.py --oos-fraction 0.3
    uv run python scheduler/backfill_eval.py --regime-window 2026-06-01:2026-06-07

Read-only: never writes to predictions.db.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp-prediction-store"))

from metrics import (  # noqa: E402
    apply_recalibration,
    build_recalibration_map,
)


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open predictions.db strictly read-only.

    Deliberately bypasses ``models.get_connection`` — that helper runs
    migrations, creates indexes, sets WAL mode, and self-heals duplicate OPEN
    rows, all of which would write to the DB. A backfill must never mutate the
    production history, so we open via a ``mode=ro`` URI instead.

    Args:
        db_path: Path to the existing predictions.db.

    Returns:
        A read-only SQLite connection with row access by column name.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# Signals graded "reversed"/"dead" with 0% hit rate (see plan F2/F5).
DEFAULT_DEAD_SIGNALS = ("valuation", "cycle", "mean_reversion")


@dataclass
class Metrics:
    """Aggregate outcome statistics for a set of closed predictions.

    Args:
        n: Number of closed (HIT/MISS) predictions.
        wins: Count of HIT.
        win_rate: wins / n, or None when n == 0.
        brier: Mean (confidence - outcome)^2 over the set, or None when n == 0.
        avg_return: Mean outcome_return over rows that have one, or None.
    """

    n: int
    wins: int
    win_rate: Optional[float]
    brier: Optional[float]
    avg_return: Optional[float]


def _metrics(
    rows: list[sqlite3.Row], conf_fn: Optional[Callable[[float], float]] = None
) -> Metrics:
    """Compute outcome metrics for ``rows``, optionally remapping confidence.

    Args:
        rows: Closed (HIT/MISS) prediction rows.
        conf_fn: Optional confidence transform applied before computing Brier
            (used by the recalibration A/B). Identity when None.

    Returns:
        Metrics over the supplied rows.
    """
    n = len(rows)
    if n == 0:
        return Metrics(0, 0, None, None, None)
    wins = sum(1 for r in rows if r["status"] == "HIT")
    brier_terms = []
    returns = []
    for r in rows:
        outcome = 1.0 if r["status"] == "HIT" else 0.0
        conf = r["confidence"]
        if conf_fn is not None:
            conf = conf_fn(conf)
        brier_terms.append((conf - outcome) ** 2)
        if r["outcome_return"] is not None:
            returns.append(r["outcome_return"])
    return Metrics(
        n=n,
        wins=wins,
        win_rate=wins / n,
        brier=sum(brier_terms) / len(brier_terms),
        avg_return=(sum(returns) / len(returns)) if returns else None,
    )


def load_closed(
    conn: sqlite3.Connection, source: Optional[str] = None
) -> list[sqlite3.Row]:
    """Load closed predictions, time-ordered for deterministic train/test splits.

    Args:
        conn: SQLite connection to predictions.db.
        source: Optional source filter (LIVE, BACKTEST, INTERACTIVE).

    Returns:
        Rows ordered by (outcome_date, created_at, id) ascending.
    """
    where = ["status IN ('HIT', 'MISS')", "outcome_date IS NOT NULL"]
    params: list = []
    if source:
        where.append("source = ?")
        params.append(source)
    sql = (
        "SELECT id, created_at, confidence, status, outcome_return, source, "
        "timeframe, direction, signals_used FROM predictions WHERE "
        + " AND ".join(where)
        + " ORDER BY outcome_date ASC, created_at ASC, id ASC"
    )
    return conn.execute(sql, params).fetchall()


def _train_map(
    train_rows: list[sqlite3.Row], timeframe: Optional[str]
) -> list[tuple[float, float]]:
    """Fit a recalibration map from train rows only via the production builder.

    Loads the train slice into an in-memory DB and calls
    ``build_recalibration_map`` so the bucketing/isotonic logic is identical to
    what production would apply — no duplicated calibration math.

    Args:
        train_rows: Earlier (train) closed predictions.
        timeframe: Optional timeframe filter passed through to the builder.

    Returns:
        Isotonic (raw_confidence, recalibrated) anchor points.
    """
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    # raw_confidence column is required because get_calibration_report selects
    # COALESCE(raw_confidence, confidence); left NULL here so the fallback uses
    # confidence — correct for historical rows, which predate recalibration.
    mem.execute(
        "CREATE TABLE predictions "
        "(confidence REAL, raw_confidence REAL, status TEXT, source TEXT, timeframe TEXT)"
    )
    mem.executemany(
        "INSERT INTO predictions (confidence, status, source, timeframe) VALUES (?, ?, ?, ?)",
        [
            (r["confidence"], r["status"], r["source"], r["timeframe"])
            for r in train_rows
        ],
    )
    mem.commit()
    return build_recalibration_map(mem, timeframe=timeframe)


def eval_recalibration(
    rows: list[sqlite3.Row],
    oos_fraction: float = 0.3,
    timeframe: Optional[str] = None,
) -> tuple[Metrics, Metrics, int, int]:
    """Leakage-safe A/B for isotonic confidence recalibration (change A).

    Splits time-ordered closed rows into an earlier train slice and a recent
    test slice, fits the recalibration map on train only, then compares raw vs
    recalibrated Brier on the held-out test slice.

    Leakage note: rows are split by resolution order (``outcome_date``), so the
    test rows' outcomes are never in the fitted map. This mirrors how live
    recalibration works — the map is periodically rebuilt from already-resolved
    predictions and applied going forward. It is not strict point-in-time (a
    test prediction created before the split may have resolved after some train
    rows), but that is a second-order effect on a stationary overconfidence
    bias; it does not put test outcomes into the training set.

    Args:
        rows: Time-ordered closed predictions (from ``load_closed``).
        oos_fraction: Fraction placed in the recent test slice. Must be in
            the open interval (0, 1).
        timeframe: Optional timeframe filter for the fitted map. ``None`` fits
            one global map across horizons; pass e.g. "1W" to evaluate the
            per-horizon recalibration that change A may adopt (short/long
            horizons have different base rates).

    Returns:
        (test_raw_metrics, test_recal_metrics, n_train, n_test).
    """
    if not 0.0 < oos_fraction < 1.0:
        raise ValueError(f"oos_fraction must be in (0, 1), got {oos_fraction}")
    n = len(rows)
    if n < 2:
        return _metrics(rows), _metrics(rows), 0, n
    split = min(max(int(n * (1 - oos_fraction)), 1), n - 1)
    train, test = rows[:split], rows[split:]
    recal_map = _train_map(train, timeframe=timeframe)
    raw = _metrics(test)
    recal = _metrics(test, conf_fn=lambda c: apply_recalibration(c, recal_map))
    return raw, recal, len(train), len(test)


def _has_signal(row: sqlite3.Row, signals: tuple[str, ...]) -> bool:
    """Return True when the row's signals_used intersects ``signals``."""
    used = json.loads(row["signals_used"])
    return any(s in used for s in signals)


def eval_dead_signals(
    rows: list[sqlite3.Row], dead: tuple[str, ...] = DEFAULT_DEAD_SIGNALS
) -> tuple[Metrics, Metrics, Metrics]:
    """Quantify the contribution of dead signals (change B).

    The stored history does not keep per-signal point contributions, so the
    composite score cannot be exactly recomputed after dropping a signal. The
    honest backfill measure is therefore a *contribution split*: how do
    predictions that leaned on a dead signal perform versus those that did not.

    Args:
        rows: Closed predictions.
        dead: Signal names to treat as dead.

    Returns:
        (overall, with_dead_signal, without_dead_signal) metrics.
    """
    with_dead = [r for r in rows if _has_signal(r, dead)]
    without = [r for r in rows if not _has_signal(r, dead)]
    return _metrics(rows), _metrics(with_dead), _metrics(without)


def eval_regime_window(
    rows: list[sqlite3.Row], start: str, end: str, direction: str = "BULL"
) -> Metrics:
    """Outcome of directional predictions created inside a risk-off window (change D).

    A hard regime gate would have suppressed new BULL issuance during the
    correction; this reports how those suppressed predictions actually resolved
    (low win rate ⇒ the gate would have helped).

    Args:
        rows: Closed predictions.
        start: Inclusive ISO date (YYYY-MM-DD) on created_at.
        end: Inclusive ISO date (YYYY-MM-DD) on created_at.
        direction: Direction to filter (default BULL).

    Returns:
        Metrics over predictions created in [start, end] with the given direction.
    """
    sel = [
        r
        for r in rows
        if r["direction"] == direction and start <= r["created_at"][:10] <= end
    ]
    return _metrics(sel)


def _fmt(m: Metrics) -> str:
    """Render a Metrics row for the text report."""
    wr = f"{m.win_rate:.1%}" if m.win_rate is not None else "  n/a"
    br = f"{m.brier:.3f}" if m.brier is not None else "  n/a"
    # outcome_return is stored as percentage points (e.g. -11.68 == -11.68%),
    # so format with a literal "%" — do NOT use the "%" format spec, which would
    # multiply by 100 and report -1168%.
    ar = f"{m.avg_return:+.2f}%" if m.avg_return is not None else "    n/a"
    return f"n={m.n:<4d} win={wr:>6s} brier={br:>6s} avg_ret={ar:>9s}"


def build_report(
    conn: sqlite3.Connection,
    source: Optional[str],
    oos_fraction: float,
    regime_window: Optional[tuple[str, str]],
    timeframe: Optional[str] = None,
) -> str:
    """Assemble the full backfill A/B report as text.

    Args:
        conn: SQLite connection to predictions.db.
        source: Optional source filter.
        oos_fraction: Test-slice fraction for the recalibration A/B.
        regime_window: Optional (start, end) ISO dates for the regime slice.
        timeframe: Optional timeframe filter for the recalibration A/B map.

    Returns:
        Multi-section report string.
    """
    rows = load_closed(conn, source=source)
    lines: list[str] = []
    scope = source or "ALL"
    tf = timeframe or "ALL"
    lines.append(
        f"=== Backfill A/B  (source={scope}, timeframe={tf}, closed n={len(rows)}) ===\n"
    )

    # A — recalibration
    raw, recal, n_tr, n_te = eval_recalibration(
        rows, oos_fraction=oos_fraction, timeframe=timeframe
    )
    lines.append(f"[A] Isotonic recalibration  (train={n_tr} / test={n_te}, OOS)")
    if n_tr == 0 or n_te == 0:
        lines.append("    insufficient data for a train/test split\n")
    else:
        lines.append(f"    raw   : {_fmt(raw)}")
        lines.append(f"    recal : {_fmt(recal)}")
        delta = recal.brier - raw.brier
        verdict = "IMPROVED" if delta < 0 else "no improvement"
        lines.append(f"    Brier Δ = {delta:+.3f}  -> {verdict}\n")

    # B — dead-signal contribution
    overall, with_dead, without = eval_dead_signals(rows)
    lines.append(f"[B] Dead-signal contribution  ({', '.join(DEFAULT_DEAD_SIGNALS)})")
    lines.append(f"    overall      : {_fmt(overall)}")
    lines.append(f"    with dead sig: {_fmt(with_dead)}")
    lines.append(f"    without      : {_fmt(without)}\n")

    # D — regime window suppression
    if regime_window:
        start, end = regime_window
        m = eval_regime_window(rows, start, end)
        lines.append(
            f"[D] BULL created in risk-off window {start}..{end} (would be suppressed)"
        )
        lines.append(f"    {_fmt(m)}\n")

    return "\n".join(lines)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    p = argparse.ArgumentParser(description="Backfill A/B harness for accuracy changes")
    p.add_argument(
        "--source", choices=["LIVE", "BACKTEST", "INTERACTIVE"], default=None
    )
    p.add_argument("--oos-fraction", type=float, default=0.3)
    p.add_argument(
        "--timeframe",
        default=None,
        help="Restrict the recalibration A/B to one horizon (e.g. 1W, 1M)",
    )
    p.add_argument(
        "--regime-window",
        default="2026-06-01:2026-06-07",
        help="START:END ISO dates for the regime slice, or 'none' to skip",
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point: print the backfill A/B report."""
    args = parse_args(argv)
    window: Optional[tuple[str, str]] = None
    if args.regime_window and args.regime_window.lower() != "none":
        start, end = args.regime_window.split(":", 1)
        window = (start, end)
    conn = _connect_readonly(PROJECT_ROOT / "data" / "predictions.db")
    try:
        print(
            build_report(
                conn, args.source, args.oos_fraction, window, timeframe=args.timeframe
            )
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
