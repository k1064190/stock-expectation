"""Unit tests for scheduler.pre_surge_discovery and the new indicator helpers.

All tests are offline and deterministic — the discovery-pipeline test uses a
FakeProvider and monkeypatches the scorer, so the detector logic is exercised
directly via constructed HorizonMetrics rather than reverse-engineered bars.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "mcp-market-data"))

import pre_surge_discovery as psd  # noqa: E402
from indicators import (  # noqa: E402
    HorizonMetrics,
    compute_return_stdev,
    contraction_ratio,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeBar:
    """Minimal OHLCV substitute — only close + volume are read here."""

    close: float
    volume: int


class FakeProvider:
    """Mock provider with batch + single price history (for the benchmark)."""

    def __init__(self, bars_by_ticker, benchmark=None, raise_on_batch=False):
        self._bars = bars_by_ticker
        self._benchmark = benchmark or []
        self._raise = raise_on_batch

    def get_price_history_batch(self, tickers, days=30):
        if self._raise:
            raise RuntimeError("simulated provider failure")
        return {t: self._bars.get(t, []) for t in tickers}

    def get_price_history(self, ticker, days=30):
        return self._benchmark


def _mk_metrics(**overrides) -> HorizonMetrics:
    """Build a HorizonMetrics with all-None defaults, overriding select fields."""
    base = dict(
        ticker="T",
        market="US",
        current_price=100.0,
        ma20=None,
        ma50=None,
        ma200=None,
        rsi14=None,
        return_1w=None,
        return_1m=None,
        return_6m=None,
        return_1y=None,
        pct_from_52w_high=None,
        pct_from_52w_low=None,
        max_drawdown_1y=None,
        cycle_risk_flag=False,
        vol_5d_avg=None,
        vol_50d_avg=None,
        vol_ratio=None,
        overextension_level="NONE",
    )
    base.update(overrides)
    return HorizonMetrics(**base)


def _contracting_closes() -> list[float]:
    """31 closes whose recent-10 daily vol is far below the prior-20 vol.

    Prior window alternates +/-3% (high stdev); recent window +/-0.1% (low
    stdev) so contraction_ratio(closes, 10, 20) is well below 0.75.
    """
    closes = [100.0]
    for i in range(20):  # prior 20 volatile returns
        closes.append(closes[-1] * (1.03 if i % 2 == 0 else 0.97))
    for i in range(10):  # recent 10 quiet returns
        closes.append(closes[-1] * (1.001 if i % 2 == 0 else 0.999))
    return closes


def _dryup_volumes() -> list[float]:
    """60 volumes: older window high, the [-20:-5] window lower (dry-up)."""
    older = [3000.0] * 30  # [-60:-30] roughly -> contributes to [-50:-20]
    mid = [3000.0] * 10
    dry = [1000.0] * 15  # maps into [-20:-5]
    last = [1500.0] * 5
    return older + mid + dry + last


# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------


def test_compute_return_stdev_insufficient_bars():
    assert compute_return_stdev([100.0, 101.0], 5) is None
    assert compute_return_stdev([100.0, 101.0, 102.0], 1) is None  # window < 2


def test_compute_return_stdev_zero_for_constant_series():
    assert compute_return_stdev([100.0] * 11, 10) == pytest.approx(0.0)


def test_contraction_ratio_below_one_when_recent_quiet():
    cr = contraction_ratio(_contracting_closes(), 10, 20)
    assert cr is not None and cr < 0.75


def test_contraction_ratio_none_when_too_short():
    assert contraction_ratio([100.0] * 10, 10, 20) is None


# ---------------------------------------------------------------------------
# Detectors via the pure scorer
# ---------------------------------------------------------------------------


def test_base_pivot_matches_tight_coil():
    metrics = _mk_metrics(
        current_price=100.0,
        ma20=100.0,
        ma50=98.0,
        rsi14=55.0,
        return_1m=0.08,
        vol_ratio=1.2,
        overextension_level="NONE",
    )
    hits = psd.score_presurge_setups(metrics, _contracting_closes(), _dryup_volumes())
    assert any(h.setup_type == "base_pivot" and h.score >= 0.5 for h in hits)


def test_base_pivot_rejected_when_overextended():
    metrics = _mk_metrics(
        current_price=100.0,
        ma20=100.0,
        ma50=98.0,
        rsi14=55.0,
        return_1m=0.08,
        vol_ratio=1.2,
        overextension_level="EXTREME",
    )
    hits = psd.score_presurge_setups(metrics, _contracting_closes(), _dryup_volumes())
    assert not any(h.setup_type == "base_pivot" for h in hits)


def test_base_pivot_rejected_when_parabolic_return():
    metrics = _mk_metrics(
        current_price=100.0,
        ma20=100.0,
        ma50=98.0,
        rsi14=55.0,
        return_1m=0.25,
        vol_ratio=1.2,
        overextension_level="NONE",
    )
    hits = psd.score_presurge_setups(metrics, _contracting_closes(), _dryup_volumes())
    assert not any(h.setup_type == "base_pivot" for h in hits)


def test_pullback_matches_dip_in_uptrend():
    metrics = _mk_metrics(
        current_price=100.0,
        ma20=99.0,
        ma50=95.0,
        ma200=90.0,
        rsi14=48.0,
        overextension_level="NONE",
    )
    hits = psd.score_presurge_setups(metrics, [100.0] * 5, [1000.0] * 5)
    assert any(h.setup_type == "pullback" and h.score >= 0.5 for h in hits)


def test_pullback_rejected_without_ma_stack():
    metrics = _mk_metrics(
        current_price=100.0,
        ma20=95.0,
        ma50=99.0,
        ma200=90.0,
        rsi14=48.0,
    )  # ma20 < ma50 => not a stacked uptrend
    hits = psd.score_presurge_setups(metrics, [100.0] * 5, [1000.0] * 5)
    assert not any(h.setup_type == "pullback" for h in hits)


def test_rs_leader_matches_modest_outperformer():
    metrics = _mk_metrics(return_1m=0.12, return_6m=0.30, overextension_level="NONE")
    hits = psd.score_presurge_setups(
        metrics,
        [100.0] * 5,
        [1000.0] * 5,
        benchmark_return_1m=0.03,
        benchmark_return_6m=0.10,
    )
    assert any(h.setup_type == "rs_leader" and h.score >= 0.5 for h in hits)


def test_rs_leader_rejected_when_parabolic():
    metrics = _mk_metrics(return_1m=0.35, return_6m=0.50)
    hits = psd.score_presurge_setups(
        metrics,
        [100.0] * 5,
        [1000.0] * 5,
        benchmark_return_1m=0.03,
    )
    assert not any(h.setup_type == "rs_leader" for h in hits)


def test_rs_leader_rejected_without_benchmark():
    metrics = _mk_metrics(return_1m=0.12, return_6m=0.30)
    hits = psd.score_presurge_setups(metrics, [100.0] * 5, [1000.0] * 5)
    assert not any(h.setup_type == "rs_leader" for h in hits)


def test_pre_earnings_matches_in_window():
    metrics = _mk_metrics(return_1m=0.05, overextension_level="NONE")
    hits = psd.score_presurge_setups(
        metrics,
        _contracting_closes(),
        [1000.0] * 5,
        earnings_in_days=7,
    )
    assert any(h.setup_type == "pre_earnings" and h.score >= 0.5 for h in hits)


def test_pre_earnings_rejected_outside_window():
    metrics = _mk_metrics(return_1m=0.05)
    hits = psd.score_presurge_setups(
        metrics,
        _contracting_closes(),
        [1000.0] * 5,
        earnings_in_days=20,
    )
    assert not any(h.setup_type == "pre_earnings" for h in hits)


# ---------------------------------------------------------------------------
# best_setup tie-break
# ---------------------------------------------------------------------------


def test_best_setup_none_when_empty():
    assert psd.best_setup([]) is None


def test_best_setup_breaks_score_tie_by_priority():
    hits = [psd.SetupHit("base_pivot", 0.8), psd.SetupHit("pullback", 0.8)]
    assert psd.best_setup(hits).setup_type == "pullback"


def test_best_setup_prefers_higher_score_over_priority():
    hits = [psd.SetupHit("pullback", 0.6), psd.SetupHit("rs_leader", 0.9)]
    assert psd.best_setup(hits).setup_type == "rs_leader"


# ---------------------------------------------------------------------------
# Discovery pipeline
# ---------------------------------------------------------------------------


def test_discover_tags_filters_and_sorts(monkeypatch):
    monkeypatch.setattr(
        psd,
        "_load_static_us_universe",
        lambda: [("AAA", None, None), ("BBB", None, None), ("CCC", None, None)],
    )
    bars = [FakeBar(close=100.0 + i * 0.1, volume=1000) for i in range(40)]
    provider = FakeProvider({"AAA": bars, "BBB": bars, "CCC": bars}, benchmark=bars)

    def fake_score(metrics, closes, volumes, **kw):
        return {
            "AAA": [psd.SetupHit("pullback", 0.9)],
            "BBB": [psd.SetupHit("base_pivot", 0.6)],
            "CCC": [psd.SetupHit("rs_leader", 0.3)],  # below min_score => dropped
        }[metrics.ticker]

    monkeypatch.setattr(psd, "score_presurge_setups", fake_score)
    cands = psd.discover_presurge_candidates("US", provider=provider, min_score=0.5)

    assert [c.ticker for c in cands] == ["AAA", "BBB"]  # sorted by score, CCC filtered
    assert cands[0].discovery_source == "presurge"
    assert cands[0].setup_type == "pullback"
    assert all(c.reason == "presurge" for c in cands)


def test_discover_never_raises_on_provider_failure(monkeypatch):
    monkeypatch.setattr(psd, "_load_static_us_universe", lambda: [("AAA", None, None)])
    provider = FakeProvider({}, raise_on_batch=True)
    assert psd.discover_presurge_candidates("US", provider=provider) == []


def test_discover_empty_universe_returns_empty(monkeypatch):
    monkeypatch.setattr(psd, "_load_static_us_universe", lambda: [])
    provider = FakeProvider({})
    assert psd.discover_presurge_candidates("US", provider=provider) == []
