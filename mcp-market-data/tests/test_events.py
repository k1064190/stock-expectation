"""Tests for the catalyst event timeline + R3 event-risk gate.

All fetchers are STUBBED — no test in this file touches the live FMP API or
yfinance. Covers:
  - trading_days_between boundaries (same-day, Fri->Mon, +7 calendar days);
  - evaluate_gate R3 thresholds (earnings WATCH / trim / none, macro trim);
  - timeline merge + grouping;
  - yfinance earnings fallback (FMP 403 → per-ticker caps still applied);
  - partial availability (macro down / earnings down — visible, not silent);
  - FAIL-OPEN (missing key, simulated fetch exception on every source).
"""

import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from events import (  # noqa: E402
    EARNINGS_CONFIDENCE_TRIM,
    MACRO_CONFIDENCE_TRIM,
    _fetch_earnings_fallback_yf,
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
    # Happy path: availability fields report both sources live.
    assert gate.earnings_source == "fmp"
    assert gate.macro_available is True


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
# yfinance earnings fallback + partial availability (visible fail-open)
# ---------------------------------------------------------------------------


def test_evaluate_gate_fmp_403_yfinance_fallback_caps(monkeypatch):
    """FMP earnings 403 → the yfinance fallback still produces the WATCH cap."""
    monkeypatch.setenv("FMP_API_KEY", "fake-key")

    def _fmp_403(*_args, **_kwargs):
        raise RuntimeError("403 Forbidden")

    def _yf_rows(tickers, asof):
        assert tickers == ["NVDA", "AMD"]
        assert asof == "2026-06-17"
        return [
            {
                "symbol": "NVDA",
                "date": "2026-06-18",
                "time": None,
                "source": "yfinance:calendar",
            }
        ]

    gate = evaluate_gate(
        "2026-06-17",  # Wed; 2026-06-18 (Thu) is 1 td away → WATCH
        ["NVDA", "AMD"],
        "US",
        fetch_earnings=_fmp_403,
        fetch_macro=_stub_macro([]),
        fetch_earnings_fallback=_yf_rows,
    )
    assert gate.gate_unavailable is False
    assert gate.earnings_source == "yfinance"
    assert gate.by_ticker["NVDA"]["cap_label"] == "WATCH"
    assert gate.by_ticker["NVDA"]["trading_days_until"] == 1
    assert gate.by_ticker["AMD"]["cap_label"] is None
    assert any("FMP earnings calendar fetch failed" in n for n in gate.notes)


def test_evaluate_gate_missing_key_uses_yfinance_fallback(monkeypatch):
    """No FMP key no longer kills the earnings side — yfinance covers it."""
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    gate = evaluate_gate(
        "2026-06-17",
        ["NVDA"],
        "US",
        fetch_earnings_fallback=lambda _tickers, _asof: [
            {"symbol": "NVDA", "date": "2026-06-18", "time": None}
        ],
    )
    assert gate.gate_unavailable is False
    assert gate.earnings_source == "yfinance"
    assert gate.by_ticker["NVDA"]["cap_label"] == "WATCH"
    # Macro still needs the FMP key → unavailable, but VISIBLY so.
    assert gate.macro_available is False
    assert any("FMP_API_KEY" in n for n in gate.notes)


def test_evaluate_gate_macro_down_is_visible_not_silent(monkeypatch):
    """Earnings OK + macro down (402) → gate stays live, macro flagged."""
    monkeypatch.setenv("FMP_API_KEY", "fake-key")

    def _macro_402(*_args, **_kwargs):
        raise RuntimeError("402 Payment Required")

    gate = evaluate_gate(
        "2026-06-17",
        ["NVDA"],
        "US",
        fetch_earnings=_stub_earnings(
            [{"symbol": "NVDA", "date": "2026-06-18", "time": "amc"}]
        ),
        fetch_macro=_macro_402,
    )
    assert gate.gate_unavailable is False
    assert gate.earnings_source == "fmp"
    assert gate.macro_available is False
    assert gate.macro_trim == 0.0
    assert gate.by_ticker["NVDA"]["cap_label"] == "WATCH"
    assert any("macro calendar fetch failed" in n for n in gate.notes)


def test_evaluate_gate_notes_redact_api_key(monkeypatch):
    """httpx errors embed the request URL — the apikey must never reach notes."""
    monkeypatch.setenv("FMP_API_KEY", "fake-key")

    def _leaky_boom(*_args, **_kwargs):
        raise RuntimeError(
            "Client error '402 Payment Required' for url "
            "'https://financialmodelingprep.com/stable/economic-calendar"
            "?from=2026-07-02&to=2026-07-11&apikey=SUPERSECRET'"
        )

    gate = evaluate_gate(
        "2026-06-17",
        ["NVDA"],
        "US",
        fetch_earnings=_stub_earnings([]),
        fetch_macro=_leaky_boom,
    )
    joined = " ".join(gate.notes)
    assert "SUPERSECRET" not in joined
    assert "apikey=***" in joined


def test_evaluate_gate_earnings_both_down_macro_up_partial(monkeypatch):
    """Both earnings sources down + macro up → live gate, earnings flagged."""
    monkeypatch.setenv("FMP_API_KEY", "fake-key")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated outage")

    gate = evaluate_gate(
        "2026-06-17",
        ["NVDA"],
        "US",
        fetch_earnings=_boom,
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
        fetch_earnings_fallback=_boom,
    )
    assert gate.gate_unavailable is False
    assert gate.earnings_source is None
    assert gate.macro_available is True
    assert gate.macro_trim == MACRO_CONFIDENCE_TRIM
    assert gate.by_ticker["NVDA"]["cap_label"] is None
    assert any("yfinance earnings fallback failed" in n for n in gate.notes)


# ---------------------------------------------------------------------------
# _fetch_earnings_fallback_yf (yfinance mocked — no network)
# ---------------------------------------------------------------------------


def test_yf_fallback_builds_fmp_shaped_rows(monkeypatch):
    calendars = {
        "NVDA": {"Earnings Date": [date(2026, 8, 27), date(2026, 8, 31)]},
        "AMD": {},  # no scheduled earnings → omitted, not an error
        "MSFT": {"Earnings Date": [datetime(2026, 7, 29, 21, 0)]},  # datetime OK
    }

    class _FakeTicker:
        def __init__(self, symbol):
            self.calendar = calendars[symbol]

    monkeypatch.setattr("yfinance.Ticker", _FakeTicker)
    rows = _fetch_earnings_fallback_yf(["NVDA", "AMD", "MSFT"], "2026-07-02")
    assert rows == [
        {
            "symbol": "NVDA",
            "date": "2026-08-27",  # nearest of the two candidate dates
            "time": None,
            "source": "yfinance:calendar",
        },
        {
            "symbol": "MSFT",
            "date": "2026-07-29",
            "time": None,
            "source": "yfinance:calendar",
        },
    ]


def test_yf_fallback_partial_errors_keep_survivors(monkeypatch):
    class _MixedTicker:
        def __init__(self, symbol):
            if symbol == "AMD":
                raise RuntimeError("boom")
            self.calendar = {"Earnings Date": [date(2026, 7, 30)]}

    monkeypatch.setattr("yfinance.Ticker", _MixedTicker)
    rows = _fetch_earnings_fallback_yf(["NVDA", "AMD"], "2026-07-02")
    assert [r["symbol"] for r in rows] == ["NVDA"]


def test_yf_fallback_total_outage_raises(monkeypatch):
    class _BoomTicker:
        def __init__(self, _symbol):
            raise RuntimeError("network down")

    monkeypatch.setattr("yfinance.Ticker", _BoomTicker)
    with pytest.raises(RuntimeError, match="all 2 tickers"):
        _fetch_earnings_fallback_yf(["NVDA", "AMD"], "2026-07-02")


def test_yf_fallback_skips_past_dates(monkeypatch):
    """A stale past date must not shadow the real future date (min() trap),
    a ticker with ONLY past dates yields no row — not a bogus td-0 cap —
    and a same-day date is KEPT (td == 0 still carries binary risk)."""
    calendars = {
        # past + future → the future date must win, not min() = the past one
        "NVDA": {"Earnings Date": [date(2026, 5, 28), date(2026, 8, 27)]},
        # only past dates → treated as "no scheduled earnings", row omitted
        "AMD": {"Earnings Date": [date(2026, 4, 30)]},
        # same-day (== asof) → kept, matching the FMP-side td-0 WATCH behavior
        "MSFT": {"Earnings Date": [date(2026, 7, 2)]},
    }

    class _FakeTicker:
        def __init__(self, symbol):
            self.calendar = calendars[symbol]

    monkeypatch.setattr("yfinance.Ticker", _FakeTicker)
    rows = _fetch_earnings_fallback_yf(["NVDA", "AMD", "MSFT"], "2026-07-02")
    assert rows == [
        {
            "symbol": "NVDA",
            "date": "2026-08-27",
            "time": None,
            "source": "yfinance:calendar",
        },
        {
            "symbol": "MSFT",
            "date": "2026-07-02",
            "time": None,
            "source": "yfinance:calendar",
        },
    ]


def test_evaluate_gate_yf_past_only_dates_no_cap(monkeypatch):
    """FMP down + yfinance reporting only an already-past earnings date →
    neutral verdict for that ticker (no misapplied WATCH cap)."""
    monkeypatch.setenv("FMP_API_KEY", "fake-key")

    class _PastTicker:
        def __init__(self, _symbol):
            self.calendar = {"Earnings Date": [date(2026, 6, 10)]}

    monkeypatch.setattr("yfinance.Ticker", _PastTicker)

    def _fmp_403(*_args, **_kwargs):
        raise RuntimeError("403 Forbidden")

    gate = evaluate_gate(
        "2026-06-17",
        ["NVDA"],
        "US",
        fetch_earnings=_fmp_403,
        fetch_macro=_stub_macro([]),
        # no injected fallback → exercises the real yfinance path (mocked)
    )
    assert gate.earnings_source == "yfinance"
    assert gate.by_ticker["NVDA"]["cap_label"] is None
    assert gate.by_ticker["NVDA"]["confidence_trim"] == 0.0
    assert gate.by_ticker["NVDA"]["next_earnings_date"] is None


# ---------------------------------------------------------------------------
# FAIL-OPEN
# ---------------------------------------------------------------------------


def test_evaluate_gate_missing_key_fails_open(monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)

    def _yf_boom(_tickers, _asof):
        raise RuntimeError("simulated yfinance outage")

    # No key (FMP skipped) AND the yfinance fallback down → neutral gate,
    # clearly flagged. The fallback is injected so no network is touched.
    gate = evaluate_gate(
        "2026-06-17", ["NVDA", "AMD"], "US", fetch_earnings_fallback=_yf_boom
    )
    assert gate.gate_unavailable is True
    assert gate.macro_trim == 0.0
    assert gate.earnings_source is None
    assert gate.macro_available is False
    for t in ("NVDA", "AMD"):
        assert gate.by_ticker[t]["cap_label"] is None
        assert gate.by_ticker[t]["confidence_trim"] == 0.0
    assert any("FMP_API_KEY" in n for n in gate.notes)


def test_evaluate_gate_fetch_exception_fails_open(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "fake-key")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated FMP outage")

    # Every source down (FMP earnings + yfinance fallback + macro) → neutral
    # gate with explanatory notes for each failed side.
    gate = evaluate_gate(
        "2026-06-17",
        ["NVDA"],
        "US",
        fetch_earnings=_boom,
        fetch_macro=_boom,
        fetch_earnings_fallback=_boom,
    )
    assert gate.gate_unavailable is True
    assert gate.macro_trim == 0.0
    assert gate.by_ticker["NVDA"]["cap_label"] is None
    assert any("fetch failed" in n for n in gate.notes)
    assert any("yfinance earnings fallback failed" in n for n in gate.notes)
    assert any("macro calendar fetch failed" in n for n in gate.notes)
