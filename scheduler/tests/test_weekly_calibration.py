"""Tests for the weekly calibration aggregator."""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import weekly_calibration as wc


@dataclass
class _FakeBucket:
    confidence_range: str
    predicted_confidence: float
    actual_accuracy: float
    count: int


@dataclass
class _FakeSignal:
    signal: str
    total: int
    wins: int
    win_rate: float
    p_value: float = 1.0
    verdict: str = "weak"


@dataclass
class _FakeTrack:
    total: int
    wins: int
    losses: int
    expired: int
    win_rate: float | None
    avg_return: float | None
    current_streak: int
    brier_score: float | None


def test_parse_since_days():
    assert wc.parse_since("30d") == 30
    assert wc.parse_since("30") == 30
    assert wc.parse_since("4w") == 28
    assert wc.parse_since("2m") == 60
    assert wc.parse_since("  90D ") == 90


def test_overconfident_buckets_flags_drift_above_threshold():
    buckets = [
        _FakeBucket("0.50-0.60", 0.55, 0.50, 10),  # drift +0.05 — fine
        _FakeBucket("0.60-0.70", 0.65, 0.40, 20),  # drift +0.25 — flagged
        _FakeBucket("0.70-0.80", 0.75, 0.85, 8),  # underconfident — fine
    ]
    flagged = wc.find_overconfident_buckets(buckets)
    assert flagged == ["0.60-0.70"]


def test_overconfident_buckets_skips_small_samples():
    """Buckets with <3 predictions are too noisy to flag."""
    buckets = [_FakeBucket("0.70-0.80", 0.75, 0.20, 2)]
    assert wc.find_overconfident_buckets(buckets) == []


def test_worst_signals_filters_by_volume_and_threshold():
    signals = [
        _FakeSignal(
            "technical", total=20, wins=8, win_rate=0.40
        ),  # boundary, NOT flagged
        _FakeSignal("news", total=15, wins=5, win_rate=0.33),  # flagged
        _FakeSignal("flaky", total=2, wins=0, win_rate=0.0),  # too few — not flagged
        _FakeSignal(
            "breadth", total=30, wins=18, win_rate=0.60
        ),  # winning — not flagged
    ]
    worst = wc.find_worst_signals(signals)
    assert len(worst) == 1
    assert worst[0]["signal"] == "news"


def test_worst_signals_flags_statistically_dead_verdict():
    """A signal with a 'dead' verdict is surfaced even if win_rate >= cutoff."""
    signals = [
        # win_rate 0.45 is above the 0.40 heuristic cutoff, but the binomial
        # verdict marks it a statistically significant anti-signal.
        _FakeSignal(
            "valuation", total=40, wins=18, win_rate=0.45, p_value=0.01, verdict="dead"
        ),
        _FakeSignal(
            "momentum", total=40, wins=28, win_rate=0.70, p_value=0.01, verdict="alive"
        ),
    ]
    worst = wc.find_worst_signals(signals)
    assert [w["signal"] for w in worst] == ["valuation"]
    assert worst[0]["verdict"] == "dead"


def test_render_markdown_uses_30d_as_primary_when_present():
    windows = [
        {
            "days": 7,
            "track_record": asdict_helper(_FakeTrack(5, 3, 2, 0, 0.60, 0.012, 1, 0.18)),
            "buckets": [],
            "signals": [],
            "overconfident_buckets": [],
            "worst_signals": [],
        },
        {
            "days": 30,
            "track_record": asdict_helper(
                _FakeTrack(40, 24, 14, 2, 0.63, 0.015, 3, 0.21)
            ),
            "buckets": [],
            "signals": [],
            "overconfident_buckets": ["0.70-0.80"],
            "worst_signals": [],
        },
    ]
    md = wc.render_markdown("2026-05-10", windows)
    assert "# Weekly calibration — 2026-05-10" in md
    # Headline section pulls from the 30-day window.
    assert "Total closed: **40**" in md
    assert "Win rate: **63.0%** (24W / 14L / 2E)" in md
    assert "Brier score: **0.210**" in md
    assert "Overconfident bucket(s): **0.70-0.80**" in md


def test_render_markdown_handles_no_data():
    """When all metrics are None, the report still renders without crashing."""
    windows = [
        {
            "days": 30,
            "track_record": asdict_helper(_FakeTrack(0, 0, 0, 0, None, None, 0, None)),
            "buckets": [],
            "signals": [],
            "overconfident_buckets": [],
            "worst_signals": [],
        }
    ]
    md = wc.render_markdown("2026-05-10", windows)
    assert "*insufficient data*" in md
    # No crash on missing brier/return.
    assert "Brier score" not in md or "n/a" in md


def test_update_trend_file_caps_history():
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "calibration-trend.json"
        # Pre-populate with 12 entries from prior weeks so we exceed the cap.
        existing = [
            {"report_date": f"2026-01-{i:02d}", "track_record": {}}
            for i in range(1, 13)
        ]
        state_path.write_text(json.dumps(existing), encoding="utf-8")

        new_entry = {"report_date": "2026-02-15", "track_record": {"total": 50}}
        wc.update_trend_file(state_path, new_entry)

        history = json.loads(state_path.read_text(encoding="utf-8"))
        assert len(history) == wc.TREND_HISTORY_WEEKS
        # New entry must be present, oldest dropped.
        assert history[-1]["report_date"] == "2026-02-15"
        assert (
            history[0]["report_date"] != "2026-01-01"
        )  # oldest from January was evicted


def test_update_trend_file_replaces_same_day_entry():
    """Re-running on the same day overwrites rather than duplicating."""
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "calibration-trend.json"
        wc.update_trend_file(
            state_path, {"report_date": "2026-05-10", "track_record": {"v": 1}}
        )
        wc.update_trend_file(
            state_path, {"report_date": "2026-05-10", "track_record": {"v": 2}}
        )

        history = json.loads(state_path.read_text(encoding="utf-8"))
        assert len(history) == 1
        assert history[0]["track_record"]["v"] == 2


def test_compute_window_uses_metrics_helpers():
    """compute_window delegates to the metrics module — verify wiring with mocks."""
    fake_track = _FakeTrack(10, 6, 3, 1, 0.67, 0.02, 2, 0.19)
    fake_buckets = [_FakeBucket("0.60-0.70", 0.65, 0.50, 5)]
    fake_signals = [_FakeSignal("technical", 10, 6, 0.60)]

    with (
        patch("weekly_calibration.get_track_record", return_value=fake_track),
        patch("weekly_calibration.get_calibration_report", return_value=fake_buckets),
        patch("weekly_calibration.get_signal_performance", return_value=fake_signals),
    ):
        window = wc.compute_window(conn=None, days=30)

    assert window["days"] == 30
    assert window["track_record"]["total"] == 10
    assert window["track_record"]["win_rate"] == 0.67
    assert window["buckets"][0]["confidence_range"] == "0.60-0.70"
    assert window["signals"][0]["signal"] == "technical"


def asdict_helper(obj):
    """Convert dataclass to dict for fixture building (avoid importing real metrics types)."""
    from dataclasses import asdict as _asdict

    return _asdict(obj)
