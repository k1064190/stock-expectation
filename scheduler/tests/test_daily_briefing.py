"""Tests for daily briefing prediction parsing."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from daily_briefing import parse_predictions


def test_parse_single_prediction():
    """Parse a single prediction from a JSON block."""
    response = '''Here is the analysis.

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

That's the pick.'''

    preds = parse_predictions(response)
    assert len(preds) == 1
    assert preds[0]["ticker"] == "NVDA"
    assert preds[0]["confidence"] == 0.72
    assert preds[0]["signals_used"] == ["technical", "momentum"]


def test_parse_multiple_predictions():
    """Parse multiple predictions from one JSON block."""
    response = '''```json
[
  {"ticker": "AAPL", "market": "US", "direction": "BULL", "confidence": 0.65, "timeframe": "1W", "entry_price": 180.0, "reasoning": "test", "signals_used": ["technical"]},
  {"ticker": "005930", "market": "KR", "direction": "BEAR", "confidence": 0.60, "timeframe": "2W", "entry_price": 70000, "reasoning": "test", "signals_used": ["sector"]}
]
```'''

    preds = parse_predictions(response)
    assert len(preds) == 2
    assert preds[0]["ticker"] == "AAPL"
    assert preds[1]["ticker"] == "005930"
    assert preds[1]["market"] == "KR"


def test_parse_multiple_json_blocks():
    """Parse predictions spread across multiple JSON blocks."""
    response = '''US picks:
```json
[{"ticker": "MSFT", "market": "US", "direction": "BULL", "confidence": 0.70, "timeframe": "1W", "entry_price": 400.0, "reasoning": "test", "signals_used": ["technical"]}]
```

KR picks:
```json
[{"ticker": "000660", "market": "KR", "direction": "BULL", "confidence": 0.68, "timeframe": "2W", "entry_price": 150000, "reasoning": "test", "signals_used": ["cross_market"]}]
```'''

    preds = parse_predictions(response)
    assert len(preds) == 2


def test_parse_no_json_blocks():
    """Return empty list when no JSON blocks found."""
    response = "No predictions today, market is too uncertain."
    preds = parse_predictions(response)
    assert preds == []


def test_parse_invalid_json():
    """Handle malformed JSON gracefully."""
    response = '''```json
[{"ticker": "AAPL", invalid json here}]
```'''
    preds = parse_predictions(response)
    assert preds == []


def test_parse_single_dict():
    """Handle a single dict (not array) in JSON block."""
    response = '''```json
{"ticker": "GOOG", "market": "US", "direction": "NEUTRAL", "confidence": 0.55, "timeframe": "1W", "entry_price": 170.0, "reasoning": "range-bound", "signals_used": ["technical"]}
```'''
    preds = parse_predictions(response)
    assert len(preds) == 1
    assert preds[0]["ticker"] == "GOOG"
