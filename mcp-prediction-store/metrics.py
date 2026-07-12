"""Track record computation and calibration analysis.

Computes win rates, calibration curves, signal attribution, and Brier scores
from the prediction database.
"""

import random
import sqlite3
from dataclasses import dataclass
from math import ceil, exp, lgamma, log
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
        p_value: Two-sided exact-binomial p-value of the win count against a
            50% coin-flip null. Low p_value = the signal's hit rate is unlikely
            to be chance.
        verdict: Significance verdict against the 50% null:
            "alive" (significantly > 50%), "dead" (significantly < 50% —
            an anti-signal to prune or invert), or "weak" (not statistically
            distinguishable from a coin flip).
    """

    signal: str
    total: int
    wins: int
    win_rate: float
    p_value: float = 1.0
    verdict: str = "weak"


@dataclass
class SignalDecay:
    """Out-of-sample stability grade for an analysis signal.

    Splits a signal's closed predictions by ``outcome_date`` into an earlier
    "train" window and a recent "test" window, grades each side against the 50%
    null, and combines them into a decay label. Adapted from Vibe-Trading's
    strict factor gate (bench_runner_strict.py) — a signal that worked in-sample
    but decays out-of-sample is the main thing a static all-history win rate
    hides.

    Args:
        signal: Signal name.
        train_total/train_wins: Closed count / HIT count in the earlier window.
        test_total/test_wins: Closed count / HIT count in the recent window.
        train_verdict/test_verdict: Per-window verdict (alive/weak/dead).
        label: Combined grade — "confirmed_alive" (alive in both),
            "train_only" (alive in train, only a coin flip recently — decaying),
            "reversed" (significant loser out-of-sample), or "noise".
    """

    signal: str
    train_total: int
    train_wins: int
    test_total: int
    test_wins: int
    train_verdict: str
    test_verdict: str
    label: str


def _binomial_two_sided_p(k: int, n: int, p: float = 0.5) -> float:
    """Exact two-sided binomial p-value for k successes in n trials.

    Sums the probability of all outcomes no more likely than the observed count
    under Binomial(n, p) (the minimum-likelihood method). Computed in log space
    via ``math.lgamma`` so it stays stable as the track record grows: a direct
    ``comb(n, i) * p**i * ...`` overflows when converting the huge combination
    to float (e.g. n ~ 1100). Each individual pmf is <= 1, so summing the
    exponentials never overflows; negligible tail terms underflow harmlessly.

    Args:
        k: Observed number of successes (HITs).
        n: Number of trials (HIT + MISS).
        p: Null success probability in (0, 1). Defaults to 0.5 (coin flip).

    Returns:
        Two-sided p-value in [0.0, 1.0]; 1.0 when n == 0 or k is out of the
        valid [0, n] range (an impossible count carries no evidence).
    """
    if n == 0 or k < 0 or k > n:
        return 1.0

    log_n_fact = lgamma(n + 1)
    log_p = log(p)
    log_q = log(1 - p)

    def _log_pmf(i: int) -> float:
        return (
            log_n_fact - lgamma(i + 1) - lgamma(n - i + 1) + i * log_p + (n - i) * log_q
        )

    # Include every outcome no more likely than the observed one. The small
    # additive slack in log space (~ a relative factor of 1+1e-9) keeps
    # symmetric ties (e.g. the mirror outcome under p=0.5) in the sum.
    observed_log = _log_pmf(k)
    threshold = observed_log + 1e-9
    total = sum(exp(lp) for i in range(n + 1) if (lp := _log_pmf(i)) <= threshold)
    return min(1.0, total)


def _signal_verdict(wins: int, total: int, alpha: float = 0.05) -> tuple[float, str]:
    """Classify a signal's hit rate against a 50% coin-flip null.

    Adapts Vibe-Trading's ``categorise()`` (bench_runner.py:46-55) to our
    sparse single-ticker win/loss data: instead of an IC t-stat we use an
    exact binomial test of the HIT count against 0.5.

    Args:
        wins: HIT count for the signal.
        total: HIT + MISS count for the signal.
        alpha: Significance threshold. Defaults to 0.05.

    Returns:
        Tuple of (p_value, verdict) where verdict is "alive", "dead", or "weak".
    """
    p_value = _binomial_two_sided_p(wins, total)
    if total == 0:
        return p_value, "weak"
    win_rate = wins / total
    if p_value < alpha:
        return p_value, "alive" if win_rate > 0.5 else "dead"
    return p_value, "weak"


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

    # datetime() normalizes the stored ISO timestamp so the window compares
    # chronologically (see _closed_filter / get_calibration_report for the same
    # predicate), not by raw lexicographic order.
    conditions.append("datetime(outcome_date) >= datetime('now', ?)")
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


@dataclass
class TrackRecordCI:
    """Bootstrap confidence intervals around the track record.

    Args:
        n: Number of closed (HIT/MISS) predictions resampled.
        win_rate: Point win rate.
        win_rate_ci: (low, high) percentile CI for the win rate.
        brier_score: Point Brier score.
        brier_ci: (low, high) percentile CI for the Brier score.
        prob_better_than_coin: Fraction of bootstrap resamples whose win rate
            exceeds 0.50 — a one-line "is this distinguishable from a coin flip".
    """

    n: int
    win_rate: Optional[float]
    win_rate_ci: Optional[tuple[float, float]]
    brier_score: Optional[float]
    brier_ci: Optional[tuple[float, float]]
    prob_better_than_coin: Optional[float]


def _closed_filter(
    market: Optional[str],
    source: Optional[str],
    days: Optional[int],
    timeframe: Optional[str] = None,
) -> tuple[str, list]:
    """Build the WHERE clause for closed (HIT/MISS) predictions."""
    conditions = ["status IN ('HIT', 'MISS')"]
    params: list = []
    if market:
        conditions.append("market = ?")
        params.append(market)
    if source:
        conditions.append("source = ?")
        params.append(source)
    if timeframe:
        conditions.append("timeframe = ?")
        params.append(timeframe)
    if days is not None:
        # datetime() normalizes the stored ISO timestamp (e.g. ...T12:00:00+00:00)
        # so the window compares chronologically against datetime('now', ?) rather
        # than by raw lexicographic order — where the ISO 'T' sorts after the
        # space separator, wrongly including same-cutoff-date rows.
        conditions.append("datetime(outcome_date) >= datetime('now', ?)")
        params.append(f"-{days} days")
    return " AND ".join(conditions), params


def _percentile_ci(samples: list[float], alpha: float = 0.05) -> tuple[float, float]:
    """(alpha/2, 1-alpha/2) percentile interval from a list of bootstrap stats.

    Uses the standard nearest-rank index ``ceil(p*m) - 1`` (not ``int(p*m)``,
    which truncates inward and biases the interval too narrow for small m).
    """
    ordered = sorted(samples)
    m = len(ordered)
    lo_idx = max(0, ceil((alpha / 2) * m) - 1)
    hi_idx = min(m - 1, ceil((1 - alpha / 2) * m) - 1)
    return ordered[lo_idx], ordered[hi_idx]


def get_track_record_ci(
    conn: sqlite3.Connection,
    market: Optional[str] = None,
    source: Optional[str] = None,
    days: Optional[int] = 30,
    timeframe: Optional[str] = None,
    n_resamples: int = 1000,
    seed: int = 12345,
    alpha: float = 0.05,
) -> TrackRecordCI:
    """Bootstrap CIs for win rate and Brier score (resample-with-replacement).

    Adapted from Vibe-Trading's ``bootstrap_sharpe_ci`` (validation.py): resample
    the closed-outcome series ``n_resamples`` times and take percentile
    intervals, so the calibration report can say "win rate 0.54 [0.47, 0.61] —
    not distinguishable from 0.50" instead of a bare point estimate.

    Args:
        conn: SQLite connection.
        market/source/days/timeframe: Optional filters (days=None → all history).
        n_resamples: Bootstrap resample count. Defaults to 1000.
        seed: RNG seed for reproducible intervals.
        alpha: 1 - confidence level. Defaults to 0.05 (95% CI).

    Returns:
        TrackRecordCI; CIs are None when there are no closed predictions.
    """
    where, params = _closed_filter(market, source, days, timeframe)
    rows = conn.execute(
        f"SELECT status, confidence FROM predictions WHERE {where}", params
    ).fetchall()
    n = len(rows)
    if n == 0:
        return TrackRecordCI(0, None, None, None, None, None)

    outcomes = [1.0 if r["status"] == "HIT" else 0.0 for r in rows]
    briers = [(r["confidence"] - o) ** 2 for r, o in zip(rows, outcomes)]
    win_rate = sum(outcomes) / n
    brier = sum(briers) / n

    rng = random.Random(seed)
    win_samples = []
    brier_samples = []
    better = 0
    for _ in range(n_resamples):
        idx = [rng.randrange(n) for _ in range(n)]
        wr = sum(outcomes[j] for j in idx) / n
        br = sum(briers[j] for j in idx) / n
        win_samples.append(wr)
        brier_samples.append(br)
        if wr > 0.5:
            better += 1

    return TrackRecordCI(
        n=n,
        win_rate=round(win_rate, 4),
        win_rate_ci=tuple(round(x, 4) for x in _percentile_ci(win_samples, alpha)),
        brier_score=round(brier, 4),
        brier_ci=tuple(round(x, 4) for x in _percentile_ci(brier_samples, alpha)),
        prob_better_than_coin=round(better / n_resamples, 4),
    )


def permutation_test_confidence(
    conn: sqlite3.Connection,
    market: Optional[str] = None,
    source: Optional[str] = None,
    days: Optional[int] = None,
    timeframe: Optional[str] = None,
    n_permutations: int = 1000,
    seed: int = 12345,
) -> dict:
    """Permutation test: does confidence carry information about the outcome?

    Statistic = mean(confidence | HIT) - mean(confidence | MISS). Under the null
    (confidence unrelated to outcome) the HIT/MISS labels are exchangeable, so we
    shuffle them ``n_permutations`` times and measure how often the permuted
    statistic is at least as extreme as the observed one. A low p-value means
    higher-confidence calls really do hit more — the model's conviction is
    informative. (Win rate itself is invariant to label shuffling, so this tests
    the confidence signal, not the hit rate, which the binomial test covers.)

    Args:
        conn: SQLite connection.
        market/source/days/timeframe: Optional filters (days=None → all history).
        n_permutations: Shuffle count. Defaults to 1000.
        seed: RNG seed for reproducibility.

    Returns:
        Dict with ``n``, ``observed_diff``, and a two-sided ``p_value`` (so an
        *anti*-informative confidence — higher conviction hitting less — is
        flagged too). When the statistic is undefined (no HIT or no MISS),
        p_value is 1.0.
    """
    where, params = _closed_filter(market, source, days, timeframe)
    rows = conn.execute(
        f"SELECT status, confidence FROM predictions WHERE {where}", params
    ).fetchall()
    n = len(rows)
    confidences = [r["confidence"] for r in rows]
    labels = [1 if r["status"] == "HIT" else 0 for r in rows]
    n_hit = sum(labels)
    n_miss = n - n_hit

    if n_hit == 0 or n_miss == 0:
        return {"n": n, "observed_diff": 0.0, "p_value": 1.0}

    def _diff(lbls: list[int]) -> float:
        hit_sum = sum(c for c, l in zip(confidences, lbls) if l == 1)
        miss_sum = sum(c for c, l in zip(confidences, lbls) if l == 0)
        return hit_sum / n_hit - miss_sum / n_miss

    observed = _diff(labels)
    abs_observed = abs(observed)
    rng = random.Random(seed)
    shuffled = list(labels)
    at_least = 0
    for _ in range(n_permutations):
        rng.shuffle(shuffled)
        # Two-sided: count permutations at least as extreme in either direction.
        if abs(_diff(shuffled)) >= abs_observed - 1e-12:
            at_least += 1

    # (k+1)/(N+1) includes the observed arrangement, so a finite Monte-Carlo
    # permutation test never reports an impossible exact-zero p-value.
    p_value = (at_least + 1) / (n_permutations + 1)
    return {
        "n": n,
        "observed_diff": round(observed, 4),
        "p_value": round(p_value, 4),
    }


def get_calibration_report(
    conn: sqlite3.Connection,
    source: Optional[str] = None,
    timeframe: Optional[str] = None,
    buckets: int = 5,
    days: Optional[int] = None,
    use_raw: bool = True,
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
        days: Optional look-back window. When set, only predictions whose
            ``outcome_date`` falls within the last ``days`` days are counted
            (matching ``get_track_record``); ``None`` uses the full history.
        use_raw: When True (default), bucket on the model's RAW confidence
            (``COALESCE(raw_confidence, confidence)``) so the curve trains on
            pre-recalibration input. When False, bucket on the as-logged
            ``confidence`` column — i.e. the value actually emitted after
            ``--recalibrate`` — to reveal whether recalibration closed the gap.

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
    if days is not None:
        # datetime() normalizes the stored ISO timestamp (e.g. ...T12:00:00+00:00)
        # so the window compares chronologically against datetime('now', ?) rather
        # than by raw lexicographic order — where the ISO 'T' sorts after the
        # space separator, wrongly including same-cutoff-date rows.
        conditions.append("datetime(outcome_date) >= datetime('now', ?)")
        params.append(f"-{days} days")

    where = " AND ".join(conditions)
    # ``use_raw`` (default) calibrates on RAW model confidence: once
    # --recalibrate is in use the ``confidence`` column holds recalibrated
    # values, so training the curve on it would feed recalibrated output back as
    # input (a recursive loop that collapses the map's domain). ``raw_confidence``
    # preserves the original; COALESCE falls back to ``confidence`` for
    # legacy/non-recalibrated rows. ``use_raw=False`` buckets on the as-logged
    # ``confidence`` instead — used only for reporting how the emitted (post-
    # recalibration) confidence calibrates, never to train the map.
    confidence_expr = (
        "COALESCE(raw_confidence, confidence)" if use_raw else "confidence"
    )
    rows = conn.execute(
        f"SELECT {confidence_expr} AS confidence, status "
        f"FROM predictions WHERE {where}",
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
    days: Optional[int] = None,
) -> list[SignalPerformance]:
    """Compute win rate per analysis signal.

    Only reports signals with at least `min_count` closed predictions
    to avoid noise.

    Args:
        conn: SQLite connection.
        min_count: Minimum predictions per signal to report. Defaults to 10.
        days: Optional look-back window. When set, only predictions whose
            ``outcome_date`` falls within the last ``days`` days are counted
            (matching ``get_track_record``); ``None`` uses the full history.

    Returns:
        List of SignalPerformance, sorted by win_rate descending.
    """
    conditions = ["status IN ('HIT', 'MISS')"]
    params: list = []
    if days is not None:
        # datetime() normalizes the stored ISO timestamp (e.g. ...T12:00:00+00:00)
        # so the window compares chronologically against datetime('now', ?) rather
        # than by raw lexicographic order — where the ISO 'T' sorts after the
        # space separator, wrongly including same-cutoff-date rows.
        conditions.append("datetime(outcome_date) >= datetime('now', ?)")
        params.append(f"-{days} days")
    where = " AND ".join(conditions)
    rows = conn.execute(
        f"SELECT signals_used, status FROM predictions WHERE {where}",
        params,
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
            p_value, verdict = _signal_verdict(stats["wins"], stats["total"])
            result.append(
                SignalPerformance(
                    signal=sig,
                    total=stats["total"],
                    wins=stats["wins"],
                    win_rate=round(stats["wins"] / stats["total"], 3),
                    p_value=round(p_value, 4),
                    verdict=verdict,
                )
            )

    result.sort(key=lambda x: x.win_rate, reverse=True)
    return result


def _decay_label(
    train_verdict: str, test_verdict: str, train_total: int, test_total: int
) -> str:
    """Combine train/test verdicts into an out-of-sample decay grade.

    Returns "insufficient_oos" when either window is empty — without samples on
    both sides there is no out-of-sample comparison to make, so we must not
    claim decay (e.g. a signal with no recent predictions is not "train_only").
    """
    if train_total == 0 or test_total == 0:
        return "insufficient_oos"
    if test_verdict == "dead":
        return "reversed"  # OOS sign-flip is the strongest negative evidence
    if train_verdict == "alive" and test_verdict == "alive":
        return "confirmed_alive"
    if train_verdict == "alive":
        return "train_only"  # worked historically, only a coin flip recently
    return "noise"


def get_signal_decay(
    conn: sqlite3.Connection,
    min_count: int = 10,
    oos_fraction: float = 0.3,
) -> list[SignalDecay]:
    """Grade each signal's out-of-sample stability via a train/test split.

    Closed predictions are ordered by ``outcome_date`` and split at the
    ``1 - oos_fraction`` count boundary into an earlier (train) and a recent
    (test) window. Each signal is graded against the 50% null on each side and
    assigned a decay label (see ``SignalDecay``). This exposes signals whose
    edge decayed recently — exactly what a static all-history win rate hides.

    Args:
        conn: SQLite connection.
        min_count: Minimum total closed predictions per signal to report.
        oos_fraction: Fraction of (time-ordered) closed predictions placed in
            the recent test window. Defaults to 0.3.

    Returns:
        List of SignalDecay sorted by signal name.
    """
    import json

    rows = conn.execute(
        "SELECT signals_used, status, outcome_date FROM predictions "
        "WHERE status IN ('HIT', 'MISS') AND outcome_date IS NOT NULL "
        # created_at + id are stable tie-breakers so the index split is
        # deterministic when several predictions resolve on the same date.
        "ORDER BY outcome_date ASC, created_at ASC, id ASC"
    ).fetchall()
    if not rows:
        return []

    # Split by row INDEX, not by date value: rows are already time-ordered, and
    # an index split guarantees the train/test count ratio even when many
    # predictions share the same outcome_date (a date-value boundary would push
    # every same-day row to one side and could empty the train window).
    n = len(rows)
    split_idx = min(max(int(n * (1 - oos_fraction)), 0), n - 1)

    stats: dict[str, dict[str, int]] = {}
    for i, r in enumerate(rows):
        is_test = i >= split_idx
        win = 1 if r["status"] == "HIT" else 0
        for sig in json.loads(r["signals_used"]):
            d = stats.setdefault(
                sig, {"train_t": 0, "train_w": 0, "test_t": 0, "test_w": 0}
            )
            if is_test:
                d["test_t"] += 1
                d["test_w"] += win
            else:
                d["train_t"] += 1
                d["train_w"] += win

    result = []
    for sig, d in stats.items():
        if d["train_t"] + d["test_t"] < min_count:
            continue
        _, train_verdict = _signal_verdict(d["train_w"], d["train_t"])
        _, test_verdict = _signal_verdict(d["test_w"], d["test_t"])
        result.append(
            SignalDecay(
                signal=sig,
                train_total=d["train_t"],
                train_wins=d["train_w"],
                test_total=d["test_t"],
                test_wins=d["test_w"],
                train_verdict=train_verdict,
                test_verdict=test_verdict,
                label=_decay_label(
                    train_verdict, test_verdict, d["train_t"], d["test_t"]
                ),
            )
        )
    result.sort(key=lambda s: s.signal)
    return result


RECAL_MIN_CLOSED = 30  # min closed (HIT/MISS) rows before a source's map is trusted


def recalibrate_confidence(
    conn: sqlite3.Connection,
    raw_confidence: float,
    source: str,
    min_closed: int = RECAL_MIN_CLOSED,
) -> tuple[float, bool]:
    """Map a raw confidence through the source's isotonic recalibration curve.

    Shared by ``predict create --recalibrate`` and the scheduler's API-mode
    logger so both honour the same source-scoped recalibration guarantee. The
    curve is built from *closed* predictions of the same source (a global,
    all-horizon map — as accurate as per-horizon maps and far more robust at
    current sample sizes).

    Args:
        conn: SQLite connection to predictions.db.
        raw_confidence: The model's raw confidence in [0, 1].
        source: Prediction source whose history defines the curve.
        min_closed: Below this many closed rows the map is noise, so this is a
            no-op (returns the raw confidence, ``applied=False``).

    Returns:
        ``(confidence_to_store, applied)``.
    """
    n_closed = conn.execute(
        "SELECT COUNT(*) FROM predictions WHERE status IN ('HIT', 'MISS') AND source = ?",
        (source,),
    ).fetchone()[0]
    if n_closed < min_closed:
        return raw_confidence, False
    recal_map = build_recalibration_map(conn, source=source)
    if not recal_map:
        return raw_confidence, False
    return apply_recalibration(raw_confidence, recal_map), True


def get_component_contribution(conn: sqlite3.Connection, min_count: int = 8) -> dict:
    """Win-rate of closed predictions split by each stored ``components`` pillar.

    Reads ``predictions.components`` (JSON) on closed (HIT/MISS) rows. Numeric
    pillars (algo / news / llm_context scores) split into ``positive`` vs
    ``non_positive`` contribution; categorical pillars (overextension, regime)
    group by value. This quantifies each capability's *marginal* value — e.g.
    "does a positive news contribution actually raise the hit rate?" — and is the
    measurement substrate for a future blended confidence.

    Args:
        conn: SQLite connection.
        min_count: Minimum closed rows in a bucket for it to be reported.

    Returns:
        ``{"n_with_components": int, "pillars": {pillar: {bucket: {"n", "wins",
        "win_rate"}}}}``. Empty pillars until enough components-tagged rows
        accumulate (the column is new).
    """
    import json

    rows = conn.execute(
        "SELECT components, status FROM predictions "
        "WHERE status IN ('HIT', 'MISS') AND components IS NOT NULL"
    ).fetchall()

    buckets: dict[str, dict[str, list[int]]] = {}
    n_valid = 0
    for r in rows:
        try:
            comp = json.loads(r["components"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(comp, dict):
            continue
        n_valid += 1
        win = 1 if r["status"] == "HIT" else 0
        for k, v in comp.items():
            if isinstance(v, (dict, list)):
                # Nested payloads (e.g. news_signal) have their own readout
                # (get_news_tag_performance) and would bucket as garbage here.
                continue
            if isinstance(v, bool):
                label = str(v)
            elif isinstance(v, (int, float)):
                # Separate zero (neutral / not-applicable) from a true headwind.
                label = "positive" if v > 0 else "negative" if v < 0 else "zero"
            else:
                label = str(v)
            slot = buckets.setdefault(k, {}).setdefault(label, [0, 0])
            slot[0] += win
            slot[1] += 1

    pillars: dict[str, dict] = {}
    for pillar, labs in buckets.items():
        reported = {
            label: {"n": t, "wins": w, "win_rate": round(w / t, 3)}
            for label, (w, t) in labs.items()
            if t >= min_count
        }
        if reported:
            pillars[pillar] = reported
    return {"n_with_components": n_valid, "pillars": pillars}


def get_news_tag_performance(conn: sqlite3.Connection, min_count: int = 8) -> dict:
    """Win-rate of closed predictions split by persisted news-signal fields.

    Reads ``components.news_signal`` (stored by the Stage 3 persistence path)
    on closed (HIT/MISS) rows and aggregates per event tag plus the
    hard-catalyst flags. This answers "which catalyst tags actually predict?"
    — the collapsed news score is graded dead (~21-33% win), but individual
    tags may not be.

    Args:
        conn: SQLite connection.
        min_count: Minimum closed rows in a bucket for it to be reported
            (mirrors ``get_component_contribution``; smaller buckets are noise).

    Returns:
        ``{"n_with_news_signal": int,
           "tags": {tag: {"n", "wins", "win_rate"}},
           "catalysts": {"positive"/"negative": {"n", "wins", "win_rate"}}}``.
    """
    import json

    rows = conn.execute(
        "SELECT components, status FROM predictions "
        "WHERE status IN ('HIT', 'MISS') AND components IS NOT NULL"
    ).fetchall()

    tag_buckets: dict[str, list[int]] = {}
    catalyst_buckets: dict[str, list[int]] = {}
    n_with = 0
    for r in rows:
        try:
            comp = json.loads(r["components"])
        except (json.JSONDecodeError, TypeError):
            continue
        sig = comp.get("news_signal") if isinstance(comp, dict) else None
        if not isinstance(sig, dict):
            continue
        n_with += 1
        win = 1 if r["status"] == "HIT" else 0
        tags = sig.get("event_tags")
        # LLM-authored components need type discipline: a bare string would
        # iterate per-character, duplicate tags would count one prediction
        # twice, and truthy strings like "false" would arm the catalyst flag.
        if isinstance(tags, list):
            for tag in {t for t in tags if isinstance(t, str)}:
                slot = tag_buckets.setdefault(tag, [0, 0])
                slot[0] += win
                slot[1] += 1
        if sig.get("has_positive_catalyst") is True:
            slot = catalyst_buckets.setdefault("positive", [0, 0])
            slot[0] += win
            slot[1] += 1
        if sig.get("has_negative_catalyst") is True:
            slot = catalyst_buckets.setdefault("negative", [0, 0])
            slot[0] += win
            slot[1] += 1

    def _report(buckets: dict[str, list[int]]) -> dict:
        return {
            k: {"n": t, "wins": w, "win_rate": round(w / t, 3)}
            for k, (w, t) in buckets.items()
            if t >= min_count
        }

    return {
        "n_with_news_signal": n_with,
        "tags": _report(tag_buckets),
        "catalysts": _report(catalyst_buckets),
    }


def _isotonic_nondecreasing(
    points: list[tuple[float, float, int]],
) -> list[tuple[float, float]]:
    """Pool-Adjacent-Violators projection onto non-decreasing y.

    Args:
        points: (x, y, weight) sorted ascending by x.

    Returns:
        (x, y_monotonic) with the same x's and weighted-averaged y enforced to
        be non-decreasing.
    """
    xs = [x for x, _, _ in points]
    # Each block: [y_avg, weight, n_points_covered].
    blocks: list[list[float]] = []
    for _, y, w in points:
        blocks.append([y, float(w), 1])
        while len(blocks) >= 2 and blocks[-2][0] > blocks[-1][0]:
            y2, w2, n2 = blocks.pop()
            y1, w1, n1 = blocks.pop()
            blocks.append([(y1 * w1 + y2 * w2) / (w1 + w2), w1 + w2, n1 + n2])

    out: list[tuple[float, float]] = []
    idx = 0
    for y_avg, _, n in blocks:
        for _ in range(int(n)):
            out.append((xs[idx], y_avg))
            idx += 1
    return out


def build_recalibration_map(
    conn: sqlite3.Connection,
    source: Optional[str] = None,
    timeframe: Optional[str] = None,
    buckets: int = 5,
    min_count: int = 1,
) -> list[tuple[float, float]]:
    """Fit a monotonic confidence-recalibration map from the calibration curve.

    Reads the calibration curve (predicted confidence vs actual accuracy per
    bucket) and projects the actual-accuracy values onto a non-decreasing
    function of predicted confidence (isotonic regression). The result maps a
    raw model confidence to the empirically observed hit rate — directly
    addressing near-constant, overconfident raw confidences.

    Args:
        conn: SQLite connection.
        source: Optional source filter (LIVE, BACKTEST, INTERACTIVE).
        timeframe: Optional timeframe filter (short/long horizons differ).
        buckets: Number of calibration buckets. Defaults to 5.
        min_count: Minimum predictions per bucket to use as an anchor.

    Returns:
        List of (predicted_confidence, recalibrated_confidence) anchor points,
        sorted ascending and monotonic non-decreasing. Empty if no data.
    """
    bs = get_calibration_report(
        conn, source=source, timeframe=timeframe, buckets=buckets
    )
    anchors = [
        (b.predicted_confidence, b.actual_accuracy, b.count)
        for b in bs
        if b.count >= min_count
    ]
    anchors.sort(key=lambda t: t[0])
    if not anchors:
        return []
    return _isotonic_nondecreasing(anchors)


def apply_recalibration(
    raw_confidence: float,
    recal_map: list[tuple[float, float]],
) -> float:
    """Apply a recalibration map to a raw confidence via linear interpolation.

    Args:
        raw_confidence: The model's raw confidence in [0, 1].
        recal_map: Output of ``build_recalibration_map``.

    Returns:
        The recalibrated confidence. Returns ``raw_confidence`` unchanged when
        the map is empty (no calibration data yet). Inputs outside the anchor
        range are clamped to the nearest anchor's value.
    """
    if not recal_map:
        return raw_confidence
    pts = sorted(recal_map)
    if raw_confidence <= pts[0][0]:
        return pts[0][1]
    if raw_confidence >= pts[-1][0]:
        return pts[-1][1]
    for (p0, a0), (p1, a1) in zip(pts, pts[1:]):
        if p0 <= raw_confidence <= p1:
            if p1 == p0:
                return a1
            frac = (raw_confidence - p0) / (p1 - p0)
            return a0 + frac * (a1 - a0)
    return raw_confidence
