"""Tests for the pure helpers in paper_trading_run (no network/DB)."""

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import paper_trading_run as ptr


@dataclass
class _Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def test_signal_local_date_converts_utc_to_market_date():
    # KR pre-open signal 07:00 KST == prior UTC day, but local KR date is 05-01.
    assert ptr.signal_local_date("2026-04-30T22:00:00+00:00", "KR") == "2026-05-01"
    # US signal 08:00 ET (12:00 UTC) is local ET date 05-01.
    assert ptr.signal_local_date("2026-05-01T12:00:00+00:00", "US") == "2026-05-01"


def test_effective_entry_date_picks_next_trading_session():
    dates = ["2026-05-01", "2026-05-04", "2026-05-05"]
    assert (
        ptr.effective_entry_date("2026-05-01", dates) == "2026-05-01"
    )  # same day trades
    assert (
        ptr.effective_entry_date("2026-05-02", dates) == "2026-05-04"
    )  # weekend → next session
    assert ptr.effective_entry_date("2026-05-06", dates) is None  # past the range


def test_horizon_end_maps_timeframe_to_calendar_days():
    assert ptr.horizon_end("2026-05-01", "1W") == "2026-05-08"
    assert ptr.horizon_end("2026-05-01", "1M") == "2026-05-31"
    assert ptr.horizon_end("2026-05-01", "3M") == "2026-07-30"


def test_day_candidates_dedups_ticker_keeping_highest_confidence():
    rows = [
        {
            "ticker": "AAA",
            "conf": 0.62,
            "timeframe": "1M",
            "cdate": "2026-05-01",
            "entry_price": 100.0,
            "target_price": 120.0,
            "stop_price": 90.0,
            "id": "p1",
        },
        {
            "ticker": "AAA",  # same ticker, higher confidence wins
            "conf": 0.81,
            "timeframe": "1W",
            "cdate": "2026-05-01",
            "entry_price": 100.0,
            "target_price": 115.0,
            "stop_price": 95.0,
            "id": "p2",
        },
        {
            "ticker": "BBB",
            "conf": 0.70,
            "timeframe": "1M",
            "cdate": "2026-05-01",
            "entry_price": 50.0,
            "target_price": 60.0,
            "stop_price": 45.0,
            "id": "p3",
        },
    ]
    cands = ptr.day_candidates(rows)
    by_ticker = {c.ticker: c for c in cands}
    assert set(by_ticker) == {"AAA", "BBB"}
    assert by_ticker["AAA"].confidence == pytest.approx(0.81)
    assert by_ticker["AAA"].prediction_id == "p2"  # the higher-confidence row
    assert by_ticker["AAA"].stop_price == pytest.approx(95.0)
    assert by_ticker["AAA"].horizon_end_date == "2026-05-08"  # 1W from 2026-05-01


def test_day_candidates_horizon_from_entry_date_when_delayed():
    """A weekend/holiday-delayed signal measures its horizon from the entry session."""
    rows = [
        {
            "ticker": "AAA",
            "conf": 0.7,
            "timeframe": "1W",
            "cdate": "2026-05-01",  # signal date (a Friday, say)
            "entry_date": "2026-05-04",  # actual fill session (next Monday)
            "entry_price": 100.0,
            "target_price": 120.0,
            "stop_price": 90.0,
            "id": "p1",
        }
    ]
    cand = ptr.day_candidates(rows)[0]
    assert (
        cand.horizon_end_date == "2026-05-11"
    )  # 1W from entry 05-04, not signal 05-01


def test_build_price_map_nests_by_ticker_then_date():
    batch = {
        "AAA": [
            _Bar("2026-05-01", 100, 101, 99, 100),
            _Bar("2026-05-02", 100, 105, 100, 104),
        ],
        "BBB": [_Bar("2026-05-01", 50, 51, 49, 50)],
    }
    pm = ptr.build_price_map(batch)
    assert pm["AAA"]["2026-05-02"]["close"] == 104
    assert pm["AAA"]["2026-05-02"]["high"] == 105
    assert pm["BBB"]["2026-05-01"]["low"] == 49


def test_trading_dates_filters_to_range_sorted():
    bmap = {
        "2026-04-30": {"close": 1.0},
        "2026-05-01": {"close": 1.0},
        "2026-05-04": {"close": 1.0},
        "2026-05-10": {"close": 1.0},
    }
    assert ptr.trading_dates(bmap, "2026-05-01", "2026-05-04") == [
        "2026-05-01",
        "2026-05-04",
    ]


def test_benchmark_nav_tracks_index_buy_and_hold():
    bmap = {"2026-05-01": {"close": 100.0}, "2026-05-02": {"close": 110.0}}
    assert ptr.benchmark_nav(
        100_000.0, bmap, "2026-05-01", "2026-05-01"
    ) == pytest.approx(100_000.0)
    # +10% index move → +10% benchmark NAV.
    assert ptr.benchmark_nav(
        100_000.0, bmap, "2026-05-02", "2026-05-01"
    ) == pytest.approx(110_000.0)
