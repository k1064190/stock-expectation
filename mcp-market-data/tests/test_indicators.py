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


def _bars(closes: list[float], volumes: list[int] | None = None) -> list[dict]:
    """Wrap a list of closes (and optional volumes) in OHLCV dicts.

    Bars are emitted oldest-first to match the contract of
    ``compute_horizon_metrics``. ``volumes`` must be the same length as
    ``closes`` when supplied; otherwise every bar gets a constant 1M volume.
    """
    if volumes is None:
        volumes = [1_000_000] * len(closes)
    assert len(volumes) == len(closes), "volumes length must match closes"
    return [
        {
            "date": f"2025-{(i % 12) + 1:02d}-01",
            "open": c,
            "high": c,
            "low": c,
            "close": c,
            "volume": v,
        }
        for i, (c, v) in enumerate(zip(closes, volumes))
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


# ---------------------------------------------------------------------------
# Volume metrics — added so the /expect Volume point-table bucket has data
# (previously the field was missing from horizon-metrics-batch output, so the
# Volume component was silently 0 for every ticker — see E2E 2026-05-11).
# ---------------------------------------------------------------------------


def test_volume_metrics_present_when_enough_bars():
    """50+ bars → vol_5d_avg, vol_50d_avg, vol_ratio all populated."""
    closes = [100.0 + i for i in range(60)]
    volumes = [1_000_000] * 55 + [2_000_000] * 5  # surge at the tail
    metrics = compute_horizon_metrics(_bars(closes, volumes), ticker="X", market="US")
    assert metrics.vol_5d_avg == pytest.approx(2_000_000.0)
    # Last 50 bars: 45 × 1M + 5 × 2M  →  mean = (45 + 10) / 50 = 1.1M
    assert metrics.vol_50d_avg == pytest.approx(1_100_000.0)
    assert metrics.vol_ratio == pytest.approx(2_000_000.0 / 1_100_000.0)


def test_volume_ratio_surge_above_threshold():
    """A 2× surge in the last 5 days produces vol_ratio > 1.3 (the SKILL gate)."""
    closes = [100.0] * 60
    volumes = [1_000_000] * 55 + [3_000_000] * 5  # 3× surge
    metrics = compute_horizon_metrics(_bars(closes, volumes), ticker="X", market="US")
    assert metrics.vol_ratio is not None
    assert metrics.vol_ratio > 1.3  # would award +1.0 in the expect point table


def test_volume_metrics_none_when_under_50_bars():
    """Fewer than 50 bars → vol_50d_avg and vol_ratio None (vol_5d still OK)."""
    closes = [100.0 + i for i in range(30)]
    metrics = compute_horizon_metrics(_bars(closes), ticker="X", market="US")
    assert metrics.vol_5d_avg == pytest.approx(1_000_000.0)
    assert metrics.vol_50d_avg is None
    assert metrics.vol_ratio is None


def test_volume_metrics_none_when_under_5_bars():
    """Fewer than 5 bars → all three volume fields None."""
    closes = [100.0, 101.0, 102.0]
    metrics = compute_horizon_metrics(_bars(closes), ticker="X", market="US")
    assert metrics.vol_5d_avg is None
    assert metrics.vol_50d_avg is None
    assert metrics.vol_ratio is None


def test_volume_zero_avg_returns_none_ratio():
    """If 50-day average is 0, ratio must be None (not ZeroDivisionError)."""
    closes = [100.0 + i for i in range(60)]
    # Last 50 bars all zero volume; last 5 nonzero — the 50d average covers
    # the *last* 50 bars (which includes the tail-5), so we use a layout
    # where bars[-50:-5] are zero and the tail is nonzero.
    volumes = [0] * 55 + [0] * 5  # all zero in the last-50 window
    metrics = compute_horizon_metrics(_bars(closes, volumes), ticker="X", market="US")
    assert metrics.vol_50d_avg == 0.0
    assert metrics.vol_ratio is None  # would have been ZeroDivisionError otherwise
