"""Tests for deterministic market-regime classification (mcp-market-data/regime.py).

Covers realized-volatility computation and the RISK_ON/NEUTRAL/RISK_OFF scoring
across each component, on controlled HorizonMetrics inputs (no network).
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp-market-data"))

from indicators import HorizonMetrics  # noqa: E402
from regime import (  # noqa: E402
    aggregate_regime,
    compute_realized_vol,
    compute_regime,
)


def _metrics(
    *,
    price=100.0,
    ma50=90.0,
    ma200=80.0,
    return_1m=0.05,
    pct_from_52w_high=-0.01,
) -> HorizonMetrics:
    """Build a HorizonMetrics with regime-relevant fields set, rest neutral."""
    return HorizonMetrics(
        ticker="IDX",
        market="US",
        current_price=price,
        ma20=None,
        ma50=ma50,
        ma200=ma200,
        rsi14=None,
        return_1w=None,
        return_1m=return_1m,
        return_6m=None,
        return_1y=None,
        pct_from_52w_high=pct_from_52w_high,
        pct_from_52w_low=None,
        max_drawdown_1y=None,
        cycle_risk_flag=False,
        vol_5d_avg=None,
        vol_50d_avg=None,
        vol_ratio=None,
        overextension_level="NONE",  # regime tests don't exercise the R2 gate
    )


# --- compute_realized_vol -------------------------------------------------- #
def test_realized_vol_constant_prices_is_zero():
    assert compute_realized_vol([100.0] * 25) == pytest.approx(0.0)


def test_realized_vol_insufficient_data_is_none():
    assert compute_realized_vol([100.0] * 5, window=20) is None


def test_realized_vol_positive_for_volatile_series():
    closes = [100.0 * (1.02 if i % 2 else 0.98) ** 1 for i in range(30)]
    v = compute_realized_vol(closes)
    assert v is not None and v > 0.1


def test_realized_vol_rejects_nonpositive_prices():
    assert compute_realized_vol([100.0] * 19 + [0.0, 100.0]) is None


# --- compute_regime -------------------------------------------------------- #
def test_regime_risk_on_strong_uptrend():
    v = compute_regime(_metrics(), realized_vol_annual=0.15)
    assert v.label == "RISK_ON"
    assert v.score == 0
    assert v.components == {"trend": 0, "drawdown": 0, "momentum": 0, "volatility": 0}


def test_regime_risk_off_full_breakdown():
    # Below MA200, deep drawdown, sharp 1M decline, turbulent vol.
    v = compute_regime(
        _metrics(
            price=70.0, ma50=90.0, ma200=80.0, return_1m=-0.12, pct_from_52w_high=-0.18
        ),
        realized_vol_annual=0.35,
    )
    assert v.label == "RISK_OFF"
    assert v.components == {"trend": 2, "drawdown": 2, "momentum": 2, "volatility": 2}
    assert v.score == 8


def test_regime_neutral_mild_weakness():
    # Below MA50 but above MA200 (+1), shallow pullback (+1), flat momentum, calm vol.
    v = compute_regime(
        _metrics(
            price=85.0, ma50=90.0, ma200=80.0, return_1m=0.0, pct_from_52w_high=-0.06
        ),
        realized_vol_annual=0.15,
    )
    assert v.label == "NEUTRAL"
    assert v.components["trend"] == 1
    assert v.components["drawdown"] == 1
    assert v.score == 2


def test_regime_below_ma200_dominates_trend_points():
    # Below both MAs scores the MA200 breach (+2), not stacked with the MA50 one.
    v = compute_regime(
        _metrics(
            price=70.0, ma50=90.0, ma200=80.0, return_1m=0.05, pct_from_52w_high=-0.01
        ),
        realized_vol_annual=0.15,
    )
    assert v.components["trend"] == 2


def test_regime_insufficient_history_floors_to_neutral():
    # No MA200 → primary trend unknown → a hard gate must not certify RISK_ON.
    m = _metrics(ma50=None, ma200=None, return_1m=None, pct_from_52w_high=None)
    v = compute_regime(m, realized_vol_annual=None)
    assert v.label == "NEUTRAL"
    assert v.score == 0  # score untouched for transparency
    assert any("insufficient history" in n for n in v.notes)


def test_regime_ma50_missing_ma200_present():
    # ma50 None but ma200 present and breached → MA200 trend points (+2), no floor.
    m = _metrics(
        price=70.0, ma50=None, ma200=80.0, return_1m=0.05, pct_from_52w_high=-0.01
    )
    v = compute_regime(m, realized_vol_annual=0.15)
    assert v.components["trend"] == 2


def test_realized_vol_exact_value():
    # Alternating +1%/-1% log-return series: deterministic, lets us pin the math
    # (sample stddev n-1, sqrt(252) annualization) against regressions.
    closes = [100.0]
    for i in range(20):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    v = compute_realized_vol(closes, window=20)
    # |log(1.01)| per step, near-zero mean → stddev ~= |log(1.01)|, annualized.
    import math

    expected = math.log(1.01) * math.sqrt(252.0)
    assert v == pytest.approx(expected, rel=0.05)


# --- aggregate_regime ------------------------------------------------------ #
def test_aggregate_picks_worst_and_records_proxies():
    calm = compute_regime(_metrics(), realized_vol_annual=0.12)  # SPY-like RISK_ON
    weak = compute_regime(  # QQQ-like, drawn down + volatile
        _metrics(
            price=85.0, ma50=90.0, ma200=80.0, return_1m=0.0, pct_from_52w_high=-0.06
        ),
        realized_vol_annual=0.24,
    )
    calm.index_ticker = "SPY"
    weak.index_ticker = "QQQ"
    agg = aggregate_regime([calm, weak])
    assert agg.index_ticker == "QQQ"  # the worse one drives the verdict
    assert agg.label == weak.label
    assert agg.proxy_scores == {"SPY": calm.score, "QQQ": weak.score}


def test_aggregate_does_not_mutate_inputs():
    calm = compute_regime(_metrics(), realized_vol_annual=0.12)
    calm.index_ticker = "SPY"
    aggregate_regime([calm])
    assert calm.proxy_scores == {}  # input untouched (replace returns a copy)


def test_aggregate_tie_resolves_to_first():
    a = compute_regime(_metrics(), realized_vol_annual=0.12)
    b = compute_regime(_metrics(), realized_vol_annual=0.12)
    a.index_ticker, b.index_ticker = "SPY", "QQQ"
    assert a.score == b.score
    assert aggregate_regime([a, b]).index_ticker == "SPY"  # first on tie


def test_aggregate_single_proxy():
    v = compute_regime(_metrics(), realized_vol_annual=0.12)
    v.index_ticker = "SPY"
    agg = aggregate_regime([v])
    assert agg.proxy_scores == {"SPY": v.score}


def test_aggregate_empty_raises():
    with pytest.raises(ValueError):
        aggregate_regime([])


# --- aggregate_regime ------------------------------------------------------ #
def test_aggregate_picks_worst_and_records_proxies():
    calm = compute_regime(_metrics(), realized_vol_annual=0.12)  # SPY-like RISK_ON
    weak = compute_regime(  # QQQ-like, drawn down + volatile
        _metrics(
            price=85.0, ma50=90.0, ma200=80.0, return_1m=0.0, pct_from_52w_high=-0.06
        ),
        realized_vol_annual=0.24,
    )
    calm.index_ticker = "SPY"
    weak.index_ticker = "QQQ"
    agg = aggregate_regime([calm, weak])
    assert agg.index_ticker == "QQQ"  # the worse one drives the verdict
    assert agg.label == weak.label
    assert agg.proxy_scores == {"SPY": calm.score, "QQQ": weak.score}


def test_aggregate_empty_raises():
    with pytest.raises(ValueError):
        aggregate_regime([])
