"""Track record computation and calibration analysis.

Computes win rates, calibration curves, signal attribution, and Brier scores
from the prediction database.
"""

import sqlite3
from dataclasses import dataclass
from typing import Optional


@dataclass
class TrackRecord:
    """Aggregated accuracy statistics.

    Args:
        total: Total closed predictions.
        wins: Number of HIT predictions.
        losses: Number of MISS predictions.
        expired: Number of EXPIRED predictions.
        win_rate: Wins / (wins + losses), or None if no wins+losses.
        avg_return: Average outcome_return across closed predictions.
        current_streak: Positive = consecutive wins, negative = consecutive losses.
        brier_score: Mean squared error of confidence vs outcome (lower is better).
    """

    total: int
    wins: int
    losses: int
    expired: int
    win_rate: Optional[float]
    avg_return: Optional[float]
    current_streak: int
    brier_score: Optional[float]


@dataclass
class CalibrationBucket:
    """A single calibration bucket.

    Args:
        confidence_range: e.g., "0.60-0.70".
        predicted_confidence: Average confidence in this bucket.
        actual_accuracy: Fraction of predictions that were HIT.
        count: Number of predictions in this bucket.
    """

    confidence_range: str
    predicted_confidence: float
    actual_accuracy: float
    count: int


@dataclass
class SignalPerformance:
    """Win rate for a specific analysis signal.

    Args:
        signal: Signal name (e.g., "breadth", "technical").
        total: Number of closed predictions using this signal.
        wins: Number of HIT predictions using this signal.
        win_rate: Wins / total.
    """

    signal: str
    total: int
    wins: int
    win_rate: float


def get_track_record(
    conn: sqlite3.Connection,
    market: Optional[str] = None,
    timeframe: Optional[str] = None,
    source: Optional[str] = None,
    days: int = 30,
) -> TrackRecord:
    """Compute track record statistics for closed predictions.

    Args:
        conn: SQLite connection.
        market: Optional filter by market (US, KR).
        timeframe: Optional filter by timeframe (1W, 2W, 1M, 3M).
        source: Optional filter by source (LIVE, BACKTEST, INTERACTIVE).
        days: Look-back period in days. Defaults to 30.

    Returns:
        TrackRecord with aggregated statistics.
    """
    conditions = ["status IN ('HIT', 'MISS', 'EXPIRED')"]
    params: list = []

    if market:
        conditions.append("market = ?")
        params.append(market)
    if timeframe:
        conditions.append("timeframe = ?")
        params.append(timeframe)
    if source:
        conditions.append("source = ?")
        params.append(source)

    conditions.append("outcome_date >= datetime('now', ?)")
    params.append(f"-{days} days")

    where = " AND ".join(conditions)

    rows = conn.execute(
        f"SELECT status, confidence, outcome_return FROM predictions WHERE {where} ORDER BY outcome_date ASC",
        params,
    ).fetchall()

    if not rows:
        return TrackRecord(
            total=0,
            wins=0,
            losses=0,
            expired=0,
            win_rate=None,
            avg_return=None,
            current_streak=0,
            brier_score=None,
        )

    wins = sum(1 for r in rows if r["status"] == "HIT")
    losses = sum(1 for r in rows if r["status"] == "MISS")
    expired = sum(1 for r in rows if r["status"] == "EXPIRED")
    total = len(rows)

    win_rate = wins / (wins + losses) if (wins + losses) > 0 else None

    returns = [r["outcome_return"] for r in rows if r["outcome_return"] is not None]
    avg_return = sum(returns) / len(returns) if returns else None

    # Current streak (positive = wins, negative = losses)
    streak = 0
    for r in reversed(rows):
        if r["status"] == "EXPIRED":
            continue
        if streak == 0:
            streak = 1 if r["status"] == "HIT" else -1
        elif r["status"] == "HIT" and streak > 0:
            streak += 1
        elif r["status"] == "MISS" and streak < 0:
            streak -= 1
        else:
            break

    # Brier score: mean((confidence - outcome)^2) where outcome=1 for HIT, 0 for MISS
    brier_pairs = []
    for r in rows:
        if r["status"] in ("HIT", "MISS"):
            outcome = 1.0 if r["status"] == "HIT" else 0.0
            brier_pairs.append((r["confidence"] - outcome) ** 2)
    brier_score = sum(brier_pairs) / len(brier_pairs) if brier_pairs else None

    return TrackRecord(
        total=total,
        wins=wins,
        losses=losses,
        expired=expired,
        win_rate=win_rate,
        avg_return=avg_return,
        current_streak=streak,
        brier_score=brier_score,
    )


def get_calibration_report(
    conn: sqlite3.Connection,
    source: Optional[str] = None,
    timeframe: Optional[str] = None,
    buckets: int = 5,
) -> list[CalibrationBucket]:
    """Compute calibration curve: predicted confidence vs actual accuracy.

    Args:
        conn: SQLite connection.
        source: Optional filter (LIVE, BACKTEST, INTERACTIVE).
        timeframe: Optional filter (1W, 2W, 1M, 3M, 6M, 1Y). When set, the
            calibration curve is computed only from predictions with that
            timeframe — useful because short- and long-horizon predictions
            have very different base rates and shouldn't share buckets.
        buckets: Number of confidence buckets. Defaults to 5 (0.5-0.6, ..., 0.9-1.0).

    Returns:
        List of CalibrationBucket, one per confidence range.
    """
    conditions = ["status IN ('HIT', 'MISS')"]
    params: list = []
    if source:
        conditions.append("source = ?")
        params.append(source)
    if timeframe:
        conditions.append("timeframe = ?")
        params.append(timeframe)

    where = " AND ".join(conditions)
    rows = conn.execute(
        f"SELECT confidence, status FROM predictions WHERE {where}",
        params,
    ).fetchall()

    # Create buckets from 0.5 to 1.0
    step = 0.5 / buckets
    result = []
    for i in range(buckets):
        low = 0.5 + i * step
        high = 0.5 + (i + 1) * step
        bucket_rows = [
            r
            for r in rows
            if low <= r["confidence"] < high
            or (i == buckets - 1 and r["confidence"] == high)
        ]
        if not bucket_rows:
            continue
        avg_conf = sum(r["confidence"] for r in bucket_rows) / len(bucket_rows)
        accuracy = sum(1 for r in bucket_rows if r["status"] == "HIT") / len(
            bucket_rows
        )
        result.append(
            CalibrationBucket(
                confidence_range=f"{low:.2f}-{high:.2f}",
                predicted_confidence=round(avg_conf, 3),
                actual_accuracy=round(accuracy, 3),
                count=len(bucket_rows),
            )
        )
    return result


def get_signal_performance(
    conn: sqlite3.Connection,
    min_count: int = 10,
) -> list[SignalPerformance]:
    """Compute win rate per analysis signal.

    Only reports signals with at least `min_count` closed predictions
    to avoid noise.

    Args:
        conn: SQLite connection.
        min_count: Minimum predictions per signal to report. Defaults to 10.

    Returns:
        List of SignalPerformance, sorted by win_rate descending.
    """
    rows = conn.execute(
        "SELECT signals_used, status FROM predictions WHERE status IN ('HIT', 'MISS')"
    ).fetchall()

    import json

    signal_stats: dict[str, dict[str, int]] = {}
    for r in rows:
        signals = json.loads(r["signals_used"])
        for sig in signals:
            if sig not in signal_stats:
                signal_stats[sig] = {"total": 0, "wins": 0}
            signal_stats[sig]["total"] += 1
            if r["status"] == "HIT":
                signal_stats[sig]["wins"] += 1

    result = []
    for sig, stats in signal_stats.items():
        if stats["total"] >= min_count:
            result.append(
                SignalPerformance(
                    signal=sig,
                    total=stats["total"],
                    wins=stats["wins"],
                    win_rate=round(stats["wins"] / stats["total"], 3),
                )
            )

    result.sort(key=lambda x: x.win_rate, reverse=True)
    return result
