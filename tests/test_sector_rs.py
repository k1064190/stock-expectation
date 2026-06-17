"""Tests for the sector-rotation relative-strength screener (sector_rs.py).

All inputs are controlled HorizonMetrics / synthetic close series — no network.
Covers the three axes (RS / breadth / stage), the verdict map, the score
blend, and the never-raise NEUTRAL floors (missing benchmark, missing ETF,
empty constituents).
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp-market-data"))

from indicators import HorizonMetrics  # noqa: E402
from sector_rs import (  # noqa: E402
    SectorVerdict,
    classify_stage,
    compute_breadth,
    compute_sector_verdict,
    rank_sectors,
)


def _metrics(
    *,
    ticker="ETF",
    price=100.0,
    ma20=95.0,
    ma50=98.0,
    rsi14=55.0,
    return_1m=0.05,
    overext="NONE",
) -> HorizonMetrics:
    """HorizonMetrics with the sector-RS-relevant fields set, rest neutral."""
    return HorizonMetrics(
        ticker=ticker,
        market="US",
        current_price=price,
        ma20=ma20,
        ma50=ma50,
        ma200=None,
        rsi14=rsi14,
        return_1w=None,
        return_1m=return_1m,
        return_6m=None,
        return_1y=None,
        pct_from_52w_high=None,
        pct_from_52w_low=None,
        max_drawdown_1y=None,
        cycle_risk_flag=False,
        vol_5d_avg=None,
        vol_50d_avg=None,
        vol_ratio=None,
        overextension_level=overext,
    )


# --- classify_stage -------------------------------------------------------- #
def test_stage_early_band():
    """RSI in [45,65], price within +/-8% of MA50, no overext, rs_1m>0 -> EARLY."""
    m = _metrics(price=100.0, ma50=98.0, rsi14=55.0, overext="NONE")
    assert classify_stage(m, rs_1m=0.03) == "EARLY"


def test_stage_late_on_overextension():
    """ELEVATED/EXTREME overextension -> LATE regardless of the other bands."""
    m = _metrics(rsi14=55.0, overext="ELEVATED")
    assert classify_stage(m, rs_1m=0.03) == "LATE"


def test_stage_late_on_high_rsi():
    """rsi14>70 -> LATE."""
    m = _metrics(rsi14=78.0, overext="NONE")
    assert classify_stage(m, rs_1m=0.03) == "LATE"


def test_stage_late_on_far_above_ma50():
    """Price >15% above MA50 -> LATE."""
    m = _metrics(price=120.0, ma50=100.0, rsi14=60.0, overext="NONE")
    assert classify_stage(m, rs_1m=0.03) == "LATE"


def test_stage_mid_when_neither():
    """Constructive RSI but rs_1m<=0 (fails EARLY), not stretched -> MID."""
    m = _metrics(price=100.0, ma50=98.0, rsi14=55.0, overext="NONE")
    assert classify_stage(m, rs_1m=-0.01) == "MID"


# --- compute_breadth ------------------------------------------------------- #
def test_breadth_counts_leaders():
    """Fraction above MA20 AND beating the benchmark 1m return."""
    bench_1m = 0.02
    cons = [
        _metrics(price=110.0, ma20=100.0, return_1m=0.05),  # leads
        _metrics(price=110.0, ma20=100.0, return_1m=0.01),  # below benchmark
        _metrics(price=90.0, ma20=100.0, return_1m=0.05),  # below MA20
        _metrics(price=110.0, ma20=100.0, return_1m=0.08),  # leads
    ]
    assert compute_breadth(cons, bench_1m) == pytest.approx(0.5)


def test_breadth_skips_missing_fields():
    """Constituents missing MA20 or return_1m are excluded from the denominator."""
    bench_1m = 0.0
    cons = [
        _metrics(price=110.0, ma20=100.0, return_1m=0.05),  # leads, counted
        _metrics(price=110.0, ma20=None, return_1m=0.05),  # skipped
    ]
    assert compute_breadth(cons, bench_1m) == pytest.approx(1.0)


def test_breadth_none_when_no_usable_constituents():
    assert compute_breadth([], 0.02) is None


def test_breadth_none_when_benchmark_missing():
    cons = [_metrics(price=110.0, ma20=100.0, return_1m=0.05)]
    assert compute_breadth(cons, None) is None


# --- compute_sector_verdict: happy paths ----------------------------------- #
def test_early_band_sector_favors():
    """Strong 1m RS, broad breadth, EARLY stage -> FAVOR/EARLY."""
    etf = _metrics(ticker="XLK", price=100.0, ma50=98.0, rsi14=55.0, return_1m=0.06)
    bench = _metrics(ticker="SPY", return_1m=0.01)
    cons = [
        _metrics(price=110.0, ma20=100.0, return_1m=0.05),
        _metrics(price=110.0, ma20=100.0, return_1m=0.06),
        _metrics(price=110.0, ma20=100.0, return_1m=0.07),
    ]
    v = compute_sector_verdict(
        sector="Technology",
        etf_metrics=etf,
        etf_closes=None,
        benchmark_metrics=bench,
        benchmark_closes=None,
        constituent_metrics=cons,
    )
    assert v.stage == "EARLY"
    assert v.verdict == "FAVOR"
    assert v.benchmark == "SPY"
    assert v.rs_1m == pytest.approx(0.05)
    assert v.breadth_pct == pytest.approx(1.0)
    assert 0.0 <= v.score <= 100.0


def test_rotating_in_when_1m_up_3m_down_early():
    """rs_1m>0, rs_3m<=0, EARLY -> ROTATING_IN (fresh leadership)."""
    etf = _metrics(ticker="XLE", price=100.0, ma50=98.0, rsi14=55.0, return_1m=0.04)
    bench = _metrics(ticker="SPY", return_1m=0.01)
    # 3m proxy: ETF lagged the benchmark over 63 bars (rs_3m < 0).
    etf_closes = [100.0] * 63 + [102.0]  # +2% over 63 bars
    bench_closes = [100.0] * 63 + [108.0]  # +8% over 63 bars -> rs_3m = -6%
    # Breadth deliberately below FAVOR so FAVOR doesn't pre-empt ROTATING_IN.
    cons = [
        _metrics(price=90.0, ma20=100.0, return_1m=0.05),  # below MA20, not leading
    ]
    v = compute_sector_verdict(
        sector="Energy",
        etf_metrics=etf,
        etf_closes=etf_closes,
        benchmark_metrics=bench,
        benchmark_closes=bench_closes,
        constituent_metrics=cons,
    )
    assert v.stage == "EARLY"
    assert v.rs_3m is not None and v.rs_3m < 0
    assert v.verdict == "ROTATING_IN"


def test_overextended_sector_rotates_out_late():
    """Overextended ETF -> LATE; with weak breadth -> ROTATING_OUT."""
    etf = _metrics(
        ticker="XLY",
        price=130.0,
        ma50=100.0,
        rsi14=78.0,
        return_1m=0.10,
        overext="EXTREME",
    )
    bench = _metrics(ticker="SPY", return_1m=0.02)
    # Weak breadth: most constituents not leading.
    cons = [
        _metrics(price=90.0, ma20=100.0, return_1m=0.01),
        _metrics(price=95.0, ma20=100.0, return_1m=0.00),
    ]
    v = compute_sector_verdict(
        sector="Consumer Discretionary",
        etf_metrics=etf,
        etf_closes=None,
        benchmark_metrics=bench,
        benchmark_closes=None,
        constituent_metrics=cons,
    )
    assert v.stage == "LATE"
    assert v.verdict == "ROTATING_OUT"


def test_avoid_when_lagging_and_thin():
    """rs_1m<0 AND breadth<0.4 -> AVOID."""
    etf = _metrics(ticker="XLU", price=100.0, ma50=101.0, rsi14=45.0, return_1m=-0.02)
    bench = _metrics(ticker="SPY", return_1m=0.03)
    cons = [
        _metrics(price=90.0, ma20=100.0, return_1m=-0.01),
        _metrics(price=90.0, ma20=100.0, return_1m=-0.02),
    ]
    v = compute_sector_verdict(
        sector="Utilities",
        etf_metrics=etf,
        etf_closes=None,
        benchmark_metrics=bench,
        benchmark_closes=None,
        constituent_metrics=cons,
    )
    assert v.rs_1m is not None and v.rs_1m < 0
    assert v.breadth_pct == pytest.approx(0.0)
    assert v.verdict == "AVOID"


# --- never-raise NEUTRAL floors -------------------------------------------- #
def test_missing_benchmark_floors_to_neutral():
    """No benchmark metrics -> NEUTRAL floor, score 50, no exception."""
    etf = _metrics(ticker="XLK", return_1m=0.06)
    v = compute_sector_verdict(
        sector="Technology",
        etf_metrics=etf,
        etf_closes=None,
        benchmark_metrics=None,
        benchmark_closes=None,
        constituent_metrics=[],
    )
    assert v.verdict == "NEUTRAL"
    assert v.score == 50.0
    assert v.rs_1m is None
    assert v.benchmark == ""


def test_missing_etf_floors_to_neutral():
    """No sector ETF metrics -> NEUTRAL floor with the benchmark recorded."""
    bench = _metrics(ticker="SPY", return_1m=0.02)
    v = compute_sector_verdict(
        sector="Technology",
        etf_metrics=None,
        etf_closes=None,
        benchmark_metrics=bench,
        benchmark_closes=None,
        constituent_metrics=[],
    )
    assert v.verdict == "NEUTRAL"
    assert v.score == 50.0
    assert v.benchmark == "SPY"


def test_empty_constituents_graceful():
    """No constituents -> breadth None, still produces a verdict, no exception."""
    etf = _metrics(ticker="XLK", price=100.0, ma50=98.0, rsi14=55.0, return_1m=0.06)
    bench = _metrics(ticker="SPY", return_1m=0.01)
    v = compute_sector_verdict(
        sector="Technology",
        etf_metrics=etf,
        etf_closes=None,
        benchmark_metrics=bench,
        benchmark_closes=None,
        constituent_metrics=[],
    )
    assert v.breadth_pct is None
    assert v.verdict in ("FAVOR", "ROTATING_IN", "ROTATING_OUT", "AVOID", "NEUTRAL")
    # With no breadth, FAVOR (needs breadth>=0.5) cannot fire.
    assert v.verdict != "FAVOR"


# --- rank_sectors ---------------------------------------------------------- #
def test_rank_sorts_by_score_desc():
    a = SectorVerdict("A", "SPY", 0.0, 0.0, 0.0, "MID", "NEUTRAL", 30.0)
    b = SectorVerdict("B", "SPY", 0.0, 0.0, 0.0, "MID", "NEUTRAL", 80.0)
    c = SectorVerdict("C", "SPY", 0.0, 0.0, 0.0, "MID", "NEUTRAL", 55.0)
    ranked = rank_sectors([a, b, c])
    assert [v.sector for v in ranked] == ["B", "C", "A"]
