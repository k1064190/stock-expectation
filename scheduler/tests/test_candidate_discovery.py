"""Unit tests for scheduler.candidate_discovery.

All tests mock PyKRX and provider calls — no network. Live PyKRX smoke
is exercised separately (see the verification script in Stage A.5).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "mcp-market-data"))

from candidate_discovery import (  # noqa: E402
    ANCHORS_KR,
    Candidate,
    DEFAULT_RETURN_THRESHOLD_PCT,
    DEFAULT_VOL_RATIO_THRESHOLD,
    discover_kr_candidates,
    enumerate_kr_universe,
    format_candidates_for_prompt,
    score_and_filter,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeBar:
    """Minimal OHLCV substitute — score_and_filter only reads close and volume."""

    close: float
    volume: int


class FakeProvider:
    """Mock provider returning preconfigured OHLCV per ticker."""

    def __init__(self, bars_by_ticker: dict[str, list[FakeBar]]) -> None:
        self._bars = bars_by_ticker

    def get_price_history_batch(self, tickers, days=30):
        return {t: self._bars.get(t, []) for t in tickers}


def _bars_with_return(
    prev_close: float, last_close: float, length: int = 30
) -> list[FakeBar]:
    """Flat series ending in a single-day jump from prev_close to last_close.

    Volume stays constant at 1000 so vol_ratio_5d == 1.0 (won't trip volume
    filter on its own).
    """
    bars = [FakeBar(close=prev_close, volume=1000) for _ in range(length - 1)]
    bars.append(FakeBar(close=last_close, volume=1000))
    return bars


def _bars_with_vol_spike(spike_multiple: float, length: int = 30) -> list[FakeBar]:
    """Flat price, last 5 bars at ``spike_multiple * base_volume``.

    return_5d_pct stays 0, so only the volume filter can pass.
    """
    base_vol = 1000
    spike_vol = int(base_vol * spike_multiple)
    bars = []
    for i in range(length):
        vol = spike_vol if i >= length - 5 else base_vol
        bars.append(FakeBar(close=10_000.0, volume=vol))
    return bars


# ---------------------------------------------------------------------------
# enumerate_kr_universe
# ---------------------------------------------------------------------------


def _make_pykrx_df(rows: list[tuple[str, float, float]]) -> pd.DataFrame:
    """Build a DataFrame matching PyKRX's get_market_cap_by_ticker layout."""
    idx = [t for (t, _, _) in rows]
    return pd.DataFrame(
        {
            "종가": [0.0] * len(rows),
            "시가총액": [c for (_, c, _) in rows],
            "거래량": [0] * len(rows),
            "거래대금": [v for (_, _, v) in rows],
            "상장주식수": [0] * len(rows),
        },
        index=idx,
    )


def test_enumerate_union_top_cap_and_top_value():
    """Top-N by cap and top-N by trading value form a union, deduplicated."""
    df = _make_pykrx_df(
        [
            ("AAA001", 1_000e12, 100e8),  # cap top 1
            ("AAA002", 900e12, 50e8),  # cap top 2
            ("AAA003", 800e12, 60e8),  # cap top 3
            ("AAA004", 50e12, 999e8),  # value top 1 only
            ("AAA005", 40e12, 800e8),  # value top 2 only
            ("AAA006", 10e12, 10e8),  # neither
        ]
    )
    with patch("pykrx.stock.get_market_cap_by_ticker", return_value=df):
        result = enumerate_kr_universe(top_n_cap=3, top_n_value=2)

    tickers = sorted(t for (t, _, _) in result)
    # AAA001/002/003 by cap, AAA004/005 by value, AAA006 excluded
    assert tickers == ["AAA001", "AAA002", "AAA003", "AAA004", "AAA005"]


def test_enumerate_dedupes_overlap():
    """Ticker hitting both cap and value lists appears once."""
    df = _make_pykrx_df(
        [
            ("BBB001", 500e12, 800e8),  # cap top 1 AND value top 1
            ("BBB002", 400e12, 100e8),  # cap top 2
            ("BBB003", 50e12, 600e8),  # value top 2
        ]
    )
    with patch("pykrx.stock.get_market_cap_by_ticker", return_value=df):
        result = enumerate_kr_universe(top_n_cap=2, top_n_value=2)
    assert len(result) == 3
    assert sorted(t for (t, _, _) in result) == ["BBB001", "BBB002", "BBB003"]


def test_enumerate_returns_market_cap_and_trading_value():
    """Each tuple carries (ticker, market_cap, trading_value)."""
    df = _make_pykrx_df([("CCC001", 1234.0, 5678.0)])
    with patch("pykrx.stock.get_market_cap_by_ticker", return_value=df):
        result = enumerate_kr_universe(top_n_cap=10, top_n_value=10)
    assert result == [("CCC001", 1234.0, 5678.0)]


def test_enumerate_pykrx_failure_falls_back_to_static_csv():
    """PyKRX exception → load data/kr_universe.csv (the production state today
    since KRX's bulk endpoint is broken upstream)."""
    with patch(
        "pykrx.stock.get_market_cap_by_ticker", side_effect=RuntimeError("boom")
    ):
        out = enumerate_kr_universe()
    assert len(out) > 50, "expected static CSV to provide at least ~150 tickers"
    tickers = {t for (t, _, _) in out}
    # 5/13 actors + anchors must be in the CSV — that was the whole point.
    assert {"307950", "066570", "005380", "005930", "000660", "069500"} <= tickers
    # CSV-mode tuples carry None for cap/value (only the live bulk path knows).
    assert all(cap is None and val is None for (_, cap, val) in out)


def test_enumerate_unexpected_columns_falls_back_to_static_csv():
    """Missing 시가총액 or 거래대금 column → CSV fallback (was: empty list)."""
    df = pd.DataFrame({"foo": [1], "bar": [2]}, index=["DDD001"])
    with patch("pykrx.stock.get_market_cap_by_ticker", return_value=df):
        out = enumerate_kr_universe()
    tickers = {t for (t, _, _) in out}
    assert "307950" in tickers


def test_enumerate_empty_frame_falls_back_to_static_csv():
    """PyKRX returning an empty frame → CSV fallback (was: empty list)."""
    df = pd.DataFrame(columns=["시가총액", "거래대금"])
    with patch("pykrx.stock.get_market_cap_by_ticker", return_value=df):
        out = enumerate_kr_universe()
    tickers = {t for (t, _, _) in out}
    assert "307950" in tickers


def test_csv_ticker_validator_rejects_malformed_inputs():
    """`_normalise_csv_ticker` must reject blank, non-digit, and too-long
    inputs — defends both `_load_static_universe` and
    `_load_static_universe_names` from quarterly-edit typos and the
    `''.zfill(6) == '000000'` phantom-ticker trap (Codex PR #13 findings)."""
    from candidate_discovery import _normalise_csv_ticker

    # Valid: 1-6 digits → 6-digit zero-padded.
    assert _normalise_csv_ticker("005930") == "005930"
    assert _normalise_csv_ticker("5930") == "005930"
    assert _normalise_csv_ticker("  5930  ") == "005930"

    # Invalid: blank / whitespace.
    assert _normalise_csv_ticker("") is None
    assert _normalise_csv_ticker(None) is None  # type: ignore[arg-type]
    assert _normalise_csv_ticker("   ") is None

    # Invalid: non-digit characters.
    assert _normalise_csv_ticker("AAPL") is None
    assert _normalise_csv_ticker("00593O") is None  # capital O, not zero

    # Invalid: longer than 6 digits.
    assert _normalise_csv_ticker("1234567") is None


def test_enumerate_csv_missing_returns_empty(monkeypatch):
    """When BOTH PyKRX is broken AND the static CSV is unreadable, return []
    (anchors-only fallback in discover_kr_candidates still kicks in)."""
    from pathlib import Path

    import candidate_discovery as cd

    monkeypatch.setattr(cd, "STATIC_UNIVERSE_PATH", Path("/nonexistent.csv"))
    with patch(
        "pykrx.stock.get_market_cap_by_ticker", side_effect=RuntimeError("boom")
    ):
        assert enumerate_kr_universe() == []


# ---------------------------------------------------------------------------
# score_and_filter
# ---------------------------------------------------------------------------


def test_score_filter_passes_momentum():
    """Ticker with 5-day return > threshold survives with reason='momentum'."""
    universe = [("MOM001", 1.0e12, 1.0e8)]
    provider = FakeProvider({"MOM001": _bars_with_return(100.0, 155.0)})  # +55%
    survivors = score_and_filter(universe, provider, return_threshold_pct=15.0)
    assert len(survivors) == 1
    assert survivors[0].ticker == "MOM001"
    assert survivors[0].reason == "momentum"
    assert survivors[0].return_5d_pct > 15.0


def test_score_filter_passes_negative_momentum():
    """|return_5d| (i.e. crashes too) trips the momentum filter."""
    universe = [("DOWN01", 1.0e12, 1.0e8)]
    provider = FakeProvider({"DOWN01": _bars_with_return(100.0, 70.0)})  # -30%
    survivors = score_and_filter(universe, provider, return_threshold_pct=15.0)
    assert len(survivors) == 1
    assert survivors[0].reason == "momentum"
    assert survivors[0].return_5d_pct < -15.0


def test_score_filter_rejects_quiet_ticker():
    """Below both thresholds → filtered out entirely."""
    universe = [("QUIET1", 1.0e12, 1.0e8)]
    provider = FakeProvider({"QUIET1": _bars_with_return(100.0, 105.0)})  # +5%
    survivors = score_and_filter(
        universe, provider, return_threshold_pct=15.0, vol_ratio_threshold=2.0
    )
    assert survivors == []


def test_score_filter_passes_volume_when_return_fails():
    """Volume spike alone with flat price → survives with reason='volume'."""
    universe = [("VOL001", 1.0e12, 1.0e8)]
    provider = FakeProvider({"VOL001": _bars_with_vol_spike(3.0)})
    survivors = score_and_filter(
        universe, provider, return_threshold_pct=999.0, vol_ratio_threshold=2.0
    )
    assert len(survivors) == 1
    assert survivors[0].reason == "volume"
    assert survivors[0].vol_ratio_5d >= 2.0


def test_score_filter_skips_short_bars():
    """Tickers with < 6 bars are skipped — return_5d undefined."""
    universe = [("SHRT01", 1.0e12, 1.0e8)]
    provider = FakeProvider(
        {"SHRT01": [FakeBar(close=100.0, volume=1000) for _ in range(3)]}
    )
    assert score_and_filter(universe, provider) == []


def test_score_filter_attaches_cap_and_value():
    """Surviving Candidate carries market_cap and trading_value from universe."""
    universe = [("KEEP01", 2.5e12, 1.5e9)]
    provider = FakeProvider({"KEEP01": _bars_with_return(100.0, 200.0)})
    survivors = score_and_filter(universe, provider, return_threshold_pct=15.0)
    assert survivors[0].market_cap == 2.5e12
    assert survivors[0].trading_value == 1.5e9


def test_score_filter_empty_universe():
    """Empty input → empty output, no provider call assumed."""
    assert score_and_filter([], FakeProvider({})) == []


# ---------------------------------------------------------------------------
# discover_kr_candidates
# ---------------------------------------------------------------------------


def _patch_universe(rows):
    """Convenience to patch enumerate_kr_universe with a fake list."""
    return patch(
        "candidate_discovery.enumerate_kr_universe",
        return_value=rows,
    )


def _patch_pykrx_name(name_map: dict):
    """Patch the name lookup used by _fill_names."""
    return patch(
        "pykrx.stock.get_market_ticker_name",
        side_effect=lambda t: name_map.get(t, ""),
    )


def test_discover_includes_anchors_even_when_filter_empty():
    """All three anchors always appear, in declared order, even with no dynamic."""
    with _patch_universe([]), _patch_pykrx_name({}):
        out = discover_kr_candidates(top_n_output=20, provider=FakeProvider({}))
    tickers = [c.ticker for c in out]
    assert tickers[:3] == [t for t, _ in ANCHORS_KR]
    assert all(c.reason == "anchor" for c in out)


def test_discover_anchor_overlap_dedupes_and_relabels():
    """A ticker that's both an anchor and a dynamic survivor appears once,
    with reason='anchor' but fresh return numbers."""
    # 005930 is in ANCHORS_KR. Make it also pass the dynamic filter.
    universe_rows = [("005930", 500e12, 100e8)]
    with _patch_universe(universe_rows), _patch_pykrx_name({"005930": "삼성전자"}):
        out = discover_kr_candidates(
            top_n_output=20,
            provider=FakeProvider({"005930": _bars_with_return(50000.0, 80000.0)}),
        )
    samsung = [c for c in out if c.ticker == "005930"]
    assert len(samsung) == 1
    assert samsung[0].reason == "anchor"
    assert samsung[0].return_5d_pct > 15.0  # carried from dynamic filter


def test_discover_sorts_dynamic_by_abs_return():
    """Dynamic survivors after anchors are sorted by descending |return_5d|."""
    universe_rows = [
        ("DYN001", 100e12, 10e8),
        ("DYN002", 100e12, 10e8),
        ("DYN003", 100e12, 10e8),
    ]
    bars = {
        "DYN001": _bars_with_return(100.0, 120.0),  # +20
        "DYN002": _bars_with_return(100.0, 160.0),  # +60
        "DYN003": _bars_with_return(100.0, 140.0),  # +40
    }
    with (
        _patch_universe(universe_rows),
        _patch_pykrx_name({"DYN001": "n1", "DYN002": "n2", "DYN003": "n3"}),
    ):
        out = discover_kr_candidates(
            top_n_output=20, provider=FakeProvider(bars), include_anchors=False
        )
    assert [c.ticker for c in out] == ["DYN002", "DYN003", "DYN001"]


def test_discover_truncates_to_top_n_output():
    """top_n_output truncates the merged list. Anchors fit first."""
    universe_rows = [(f"DYN{i:03d}", 100e12, 10e8) for i in range(10)]
    bars = {
        f"DYN{i:03d}": _bars_with_return(100.0, 100.0 + (i + 1) * 5.0)
        for i in range(10)
    }
    name_map = {f"DYN{i:03d}": f"n{i}" for i in range(10)}
    with _patch_universe(universe_rows), _patch_pykrx_name(name_map):
        out = discover_kr_candidates(
            top_n_output=5, provider=FakeProvider(bars), include_anchors=True
        )
    assert len(out) == 5
    # First 3 are anchors
    assert out[0].ticker == "005930"
    assert out[1].ticker == "000660"
    assert out[2].ticker == "069500"


def test_discover_handles_empty_universe_anchors_only():
    """PyKRX failure path: universe=[], anchors carry the prompt."""
    with _patch_universe([]), _patch_pykrx_name({}):
        out = discover_kr_candidates(top_n_output=20, provider=FakeProvider({}))
    assert len(out) == 3
    assert {c.ticker for c in out} == {t for t, _ in ANCHORS_KR}


# ---------------------------------------------------------------------------
# format_candidates_for_prompt
# ---------------------------------------------------------------------------


def test_format_empty_list_fallback():
    """Empty list yields a header + fallback line — never crashes."""
    out = format_candidates_for_prompt([])
    assert "KR 후보 종목" in out
    assert "스캐너 실패" in out


def test_format_renders_anchor_and_momentum_rows():
    """Korean labels, reason tag, +/- formatting, and 시총 units all render."""
    cands = [
        Candidate(
            ticker="005930",
            name="삼성전자",
            market="KR",
            market_cap=5.0e14,  # 500조
            trading_value=1.0e12,
            return_5d_pct=1.2,
            vol_ratio_5d=1.1,
            reason="anchor",
        ),
        Candidate(
            ticker="307950",
            name="현대오토에버",
            market="KR",
            market_cap=1.45e13,  # 14.5조
            trading_value=5.0e11,
            return_5d_pct=54.7,
            vol_ratio_5d=2.8,
            reason="momentum",
        ),
    ]
    out = format_candidates_for_prompt(cands)
    assert "005930 삼성전자 [anchor]" in out
    assert "307950 현대오토에버 [momentum]" in out
    assert "+54.7%" in out
    assert "2.80x" in out
    # 조 unit rendering for >= 1조
    assert "조" in out


# ---------------------------------------------------------------------------
# Defaults are sensible
# ---------------------------------------------------------------------------


def test_default_thresholds_match_plan():
    """Sanity: defaults match the values quoted in the plan."""
    assert DEFAULT_RETURN_THRESHOLD_PCT == 15.0
    assert DEFAULT_VOL_RATIO_THRESHOLD == 2.0
