"""Tests for track record computation and calibration analysis."""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import (
    Prediction,
    get_connection,
    insert_prediction,
    update_prediction_outcome,
)
from metrics import (
    get_track_record,
    get_calibration_report,
    get_signal_performance,
    build_recalibration_map,
    apply_recalibration,
)


@pytest.fixture
def db_conn():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    conn = get_connection(db_path)
    yield conn
    conn.close()
    db_path.unlink(missing_ok=True)
    Path(str(db_path) + "-wal").unlink(missing_ok=True)
    Path(str(db_path) + "-shm").unlink(missing_ok=True)


def _make_prediction(
    ticker, direction="BULL", confidence=0.7, signals=None, market="US"
):
    return Prediction(
        ticker=ticker,
        market=market,
        direction=direction,
        confidence=confidence,
        timeframe="1W",
        reasoning="test",
        entry_price=100.0,
        signals_used=signals or ["technical"],
        source="LIVE",
    )


def test_empty_track_record(db_conn):
    """Track record with no data returns zeros."""
    record = get_track_record(db_conn)
    assert record.total == 0
    assert record.wins == 0
    assert record.win_rate is None
    assert record.brier_score is None


def test_track_record_counts(db_conn):
    """Track record correctly counts wins, losses, expired."""
    preds = [
        _make_prediction("AAPL"),
        _make_prediction("MSFT"),
        _make_prediction("GOOG"),
        _make_prediction("AMZN"),
    ]
    for p in preds:
        insert_prediction(db_conn, p)

    update_prediction_outcome(db_conn, preds[0].id, "HIT", 110.0, 10.0)
    update_prediction_outcome(db_conn, preds[1].id, "HIT", 108.0, 8.0)
    update_prediction_outcome(db_conn, preds[2].id, "MISS", 92.0, -8.0)
    update_prediction_outcome(db_conn, preds[3].id, "EXPIRED", 101.0, 1.0)

    record = get_track_record(db_conn)
    assert record.total == 4
    assert record.wins == 2
    assert record.losses == 1
    assert record.expired == 1
    assert record.win_rate == pytest.approx(2 / 3, rel=0.01)


def test_track_record_avg_return(db_conn):
    """Track record computes average return correctly."""
    preds = [_make_prediction("AAPL"), _make_prediction("MSFT")]
    for p in preds:
        insert_prediction(db_conn, p)

    update_prediction_outcome(db_conn, preds[0].id, "HIT", 110.0, 10.0)
    update_prediction_outcome(db_conn, preds[1].id, "MISS", 95.0, -5.0)

    record = get_track_record(db_conn)
    assert record.avg_return == pytest.approx(2.5, rel=0.01)


def test_track_record_streak(db_conn):
    """Track record computes current streak."""
    preds = [
        _make_prediction("AAPL"),
        _make_prediction("MSFT"),
        _make_prediction("GOOG"),
    ]
    for p in preds:
        insert_prediction(db_conn, p)

    # MISS, then HIT, HIT → streak should be +2
    update_prediction_outcome(db_conn, preds[0].id, "MISS", 90.0, -10.0)
    update_prediction_outcome(db_conn, preds[1].id, "HIT", 110.0, 10.0)
    update_prediction_outcome(db_conn, preds[2].id, "HIT", 115.0, 15.0)

    record = get_track_record(db_conn)
    assert record.current_streak == 2


def test_brier_score(db_conn):
    """Brier score: perfect calibration = low score."""
    preds = [
        _make_prediction("AAPL", confidence=0.9),  # 90% confident, HIT
        _make_prediction("MSFT", confidence=0.6),  # 60% confident, MISS
    ]
    for p in preds:
        insert_prediction(db_conn, p)

    update_prediction_outcome(db_conn, preds[0].id, "HIT", 110.0, 10.0)
    update_prediction_outcome(db_conn, preds[1].id, "MISS", 95.0, -5.0)

    record = get_track_record(db_conn)
    # Brier = ((0.9-1)^2 + (0.6-0)^2) / 2 = (0.01 + 0.36) / 2 = 0.185
    assert record.brier_score == pytest.approx(0.185, rel=0.01)


def test_track_record_market_filter(db_conn):
    """Track record filters by market."""
    us_pred = _make_prediction("AAPL", market="US")
    kr_pred = _make_prediction("005930", market="KR")
    insert_prediction(db_conn, us_pred)
    insert_prediction(db_conn, kr_pred)

    update_prediction_outcome(db_conn, us_pred.id, "HIT", 110.0, 10.0)
    update_prediction_outcome(db_conn, kr_pred.id, "MISS", 65000.0, -7.1)

    us_record = get_track_record(db_conn, market="US")
    assert us_record.wins == 1
    assert us_record.losses == 0

    kr_record = get_track_record(db_conn, market="KR")
    assert kr_record.wins == 0
    assert kr_record.losses == 1


def test_calibration_report_empty(db_conn):
    """Calibration report with no data returns empty list."""
    report = get_calibration_report(db_conn)
    assert report == []


def test_calibration_buckets(db_conn):
    """Calibration report groups predictions by confidence."""
    # Create predictions at different confidence levels
    for conf, outcome in [
        (0.55, "HIT"),
        (0.58, "MISS"),
        (0.52, "HIT"),  # 0.50-0.60 bucket
        (0.75, "HIT"),
        (0.78, "HIT"),
        (0.72, "MISS"),  # 0.70-0.80 bucket
    ]:
        p = _make_prediction(f"T{conf}", confidence=conf)
        insert_prediction(db_conn, p)
        update_prediction_outcome(db_conn, p.id, outcome, 100.0, 0.0)

    report = get_calibration_report(db_conn, buckets=5)
    assert len(report) >= 1


def test_calibration_by_timeframe(db_conn):
    """timeframe filter restricts calibration to a single horizon."""
    # Two 1W predictions, both HIT at 0.70 confidence.
    for i in range(2):
        p = Prediction(
            ticker=f"W{i}",
            market="US",
            direction="BULL",
            confidence=0.70,
            timeframe="1W",
            reasoning="short",
            entry_price=100.0,
            signals_used=["technical"],
        )
        insert_prediction(db_conn, p)
        update_prediction_outcome(db_conn, p.id, "HIT", 110.0, 10.0)

    # Two 1Y predictions, both MISS at 0.70 confidence.
    for i in range(2):
        p = Prediction(
            ticker=f"Y{i}",
            market="US",
            direction="BULL",
            confidence=0.70,
            timeframe="1Y",
            reasoning="cycle",
            entry_price=100.0,
            signals_used=["cycle"],
        )
        insert_prediction(db_conn, p)
        update_prediction_outcome(db_conn, p.id, "MISS", 90.0, -10.0)

    # Without filter, buckets contain both 1W HITs and 1Y MISSes → 50% accuracy.
    all_report = get_calibration_report(db_conn)
    combined = [b for b in all_report if b.count == 4]
    assert combined, "expected one bucket with all 4 predictions"
    assert combined[0].actual_accuracy == pytest.approx(0.5, abs=0.001)

    # With timeframe=1W, only the HITs count → 100% accuracy.
    w_report = get_calibration_report(db_conn, timeframe="1W")
    assert len(w_report) == 1
    assert w_report[0].count == 2
    assert w_report[0].actual_accuracy == pytest.approx(1.0)

    # With timeframe=1Y, only the MISSes count → 0% accuracy.
    y_report = get_calibration_report(db_conn, timeframe="1Y")
    assert len(y_report) == 1
    assert y_report[0].count == 2
    assert y_report[0].actual_accuracy == pytest.approx(0.0)


def test_calibration_no_timeframe_filter(db_conn):
    """Omitting timeframe returns unchanged pre-P2 behavior."""
    for conf, outcome in [(0.65, "HIT"), (0.75, "MISS")]:
        p = _make_prediction(f"T{conf}", confidence=conf)
        insert_prediction(db_conn, p)
        update_prediction_outcome(db_conn, p.id, outcome, 100.0, 0.0)

    report = get_calibration_report(db_conn)
    total_counted = sum(b.count for b in report)
    assert total_counted == 2


def test_signal_performance_min_count(db_conn):
    """Signal performance requires minimum prediction count."""
    # Only 3 predictions — below default min_count of 10
    for i in range(3):
        p = _make_prediction(f"T{i}", signals=["breadth"])
        insert_prediction(db_conn, p)
        update_prediction_outcome(db_conn, p.id, "HIT", 110.0, 10.0)

    result = get_signal_performance(db_conn, min_count=10)
    assert len(result) == 0

    # With lower threshold, should appear
    result = get_signal_performance(db_conn, min_count=2)
    assert len(result) == 1
    assert result[0].signal == "breadth"
    assert result[0].win_rate == 1.0


def test_signal_performance_multiple_signals(db_conn):
    """Signal performance tracks each signal independently."""
    # 5 predictions with "technical", 5 with "breadth", 3 with both
    for i in range(5):
        p = _make_prediction(f"TECH{i}", signals=["technical"])
        insert_prediction(db_conn, p)
        outcome = "HIT" if i < 3 else "MISS"
        update_prediction_outcome(db_conn, p.id, outcome, 100.0, 0.0)

    for i in range(5):
        p = _make_prediction(f"BRD{i}", signals=["breadth"])
        insert_prediction(db_conn, p)
        outcome = "HIT" if i < 4 else "MISS"
        update_prediction_outcome(db_conn, p.id, outcome, 100.0, 0.0)

    result = get_signal_performance(db_conn, min_count=5)
    assert len(result) == 2

    tech = next(s for s in result if s.signal == "technical")
    assert tech.win_rate == pytest.approx(0.6, rel=0.01)

    breadth = next(s for s in result if s.signal == "breadth")
    assert breadth.win_rate == pytest.approx(0.8, rel=0.01)


# ---------------------------------------------------------------------------
# S1: significance-tested signal verdicts (alive / weak / dead)
# ---------------------------------------------------------------------------


def _seed_signal(db_conn, signal, n_wins, n_losses):
    """Insert n_wins HIT + n_losses MISS predictions all tagged with `signal`."""
    i = 0
    for _ in range(n_wins):
        p = _make_prediction(f"{signal}_W{i}", signals=[signal])
        insert_prediction(db_conn, p)
        update_prediction_outcome(db_conn, p.id, "HIT", 110.0, 10.0)
        i += 1
    for _ in range(n_losses):
        p = _make_prediction(f"{signal}_L{i}", signals=[signal])
        insert_prediction(db_conn, p)
        update_prediction_outcome(db_conn, p.id, "MISS", 95.0, -5.0)
        i += 1


def test_binomial_handles_large_n_without_overflow():
    """Exact binomial stays finite for large n (no comb()->float overflow)."""
    from metrics import _binomial_two_sided_p

    # n=1100 overflowed the old comb(n,i)*float implementation.
    assert _binomial_two_sided_p(550, 1100) == pytest.approx(1.0, abs=0.05)
    # A strong skew at large n is highly significant.
    assert _binomial_two_sided_p(700, 1100) < 0.001


def test_signal_verdict_dead(db_conn):
    """A signal that loses far more than a coin flip is flagged 'dead'."""
    _seed_signal(db_conn, "valuation", n_wins=0, n_losses=12)
    result = get_signal_performance(db_conn, min_count=5)
    sig = next(s for s in result if s.signal == "valuation")
    assert sig.verdict == "dead"
    assert sig.p_value < 0.05


def test_signal_verdict_alive(db_conn):
    """A signal that wins far more than a coin flip is flagged 'alive'."""
    _seed_signal(db_conn, "momentum", n_wins=12, n_losses=1)
    result = get_signal_performance(db_conn, min_count=5)
    sig = next(s for s in result if s.signal == "momentum")
    assert sig.verdict == "alive"
    assert sig.p_value < 0.05


def test_signal_verdict_weak(db_conn):
    """A near-coin-flip signal is 'weak' (not statistically distinguishable)."""
    _seed_signal(db_conn, "cycle", n_wins=6, n_losses=5)
    result = get_signal_performance(db_conn, min_count=5)
    sig = next(s for s in result if s.signal == "cycle")
    assert sig.verdict == "weak"
    assert sig.p_value >= 0.05


# ---------------------------------------------------------------------------
# S3: confidence recalibration from the calibration curve
# ---------------------------------------------------------------------------


def test_recalibration_map_is_monotonic(db_conn):
    """The recalibration map's actual-accuracy anchors are non-decreasing."""
    # Bucket 0.6-0.7: overconfident (50% actual); bucket 0.8-0.9: well-calibrated.
    for i in range(10):
        p = _make_prediction(f"LOW{i}", confidence=0.65)
        insert_prediction(db_conn, p)
        update_prediction_outcome(db_conn, p.id, "HIT" if i < 5 else "MISS", 100.0, 0.0)
    for i in range(10):
        p = _make_prediction(f"HIGH{i}", confidence=0.85)
        insert_prediction(db_conn, p)
        update_prediction_outcome(db_conn, p.id, "HIT" if i < 8 else "MISS", 100.0, 0.0)

    recal = build_recalibration_map(db_conn)
    actuals = [actual for _, actual in recal]
    assert actuals == sorted(actuals)


def test_apply_recalibration_corrects_overconfidence(db_conn):
    """Raw 0.65 confidence in an overconfident bucket maps down toward 0.5."""
    for i in range(10):
        p = _make_prediction(f"OC{i}", confidence=0.65)
        insert_prediction(db_conn, p)
        update_prediction_outcome(db_conn, p.id, "HIT" if i < 5 else "MISS", 100.0, 0.0)
    recal = build_recalibration_map(db_conn)
    assert apply_recalibration(0.65, recal) == pytest.approx(0.5, abs=0.05)


def test_apply_recalibration_empty_map_is_identity():
    """With no calibration data, recalibration returns the raw confidence."""
    assert apply_recalibration(0.6, []) == 0.6
