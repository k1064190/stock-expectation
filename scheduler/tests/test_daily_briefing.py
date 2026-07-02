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
    )
    out = _format_event_gate_for_prompt(gate)
    assert "no imminent earnings/macro risk" in out


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
    assert "no imminent earnings/macro risk" in out


# ---------------------------------------------------------------------------
# Macro-news block (GDELT/RSS) wiring into the briefing prompt
# ---------------------------------------------------------------------------


def test_macro_block_renders_via_get_macro_news(monkeypatch):
    """_macro_block fetches macro news and formats it for the prompt."""
    monkeypatch.setattr(
        daily_briefing, "get_macro_news", lambda limit=15: (["x"], "rss")
    )
    monkeypatch.setattr(
        daily_briefing, "format_macro_for_prompt", lambda items: "MACRO_BLOCK_OK"
    )
    assert daily_briefing._macro_block() == "MACRO_BLOCK_OK"


def test_macro_block_empty_on_error(monkeypatch):
    """A macro-news outage degrades to an empty string, never blocking the briefing."""

    def boom(limit=15):
        raise RuntimeError("network down")

    monkeypatch.setattr(daily_briefing, "get_macro_news", boom)
    assert daily_briefing._macro_block() == ""
