"""Tests for outcome tracker scoring logic."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "mcp-prediction-store"))

from models import Prediction
from outcome_tracker import evaluate_prediction, count_trading_days


def _pred(
    direction="BULL",
    entry=100.0,
    target=None,
    stop=None,
    timeframe="1W",
    market="US",
    created_days_ago=0,
) -> Prediction:
    """Create a test prediction."""
    from datetime import datetime, timedelta, timezone

    created = datetime.now(timezone.utc) - timedelta(days=created_days_ago)
    return Prediction(
        ticker="TEST",
        market=market,
        direction=direction,
        confidence=0.7,
        timeframe=timeframe,
        reasoning="test",
        entry_price=entry,
        target_price=target,
        stop_price=stop,
        created_at=created.isoformat(),
    )


# --- BULL predictions ---


def test_bull_hit_with_target():
    """BULL prediction hits when price >= target."""
    pred = _pred(direction="BULL", entry=100.0, target=110.0)
    status, ret = evaluate_prediction(pred, current_price=112.0)
    assert status == "HIT"
    assert ret == 12.0


def test_bull_hit_default_threshold():
    """BULL prediction hits at +3% when no target set."""
    pred = _pred(direction="BULL", entry=100.0)
    status, ret = evaluate_prediction(pred, current_price=103.5)
    assert status == "HIT"
    assert ret == 3.5


def test_bull_miss_with_stop():
    """BULL prediction misses when price <= stop."""
    pred = _pred(direction="BULL", entry=100.0, stop=95.0)
    status, ret = evaluate_prediction(pred, current_price=94.0)
    assert status == "MISS"
    assert ret == -6.0


def test_bull_miss_default_threshold():
    """BULL prediction misses at -5% when no stop set."""
    pred = _pred(direction="BULL", entry=100.0)
    status, ret = evaluate_prediction(pred, current_price=94.5)
    assert status == "MISS"
    assert ret == -5.5


def test_bull_miss_before_hit():
    """MISS takes priority over HIT when both could trigger."""
    # Edge case: stop at 95, target at 105, price at 94 (below stop)
    pred = _pred(direction="BULL", entry=100.0, target=105.0, stop=95.0)
    status, ret = evaluate_prediction(pred, current_price=94.0)
    assert status == "MISS"


def test_bull_still_open():
    """BULL prediction remains open when between stop and target."""
    pred = _pred(direction="BULL", entry=100.0, target=110.0, stop=95.0)
    status, ret = evaluate_prediction(pred, current_price=102.0)
    assert status is None
    assert ret == 2.0


# --- BEAR predictions ---


def test_bear_hit_with_target():
    """BEAR prediction hits when price <= target."""
    pred = _pred(direction="BEAR", entry=100.0, target=90.0)
    status, ret = evaluate_prediction(pred, current_price=88.0)
    assert status == "HIT"
    assert ret == -12.0


def test_bear_miss_with_stop():
    """BEAR prediction misses when price >= stop."""
    pred = _pred(direction="BEAR", entry=100.0, stop=105.0)
    status, ret = evaluate_prediction(pred, current_price=106.0)
    assert status == "MISS"
    assert ret == 6.0


def test_bear_still_open():
    """BEAR prediction remains open between target and stop."""
    pred = _pred(direction="BEAR", entry=100.0, target=90.0, stop=105.0)
    status, ret = evaluate_prediction(pred, current_price=97.0)
    assert status is None


# --- NEUTRAL predictions ---


def test_neutral_miss_when_moves_outside_range():
    """NEUTRAL prediction misses when price moves beyond +/-3%."""
    pred = _pred(direction="NEUTRAL", entry=100.0, timeframe="1W")
    status, ret = evaluate_prediction(pred, current_price=104.0)
    assert status == "MISS"


def test_neutral_still_open_within_range():
    """NEUTRAL stays open within range before timeframe expires."""
    pred = _pred(direction="NEUTRAL", entry=100.0, timeframe="1W", created_days_ago=2)
    status, ret = evaluate_prediction(pred, current_price=101.5)
    assert status is None


# --- EXPIRED ---


def test_expired_when_timeframe_elapsed():
    """Prediction expires when trading days exceed timeframe."""
    pred = _pred(
        direction="BULL",
        entry=100.0,
        target=110.0,
        stop=95.0,
        timeframe="1W",
        created_days_ago=10,  # Well past 5 trading days
    )
    status, ret = evaluate_prediction(pred, current_price=101.0)
    assert status == "EXPIRED"


def test_not_expired_within_timeframe():
    """Prediction does not expire within its timeframe."""
    pred = _pred(
        direction="BULL",
        entry=100.0,
        target=110.0,
        stop=95.0,
        timeframe="1M",
        created_days_ago=5,
    )
    status, ret = evaluate_prediction(pred, current_price=101.0)
    assert status is None


# --- Korean market specifics ---


def test_kr_prediction_2w_timeframe():
    """Korean 2W predictions need 10 trading days."""
    pred = _pred(
        direction="BULL",
        entry=70000.0,
        target=73000.0,
        stop=67000.0,
        timeframe="2W",
        market="KR",
        created_days_ago=8,  # ~6 trading days, not yet 10
    )
    status, _ = evaluate_prediction(pred, current_price=70500.0)
    assert status is None  # Still open


# --- Edge cases ---


def test_zero_entry_price():
    """Zero entry price returns None (can't evaluate)."""
    pred = _pred(entry=0.0)
    status, ret = evaluate_prediction(pred, current_price=100.0)
    assert status is None


def test_exact_target_price():
    """Price exactly at target counts as HIT."""
    pred = _pred(direction="BULL", entry=100.0, target=110.0)
    status, _ = evaluate_prediction(pred, current_price=110.0)
    assert status == "HIT"


def test_exact_stop_price():
    """Price exactly at stop counts as MISS."""
    pred = _pred(direction="BULL", entry=100.0, stop=95.0)
    status, _ = evaluate_prediction(pred, current_price=95.0)
    assert status == "MISS"
