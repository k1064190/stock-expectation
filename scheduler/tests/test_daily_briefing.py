"""Tests for daily briefing prediction parsing."""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "mcp-prediction-store"))

import daily_briefing
from daily_briefing import parse_predictions
from models import get_connection


def test_log_predictions_api_mode_recalibrates_and_keeps_raw(monkeypatch):
    """API-mode logging must apply source-scoped recalibration + keep raw_confidence."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    conn = get_connection(path)
    # Seed 40 closed LIVE rows pinned at 0.65 confidence, ~30% hit → the curve
    # pulls 0.65 down toward ~0.30.
    for i in range(40):
        conn.execute(
            """INSERT INTO predictions
               (id, created_at, ticker, market, direction, confidence, timeframe,
                reasoning, entry_price, signals_used, source, status, outcome_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"s{i}",
                "2026-05-01T00:00:00+00:00",
                "T",
                "US",
                "BULL",
                0.65,
                "1W",
                "r",
                100.0,
                "[]",
                "LIVE",
                "HIT" if i % 10 < 3 else "MISS",
                "2026-05-02T00:00:00+00:00",
            ),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        daily_briefing, "get_connection", lambda *a, **k: get_connection(path)
    )

    logged = daily_briefing.log_predictions(
        [
            {
                "ticker": "NVDA",
                "market": "US",
                "direction": "BULL",
                "confidence": 0.65,
                "timeframe": "1W",
                "reasoning": "t",
                "entry_price": 100.0,
                "signals_used": ["technical"],
            }
        ]
    )
    assert logged == 1
    c2 = get_connection(path)
    row = c2.execute(
        "SELECT confidence, raw_confidence FROM predictions WHERE ticker='NVDA'"
    ).fetchone()
    c2.close()
    assert row["raw_confidence"] == 0.65
    assert row["confidence"] < 0.65  # recalibrated downward


def test_log_predictions_api_mode_skips_live_1y(monkeypatch):
    """API-mode logging must skip LIVE 1Y rows (store-gated) and log the rest."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    monkeypatch.setattr(
        daily_briefing, "get_connection", lambda *a, **k: get_connection(path)
    )
    # Components carry both gate fields so _augment_gate_components returns
    # early without fetching fresh bars (keeps the test offline).
    base = {
        "ticker": "NVDA",
        "market": "US",
        "direction": "BULL",
        "confidence": 0.65,
        "reasoning": "t",
        "entry_price": 100.0,
        "signals_used": ["technical"],
        "components": {"overextension": "NONE", "return_1m": 0.05},
    }
    logged = daily_briefing.log_predictions(
        [{**base, "timeframe": "1Y"}, {**base, "timeframe": "6M"}]
    )
    assert logged == 1  # 1Y skipped without error, 6M inserted
    conn = get_connection(path)
    rows = conn.execute("SELECT timeframe FROM predictions").fetchall()
    conn.close()
    assert [r["timeframe"] for r in rows] == ["6M"]


def test_parse_single_prediction():
    """Parse a single prediction from a JSON block."""
    response = """Here is the analysis.

```json
[
  {
    "ticker": "NVDA",
    "market": "US",
    "direction": "BULL",
    "confidence": 0.72,
    "timeframe": "1W",
    "entry_price": 120.50,
    "target_price": 128.00,
    "stop_price": 116.00,
    "reasoning": "Strong breakout with volume confirmation",
    "signals_used": ["technical", "momentum"]
  }
]
```

That's the pick."""

    preds = parse_predictions(response)
    assert len(preds) == 1
    assert preds[0]["ticker"] == "NVDA"
    assert preds[0]["confidence"] == 0.72
    assert preds[0]["signals_used"] == ["technical", "momentum"]


def test_parse_multiple_predictions():
    """Parse multiple predictions from one JSON block."""
    response = """```json
[
  {"ticker": "AAPL", "market": "US", "direction": "BULL", "confidence": 0.65, "timeframe": "1W", "entry_price": 180.0, "reasoning": "test", "signals_used": ["technical"]},
  {"ticker": "005930", "market": "KR", "direction": "BEAR", "confidence": 0.60, "timeframe": "2W", "entry_price": 70000, "reasoning": "test", "signals_used": ["sector"]}
]
```"""

    preds = parse_predictions(response)
    assert len(preds) == 2
    assert preds[0]["ticker"] == "AAPL"
    assert preds[1]["ticker"] == "005930"
    assert preds[1]["market"] == "KR"


def test_parse_multiple_json_blocks():
    """Parse predictions spread across multiple JSON blocks."""
    response = """US picks:
```json
[{"ticker": "MSFT", "market": "US", "direction": "BULL", "confidence": 0.70, "timeframe": "1W", "entry_price": 400.0, "reasoning": "test", "signals_used": ["technical"]}]
```

KR picks:
```json
[{"ticker": "000660", "market": "KR", "direction": "BULL", "confidence": 0.68, "timeframe": "2W", "entry_price": 150000, "reasoning": "test", "signals_used": ["cross_market"]}]
```"""

    preds = parse_predictions(response)
    assert len(preds) == 2


def test_parse_no_json_blocks():
    """Return empty list when no JSON blocks found."""
    response = "No predictions today, market is too uncertain."
    preds = parse_predictions(response)
    assert preds == []


def test_parse_invalid_json():
    """Handle malformed JSON gracefully."""
    response = """```json
[{"ticker": "AAPL", invalid json here}]
```"""
    preds = parse_predictions(response)
    assert preds == []


def test_parse_single_dict():
    """Handle a single dict (not array) in JSON block."""
    response = """```json
{"ticker": "GOOG", "market": "US", "direction": "NEUTRAL", "confidence": 0.55, "timeframe": "1W", "entry_price": 170.0, "reasoning": "range-bound", "signals_used": ["technical"]}
```"""
    preds = parse_predictions(response)
    assert len(preds) == 1
    assert preds[0]["ticker"] == "GOOG"


# ---------------------------------------------------------------------------
# Event-gate (RULE R3) prompt block — WT-D briefing integration
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "mcp-market-data"))
from events import EventGate  # noqa: E402
from daily_briefing import _format_event_gate_for_prompt  # noqa: E402


def test_event_gate_block_unavailable_renders_zero_note():
    gate = EventGate(asof="2026-06-18", market="US", gate_unavailable=True)
    out = _format_event_gate_for_prompt(gate)
    assert "unavailable" in out and "zero" in out


def test_event_gate_block_renders_watch_and_macro():
    gate = EventGate(
        asof="2026-06-18",
        market="US",
        by_ticker={
            "AMD": {
                "cap_label": "WATCH",
                "confidence_trim": 0.0,
                "next_earnings_date": "2026-06-19",
                "trading_days_until": 1,
            },
            "NVDA": {
                "cap_label": None,
                "confidence_trim": 0.0,
                "next_earnings_date": None,
                "trading_days_until": None,
            },
        },
        macro_trim=0.05,
        macro_events=[
            {"name": "FOMC", "event_date": "2026-06-18", "trading_days_until": 1}
        ],
    )
    out = _format_event_gate_for_prompt(gate)
    assert "MACRO trim 0.05" in out
    assert "AMD" in out and "WATCH cap" in out
    assert "NVDA" not in out  # no cap/trim → not listed


def test_event_gate_block_no_risk_note():
    # Both feeds live → the unqualified no-risk line is legitimate.
    gate = EventGate(
        asof="2026-06-18",
        market="US",
        by_ticker={
            "NVDA": {
                "cap_label": None,
                "confidence_trim": 0.0,
                "next_earnings_date": None,
                "trading_days_until": None,
            }
        },
        earnings_source="fmp",
        macro_available=True,
    )
    out = _format_event_gate_for_prompt(gate)
    assert "no imminent earnings/macro risk" in out


def test_event_gate_block_macro_unavailable_no_false_no_risk_claim():
    """Macro feed down + nothing flagged → the prompt must say macro risk is
    UNKNOWN, never 'no imminent earnings/macro risk'."""
    gate = EventGate(
        asof="2026-07-02",
        market="US",
        by_ticker={
            "NVDA": {
                "cap_label": None,
                "confidence_trim": 0.0,
                "next_earnings_date": None,
                "trading_days_until": None,
            }
        },
        earnings_source="fmp",
        macro_available=False,
    )
    out = _format_event_gate_for_prompt(gate)
    assert "no imminent earnings/macro risk" not in out
    assert "macro risk unknown" in out
    assert "no imminent earnings risk" in out


def test_event_gate_block_failed_lookup_excluded_from_no_risk_claim():
    """Partial yfinance lookup failure + no caps + macro OK → the unqualified
    no-risk line must be replaced by a claim that excludes the failed ticker
    (its earnings risk is unknown, not known-zero)."""
    neutral = {
        "cap_label": None,
        "confidence_trim": 0.0,
        "next_earnings_date": None,
        "trading_days_until": None,
    }
    gate = EventGate(
        asof="2026-07-02",
        market="US",
        by_ticker={"NVDA": dict(neutral), "AMD": dict(neutral)},
        notes=["yfinance lookup failed for AMD — earnings risk unknown"],
        earnings_source="yfinance",
        macro_available=True,
    )
    out = _format_event_gate_for_prompt(gate)
    assert "no imminent earnings/macro risk" not in out
    assert "except AMD" in out
    assert "earnings risk unknown" in out
    assert "no imminent macro risk" in out


def test_event_gate_block_renders_partial_availability_notes():
    """A partial outage (e.g. macro calendar down) must be visible in the prompt."""
    gate = EventGate(
        asof="2026-07-02",
        market="US",
        by_ticker={
            "NVDA": {
                "cap_label": None,
                "confidence_trim": 0.0,
                "next_earnings_date": None,
                "trading_days_until": None,
            }
        },
        notes=["macro calendar fetch failed: 402 — macro trim unavailable (fail-open)"],
        earnings_source="yfinance",
        macro_available=False,
    )
    out = _format_event_gate_for_prompt(gate)
    assert "macro calendar fetch failed" in out
    # With macro down, the no-risk line is qualified: earnings-only claim.
    assert "no imminent earnings risk" in out
    assert "macro risk unknown" in out


# ---------------------------------------------------------------------------
# Macro-news block (GDELT/RSS) wiring into the briefing prompt
# ---------------------------------------------------------------------------


def test_macro_block_renders_via_get_macro_news(monkeypatch):
    """_macro_block fetches macro news, scores risk, and formats it for the prompt."""
    monkeypatch.setattr(
        daily_briefing, "get_macro_news", lambda limit=15: (["x"], "rss")
    )
    monkeypatch.setattr(
        daily_briefing,
        "assess_macro_risk",
        lambda items, stale=False: {"risk_level": "NORMAL"},
    )
    monkeypatch.setattr(
        daily_briefing,
        "format_macro_for_prompt",
        lambda items, risk=None: "MACRO_BLOCK_OK",
    )
    assert daily_briefing._macro_block() == "MACRO_BLOCK_OK"


def test_macro_block_empty_on_error(monkeypatch):
    """A macro-news outage degrades to an empty string, never blocking the briefing."""

    def boom(limit=15):
        raise RuntimeError("network down")

    monkeypatch.setattr(daily_briefing, "get_macro_news", boom)
    assert daily_briefing._macro_block() == ""


def test_macro_block_contains_risk_off_line(monkeypatch):
    """A shock headline set puts MACRO RISK: RISK_OFF + no-new-BULL rule in the prompt."""
    from providers.base import NewsItem  # mcp-market-data added to sys.path above

    shock = [
        NewsItem(
            headline="Iran blockades Strait of Hormuz as conflict widens",
            source="BBC",
            date="2026-06-05",
            url="u1",
        ),
        NewsItem(
            headline="US launches airstrike on military sites",
            source="CNBC",
            date="2026-06-05",
            url="u2",
        ),
        NewsItem(
            headline="Stock market crash fears as circuit breaker halts trading",
            source="BBC",
            date="2026-06-05",
            url="u3",
        ),
    ]
    monkeypatch.setattr(
        daily_briefing, "get_macro_news", lambda limit=15: (shock, "rss")
    )
    block = daily_briefing._macro_block()
    assert "MACRO RISK: RISK_OFF" in block
    assert "NO new BULL" in block


def test_macro_block_fail_open_note_when_no_sources(monkeypatch):
    """RSS + GDELT both unreachable → NORMAL with a visible fail-open note."""
    monkeypatch.setattr(daily_briefing, "get_macro_news", lambda limit=15: ([], "none"))
    block = daily_briefing._macro_block()
    assert "MACRO RISK: NORMAL" in block
    assert "fail-open" in block


def test_macro_block_degrades_on_stale_source(monkeypatch):
    """A stale GDELT cache must not hold the gate: NORMAL + visible stale note."""
    from providers.base import NewsItem

    shock = [
        NewsItem(
            headline="Iran blockades Strait of Hormuz as conflict widens",
            source="reuters.com",
            date="2026-06-05",
            url="u1",
        ),
    ]
    monkeypatch.setattr(
        daily_briefing, "get_macro_news", lambda limit=15: (shock, "gdelt-stale")
    )
    block, risk_level = daily_briefing._macro_block_and_risk()
    assert risk_level == "NORMAL"
    assert "MACRO RISK: NORMAL" in block
    assert "stale" in block


def test_log_predictions_skips_bull_on_risk_off(monkeypatch):
    """API mode: RISK_OFF deterministically skips LIVE BULL inserts even if the
    model ignored the prompt instruction; non-BULL rows still log."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = Path(f.name)
    monkeypatch.setattr(
        daily_briefing, "get_connection", lambda *a, **k: get_connection(path)
    )

    base = {
        "market": "US",
        "confidence": 0.65,
        "timeframe": "1W",
        "reasoning": "t",
        "entry_price": 100.0,
        "signals_used": ["technical"],
        # Components present → no gate-component augmentation fetch in test.
        "components": {"overextension": "NONE", "return_1m": 0.05},
    }
    logged = daily_briefing.log_predictions(
        [
            {**base, "ticker": "NVDA", "direction": "BULL"},
            {**base, "ticker": "SPY", "direction": "NEUTRAL"},
        ],
        macro_risk_level="RISK_OFF",
    )
    assert logged == 1  # BULL skipped, NEUTRAL logged
    conn = get_connection(path)
    rows = conn.execute("SELECT ticker, direction FROM predictions").fetchall()
    conn.close()
    assert [(r["ticker"], r["direction"]) for r in rows] == [("SPY", "NEUTRAL")]


def test_build_api_prompt_returns_prompt_and_risk_level(monkeypatch):
    """API mode surfaces the assessed macro risk level alongside the prompt so
    run_briefing can thread it into log_predictions (the deterministic
    RISK_OFF BULL skip); the macro block itself lands in the prompt body."""
    monkeypatch.setattr(daily_briefing, "fetch_us_market_data", lambda: "MARKET_DATA")
    monkeypatch.setattr(daily_briefing, "get_track_record_context", lambda: "TRACK")
    monkeypatch.setattr(
        daily_briefing, "_macro_block_and_risk", lambda: ("MACRO_BLOCK", "RISK_OFF")
    )
    prompt, risk_level = daily_briefing.build_api_prompt("US")
    assert risk_level == "RISK_OFF"
    assert "MACRO_BLOCK" in prompt
    assert "MARKET_DATA" in prompt


def _real_get_connection(path):
    import models as pred_models

    return pred_models.get_connection(path)


def test_log_predictions_tags_low_edge_band_api_mode(tmp_path, monkeypatch):
    """API-mode LIVE BULL in [0.60, 0.70] must carry low_edge_band (PR #62 P2)."""
    import daily_briefing as db_mod

    db_file = tmp_path / "p.db"
    monkeypatch.setattr(db_mod, "get_connection", lambda: _real_get_connection(db_file))

    class _NoNet:
        def get_price_history(self, *a, **k):
            return []

        def get_news(self, *a, **k):
            return []

    monkeypatch.setattr(db_mod, "USMarketProvider", _NoNet)
    monkeypatch.setattr(db_mod, "KoreanMarketProvider", _NoNet)

    n = db_mod.log_predictions(
        [
            {
                "ticker": "ACME",
                "market": "US",
                "direction": "BULL",
                "confidence": 0.62,
                "timeframe": "1W",
                "reasoning": "r",
                "entry_price": 10.0,
                "components": {"algo": 5.0, "overextension": "NONE", "return_1m": 0.02},
            }
        ]
    )
    assert n == 1
    conn = _real_get_connection(db_file)
    row = conn.execute("SELECT components FROM predictions").fetchone()
    conn.close()
    import json as _json

    comps = _json.loads(row["components"])
    assert comps["low_edge_band"] is True
