"""Offline tests for the as-of backtest harness (no network).

Covers date slicing, the forward simulator (HIT/MISS/EXPIRED + censoring guard),
trailing-bucket mapping, the bootstrap ship-gate, and the as-of discovery
functions on synthetic bars.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "mcp-market-data"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "mcp-prediction-store"))

import asof_backtest as ab  # noqa: E402
import asof_discovery as ad  # noqa: E402
from asof_discovery import AsofPick  # noqa: E402


@dataclass
class FakeBar:
    date: str
    close: float
    volume: int


def _pick(entry=100.0, trailing=0.05):
    return AsofPick("T", "momentum", "momentum", entry, trailing, 1.0)


# ---------------------------------------------------------------------------
# Slicing
# ---------------------------------------------------------------------------


def test_slice_keeps_only_bars_on_or_before_asof():
    bars = [FakeBar(f"2026-01-{d:02d}", 100.0, 1) for d in range(1, 11)]
    sliced = ab.slice_bars_asof(bars, "2026-01-05")
    assert [b.date for b in sliced] == [f"2026-01-0{d}" for d in range(1, 6)]


# ---------------------------------------------------------------------------
# Forward simulator
# ---------------------------------------------------------------------------


def test_simulate_hit():
    status, ret = ab.simulate_pick(_pick(entry=100.0), [101.0, 104.0], required_days=2)
    assert status == "HIT" and round(ret, 4) == 0.04


def test_simulate_miss():
    status, ret = ab.simulate_pick(_pick(entry=100.0), [94.0, 110.0], required_days=2)
    assert status == "MISS" and round(ret, 4) == -0.06


def test_simulate_miss_takes_priority_over_later_hit():
    status, _ = ab.simulate_pick(_pick(entry=100.0), [94.0, 104.0], required_days=2)
    assert status == "MISS"


def test_simulate_expired():
    status, ret = ab.simulate_pick(_pick(entry=100.0), [100.0, 101.0], required_days=2)
    assert status == "EXPIRED" and round(ret, 4) == 0.01


def test_simulate_none_when_insufficient_forward_bars():
    assert ab.simulate_pick(_pick(entry=100.0), [101.0], required_days=2) is None


# ---------------------------------------------------------------------------
# Bucket mapping
# ---------------------------------------------------------------------------


def test_bucket_boundaries():
    assert ab._bucket_of(-0.05) == "<0%"
    assert ab._bucket_of(0.0) == "0-10%"
    assert ab._bucket_of(0.10) == "10-20%"
    assert ab._bucket_of(0.20) == "20-40%"
    assert ab._bucket_of(0.40) == ">40%"
    assert ab._bucket_of(None) is None


# ---------------------------------------------------------------------------
# Bootstrap ship gate
# ---------------------------------------------------------------------------


def test_ship_gate_passes_when_presurge_dominates():
    gate = ab._bootstrap_delta_ci([1] * 20, [0] * 20)
    assert gate["pass"] is True and gate["ci_low"] > 0


def test_ship_gate_fails_when_cohorts_equal():
    gate = ab._bootstrap_delta_ci([1, 0] * 20, [1, 0] * 20)
    assert gate["pass"] is False and gate["ci_low"] <= 0


def test_ship_gate_none_when_a_cohort_empty():
    assert ab._bootstrap_delta_ci([], [1, 0]) is None


# ---------------------------------------------------------------------------
# As-of discovery
# ---------------------------------------------------------------------------


def test_momentum_asof_picks_surger_not_flat():
    surger = [FakeBar(f"2026-01-{d:02d}", 100.0, 1000) for d in range(1, 30)]
    surger.append(FakeBar("2026-01-30", 120.0, 1000))  # +20% over 5d window
    flat = [FakeBar(f"2026-01-{d:02d}", 100.0, 1000) for d in range(1, 31)]
    picks = ad.discover_momentum_asof({"UP": surger, "FLAT": flat})
    tickers = [p.ticker for p in picks]
    assert "UP" in tickers and "FLAT" not in tickers
    up = next(p for p in picks if p.ticker == "UP")
    assert up.discovery_source == "momentum" and up.entry_close == 120.0
    assert round(up.trailing_20d_return, 4) == 0.20


def test_presurge_asof_tags_and_respects_min_score(monkeypatch):
    bars = [FakeBar(f"2026-01-{d:02d}", 100.0, 1000) for d in range(1, 30)]

    # Return a real SetupHit for AAA only, nothing for BBB.
    import pre_surge_discovery as psd

    def scorer(metrics, closes, volumes, **kw):
        return [psd.SetupHit("pullback", 0.9)] if metrics.ticker == "AAA" else []

    monkeypatch.setattr(ad, "score_presurge_setups", scorer)
    picks = ad.discover_presurge_asof({"AAA": bars, "BBB": bars}, min_score=0.5)
    assert [p.ticker for p in picks] == ["AAA"]
    assert picks[0].discovery_source == "presurge" and picks[0].setup_type == "pullback"


def test_build_report_shape():
    results = [
        ab.SimResult(
            "A", "presurge", "pullback", "2026-01-05", 100.0, "HIT", 0.04, 0.12
        ),
        ab.SimResult(
            "B", "presurge", "base_pivot", "2026-01-05", 100.0, "MISS", -0.06, 0.05
        ),
        ab.SimResult(
            "C", "momentum", "momentum", "2026-01-05", 100.0, "MISS", -0.06, 0.35
        ),
    ]
    report = ab.build_report(results, skipped=2)
    assert report["total_simulated"] == 3
    assert report["presurge"]["n"] == 2 and report["momentum"]["n"] == 1
    assert report["skipped_insufficient_forward"] == 2
