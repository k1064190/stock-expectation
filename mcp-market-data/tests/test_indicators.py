"""Tests for pure-Python horizon indicators."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from indicators import (
    CYCLE_RISK_PCT_FROM_ATH_THRESHOLD,
    CYCLE_RISK_RETURN_1Y_THRESHOLD,
    compute_horizon_metrics,
    compute_rsi,
    compute_sma,
)


def _bars(closes: list[float]) -> list[dict]:
    """Wrap a list of closes in minimal OHLCV dicts (oldest-first)."""
    return [
        {
            "date": f"2025-{(i % 12) + 1:02d}-01",
            "open": c,
            "high": c,
            "low": c,
            "close": c,
            "volume": 1_000_000,
        }
        for i, c in enumerate(closes)
    ]


def test_rsi_monotonic_uptrend_maxes_out():
    """A strictly rising series has no losses, so RSI saturates at 100."""
    closes = [100.0 + i for i in range(20)]
    rsi = compute_rsi(closes, period=14)
    assert rsi == pytest.approx(100.0)


def test_rsi_returns_none_when_insufficient_bars():
    """Fewer than period+1 bars can't form even one RSI value."""
    closes = [100.0 + i for i in range(10)]
    assert compute_rsi(closes, period=14) is None


def test_rsi_midrange_for_balanced_series():
    """Alternating small ups/downs should produce RSI near 50."""
    closes: list[float] = [100.0]
    for i in range(1, 20):
        closes.append(closes[-1] + (1.0 if i % 2 == 0 else -1.0))
    rsi = compute_rsi(closes, period=14)
    assert rsi is not None
    assert 40.0 <= rsi <= 60.0


def test_sma_none_when_insufficient():
    """SMA(20) needs ≥ 20 bars."""
    assert compute_sma([float(i) for i in range(10)], 20) is None


def test_sma_trailing_window():
    """SMA uses the trailing window, not the whole series."""
    closes = [float(i) for i in range(30)]  # 0..29
    # last 20 values are 10..29; mean = (10+29)/2 = 19.5
    assert compute_sma(closes, 20) == pytest.approx(19.5)


def test_cycle_risk_flag_triggers_on_peak():
    """Synthetic series that tripled in ~1 year and ends near ATH → flag=True.

    We inject 260 bars (> 252 so return_1y is computable) climbing from 100
    to 500 then pulling back to 475 (~-5% off ATH). return_1y on the 500→475
    pattern is still well above the +100% threshold.
    """
    # 260 bars: rise from 100 to 500, then small pullback to 475 at the end.
    closes = [100.0 + (400.0 * i / 258) for i in range(259)]  # 100..500
    closes.append(475.0)  # 260th bar: near-ATH pullback
    metrics = compute_horizon_metrics(_bars(closes), ticker="MU", market="US")
    assert metrics.return_1y is not None
    assert metrics.return_1y > CYCLE_RISK_RETURN_1Y_THRESHOLD
    assert metrics.pct_from_52w_high is not None
    assert metrics.pct_from_52w_high > CYCLE_RISK_PCT_FROM_ATH_THRESHOLD
    assert metrics.cycle_risk_flag is True


def test_cycle_risk_flag_false_when_not_near_ath():
    """Same high 1Y return but price has dropped 30% off ATH → flag=False."""
    # Rise to 500, then pull back to 350 at the end (-30% from peak).
    closes = [100.0 + (400.0 * i / 258) for i in range(259)]
    closes.append(350.0)
    metrics = compute_horizon_metrics(_bars(closes), ticker="MU", market="US")
    assert metrics.return_1y is not None
    assert metrics.return_1y > CYCLE_RISK_RETURN_1Y_THRESHOLD
    assert metrics.pct_from_52w_high is not None
    assert metrics.pct_from_52w_high <= CYCLE_RISK_PCT_FROM_ATH_THRESHOLD
    assert metrics.cycle_risk_flag is False


def test_cycle_risk_flag_false_without_huge_return():
    """Near ATH but only +20% on the year → flag=False (base-case rise)."""
    closes = [100.0 + (20.0 * i / 259) for i in range(260)]  # 100..120
    metrics = compute_horizon_metrics(_bars(closes), ticker="AAPL", market="US")
    assert metrics.return_1y is not None
    assert metrics.return_1y < CYCLE_RISK_RETURN_1Y_THRESHOLD
    assert metrics.cycle_risk_flag is False


def test_horizon_metrics_returns_none_on_sparse_data():
    """With only 30 bars, long-horizon metrics must be None, flag=False."""
    closes = [100.0 + i for i in range(30)]
    metrics = compute_horizon_metrics(_bars(closes), ticker="X", market="US")
    assert metrics.ma200 is None
    assert metrics.return_6m is None
    assert metrics.return_1y is None
    assert metrics.cycle_risk_flag is False


def test_horizon_metrics_empty_bars_raises():
    """Empty input is a programmer error, not a silent None."""
    with pytest.raises(ValueError):
        compute_horizon_metrics([], ticker="X", market="US")


def test_return_1w_computed_when_enough_bars():
    """return_1w compares close[-1] to close[-6] (5 trading days back)."""
    closes = [100.0] * 6 + [110.0]  # 7 bars total, last is 110, [-6] is 100
    metrics = compute_horizon_metrics(_bars(closes), ticker="X", market="US")
    assert metrics.return_1w == pytest.approx(0.10)
