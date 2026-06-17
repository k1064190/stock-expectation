"""Tests for the catalyst event timeline + R3 event-risk gate.

All fetchers are STUBBED — no test in this file touches the live FMP API.
Covers:
  - trading_days_between boundaries (same-day, Fri->Mon, +7 calendar days);
  - evaluate_gate R3 thresholds (earnings WATCH / trim / none, macro trim);
  - timeline merge + grouping;
  - FAIL-OPEN (missing key, simulated fetch exception).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from events import (  # noqa: E402
    EARNINGS_CONFIDENCE_TRIM,
    MACRO_CONFIDENCE_TRIM,
    build_timeline,
    evaluate_gate,
    trading_days_between,
)


# ---------------------------------------------------------------------------
# trading_days_between
# ---------------------------------------------------------------------------


def test_trading_days_same_day_is_zero():
    assert trading_days_between("2026-06-17", "2026-06-17") == 0


def test_trading_days_past_event_is_zero():
    # Event before as-of carries no forward risk.
    assert trading_days_between("2026-06-17", "2026-06-10") == 0


def test_trading_days_friday_to_monday_is_one():
    # 2026-06-19 is a Friday; 2026-06-22 is the next Monday → 1 business day.
    assert trading_days_between("2026-06-19", "2026-06-22") == 1


def test_trading_days_seven_calendar_days_is_five():
    # Wed 2026-06-17 → Wed 2026-06-24 spans one weekend → 5 business days.
    assert trading_days_between("2026-06-17", "2026-06-24") == 5


def test_trading_days_next_calendar_day_weekday_is_one():
    # Wed → Thu, no weekend → 1 business day.
    assert trading_days_between("2026-06-17", "2026-06-18") == 1


# ---------------------------------------------------------------------------
# build_timeline merge + grouping
# ---------------------------------------------------------------------------


def test_build_timeline_groups_earnings_by_ticker_and_filters_macro():
    asof = "2026-06-17"
    earnings_rows = [
        {
            "symbol": "NVDA",
            "date": "2026-06-19",
            "time": "amc",
            "companyName": "NVIDIA",
        },
        {"symbol": "AMD", "date": "2026-06-24", "time": "bmo", "companyName": "AMD"},
        # past-dated row → dropped
        {
            "symbol": "NVDA",
            "date": "2026-06-10",
            "time": "amc",
            "companyName": "NVIDIA",
        },
    ]
    macro_rows = [
        {
            "date": "2026-06-18",
            "event": "FOMC Interest Rate Decision",
            "impact": "High",
            "country": "US",
        },
        # Low-impact → dropped
        {"date": "2026-06-18", "event": "CPI", "impact": "Low", "country": "US"},
        # Not a macro keyword → dropped
        {
            "date": "2026-06-18",
            "event": "Crude Oil Inventories",
            "impact": "High",
            "country": "US",
        },
    ]

    tl = build_timeline(asof, earnings_rows, macro_rows, "US")

    assert set(tl["by_ticker"].keys()) == {"NVDA", "AMD"}
    # NVDA's past-dated row was filtered → exactly one forward event.
    assert len(tl["by_ticker"]["NVDA"]) == 1
    assert tl["by_ticker"]["NVDA"][0].timing == "AMC"
    assert tl["by_ticker"]["NVDA"][0].market == "US"
    # Only the High-impact FOMC release survives the macro filter.
    assert len(tl["market_wide"]) == 1
    assert tl["market_wide"][0].kind == "macro"
    assert tl["market_wide"][0].market == "GLOBAL"


def test_build_timeline_macro_sorted_nearest_first():
    asof = "2026-06-17"
    macro_rows = [
        {
            "date": "2026-06-24",
            "event": "Nonfarm Payrolls",
            "impact": "High",
            "country": "US",
        },
        {"date": "2026-06-18", "event": "CPI", "impact": "High", "country": "US"},
    ]
    tl = build_timeline(asof, [], macro_rows, "US")
    dates = [e.event_date for e in tl["market_wide"]]
    assert dates == ["2026-06-18", "2026-06-24"]


# ---------------------------------------------------------------------------
# evaluate_gate — earnings thresholds (US)
# ---------------------------------------------------------------------------


def _stub_earnings(rows):
    """Return a fetch_earnings stub that ignores args and yields ``rows``."""
    return lambda _from, _to, _market: rows


def _stub_macro(rows):
    """Return a fetch_macro stub that ignores args and yields ``rows``."""
    return lambda _from, _to: rows


def test_evaluate_gate_earnings_one_td_caps_watch(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "fake-key")
    asof = "2026-06-17"  # Wed; 2026-06-18 (Thu) is 1 td away
    gate = evaluate_gate(
        asof,
        ["NVDA"],
        "US",
        fetch_earnings=_stub_earnings(
            [{"symbol": "NVDA", "date": "2026-06-18", "time": "amc"}]
        ),
        fetch_macro=_stub_macro([]),
    )
    v = gate.by_ticker["NVDA"]
    assert v["cap_label"] == "WATCH"
    assert v["confidence_trim"] == 0.0
    assert v["trading_days_until"] == 1
    assert gate.gate_unavailable is False


def test_evaluate_gate_earnings_same_day_caps_watch(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "fake-key")
    asof = "2026-06-17"  # earnings report TODAY → td == 0
    gate = evaluate_gate(
        asof,
        ["NVDA"],
        "US",
        fetch_earnings=_stub_earnings(
            [{"symbol": "NVDA", "date": "2026-06-17", "time": "amc"}]
        ),
        fetch_macro=_stub_macro([]),
    )
    v = gate.by_ticker["NVDA"]
    # A same-day report (td == 0) must NOT be dropped — it caps at WATCH.
    assert v["cap_label"] == "WATCH"
    assert v["trading_days_until"] == 0
    assert v["next_earnings_date"] == "2026-06-17"


def test_evaluate_gate_earnings_four_td_trims(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "fake-key")
    asof = "2026-06-17"  # Wed; 2026-06-23 (Tue) is 4 td away (skips weekend)
    gate = evaluate_gate(
        asof,
        ["NVDA"],
        "US",
        fetch_earnings=_stub_earnings(
            [{"symbol": "NVDA", "date": "2026-06-23", "time": "bmo"}]
        ),
        fetch_macro=_stub_macro([]),
    )
    v = gate.by_ticker["NVDA"]
    assert v["cap_label"] is None
    assert v["confidence_trim"] == EARNINGS_CONFIDENCE_TRIM
    assert v["trading_days_until"] == 4


def test_evaluate_gate_earnings_eight_td_none(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "fake-key")
    asof = "2026-06-17"  # Wed; 2026-06-29 (Mon) is 8 td away
    gate = evaluate_gate(
        asof,
        ["NVDA"],
        "US",
        fetch_earnings=_stub_earnings(
            [{"symbol": "NVDA", "date": "2026-06-29", "time": "amc"}]
        ),
        fetch_macro=_stub_macro([]),
    )
    v = gate.by_ticker["NVDA"]
    assert v["cap_label"] is None
    assert v["confidence_trim"] == 0.0
    assert v["trading_days_until"] == 8


def test_evaluate_gate_no_earnings_is_neutral(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "fake-key")
    gate = evaluate_gate(
        "2026-06-17",
        ["NVDA"],
        "US",
        fetch_earnings=_stub_earnings([]),
        fetch_macro=_stub_macro([]),
    )
    v = gate.by_ticker["NVDA"]
    assert v["cap_label"] is None
    assert v["confidence_trim"] == 0.0
    assert v["next_earnings_date"] is None


# ---------------------------------------------------------------------------
# evaluate_gate — macro trim (US + KR)
# ---------------------------------------------------------------------------


def test_evaluate_gate_fomc_one_td_macro_trim(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "fake-key")
    gate = evaluate_gate(
        "2026-06-17",
        ["NVDA", "AMD"],
        "US",
        fetch_earnings=_stub_earnings([]),
        fetch_macro=_stub_macro(
            [
                {
                    "date": "2026-06-18",
                    "event": "FOMC Interest Rate Decision",
                    "impact": "High",
                    "country": "US",
                }
            ]
        ),
    )
    assert gate.macro_trim == MACRO_CONFIDENCE_TRIM
    assert len(gate.macro_events) == 1
    assert gate.macro_events[0]["trading_days_until"] == 1


def test_evaluate_gate_fomc_same_day_macro_trim(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "fake-key")
    # High-impact FOMC TODAY (td == 0) must still drive macro_trim.
    gate = evaluate_gate(
        "2026-06-17",
        ["NVDA"],
        "US",
        fetch_earnings=_stub_earnings([]),
        fetch_macro=_stub_macro(
            [
                {
                    "date": "2026-06-17",
                    "event": "FOMC Interest Rate Decision",
                    "impact": "High",
                    "country": "US",
                }
            ]
        ),
    )
    assert gate.macro_trim == MACRO_CONFIDENCE_TRIM
    assert len(gate.macro_events) == 1
    assert gate.macro_events[0]["trading_days_until"] == 0


def test_evaluate_gate_non_us_macro_ignored(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "fake-key")
    # A High-impact, imminent CPI release from a non-US country must NOT trigger
    # macro_trim — the R3 macro stream is US-only by design.
    gate = evaluate_gate(
        "2026-06-17",
        ["NVDA"],
        "US",
        fetch_earnings=_stub_earnings([]),
        fetch_macro=_stub_macro(
            [
                {
                    "date": "2026-06-18",
                    "event": "Consumer Price Index",
                    "impact": "High",
                    "country": "GB",
                }
            ]
        ),
    )
    assert gate.macro_trim == 0.0
    assert gate.macro_events == []


def test_evaluate_gate_distant_macro_no_trim(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "fake-key")
    # CPI 5 td away → outside MACRO_TRIM_DAYS (2) → no macro trim.
    gate = evaluate_gate(
        "2026-06-17",
        ["NVDA"],
        "US",
        fetch_earnings=_stub_earnings([]),
        fetch_macro=_stub_macro(
            [
                {
                    "date": "2026-06-24",
                    "event": "CPI",
                    "impact": "High",
                    "country": "US",
                }
            ]
        ),
    )
    assert gate.macro_trim == 0.0
    assert gate.macro_events == []


# ---------------------------------------------------------------------------
# evaluate_gate — KR coverage (macro-only, no per-ticker earnings cap)
# ---------------------------------------------------------------------------


def test_evaluate_gate_kr_macro_only_no_earnings_cap(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "fake-key")
    earnings_called = {"hit": False}

    def _earnings_should_not_fire(_from, _to, _market):
        earnings_called["hit"] = True
        return [{"symbol": "005930", "date": "2026-06-18", "time": "bmo"}]

    gate = evaluate_gate(
        "2026-06-17",
        ["005930"],
        "KR",
        fetch_earnings=_earnings_should_not_fire,
        fetch_macro=_stub_macro(
            [
                {
                    "date": "2026-06-18",
                    "event": "Fed Interest Rate Decision",
                    "impact": "High",
                    "country": "US",
                }
            ]
        ),
    )
    # KR consumes the US macro stream → macro_trim applies.
    assert gate.macro_trim == MACRO_CONFIDENCE_TRIM
    # But KR has no forward EPS feed → no per-ticker earnings cap, and the
    # earnings fetcher is never even called (quota protection).
    assert gate.by_ticker["005930"]["cap_label"] is None
    assert gate.by_ticker["005930"]["next_earnings_date"] is None
    assert earnings_called["hit"] is False


# ---------------------------------------------------------------------------
# evaluate_gate — quota protection (single fetch per call)
# ---------------------------------------------------------------------------


def test_evaluate_gate_fetches_once_for_many_tickers(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "fake-key")
    calls = {"earnings": 0, "macro": 0}

    def _earnings(_from, _to, _market):
        calls["earnings"] += 1
        return [{"symbol": "NVDA", "date": "2026-06-18", "time": "amc"}]

    def _macro(_from, _to):
        calls["macro"] += 1
        return []

    evaluate_gate(
        "2026-06-17",
        ["NVDA", "AMD", "AVGO", "MSFT", "AAPL"],
        "US",
        fetch_earnings=_earnings,
        fetch_macro=_macro,
    )
    assert calls == {"earnings": 1, "macro": 1}


# ---------------------------------------------------------------------------
# FAIL-OPEN
# ---------------------------------------------------------------------------


def test_evaluate_gate_missing_key_fails_open(monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    # No injected fetchers → must short-circuit to a neutral gate.
    gate = evaluate_gate("2026-06-17", ["NVDA", "AMD"], "US")
    assert gate.gate_unavailable is True
    assert gate.macro_trim == 0.0
    for t in ("NVDA", "AMD"):
        assert gate.by_ticker[t]["cap_label"] is None
        assert gate.by_ticker[t]["confidence_trim"] == 0.0
    assert any("FMP_API_KEY" in n for n in gate.notes)


def test_evaluate_gate_fetch_exception_fails_open(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "fake-key")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated FMP outage")

    gate = evaluate_gate(
        "2026-06-17",
        ["NVDA"],
        "US",
        fetch_earnings=_boom,
        fetch_macro=_boom,
    )
    assert gate.gate_unavailable is True
    assert gate.macro_trim == 0.0
    assert gate.by_ticker["NVDA"]["cap_label"] is None
    assert any("fetch failed" in n for n in gate.notes)
