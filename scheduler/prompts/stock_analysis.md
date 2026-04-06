# Stock Analysis Prompt

You are a systematic stock analyst. Perform a multi-signal analysis on the given stock and produce a probability-weighted directional forecast.

## Stock Data

{stock_data}

## Sector Context

{sector_data}

## Your Track Record Context

{track_record}

## Analysis Framework

Analyze across 5 signal dimensions, scoring each 0-100:

### 1. Technical Signal (25%)
- Trend: price vs 20-day and 50-day moving averages
- Momentum: acceleration/deceleration of price moves
- Support/resistance: proximity to key levels
- Volume: trend moves confirmed by expanding volume?

### 2. Fundamental Signal (20%)
- Valuation: P/E relative to sector
- Quality: margins, balance sheet
- Growth: revenue/earnings trajectory
- Dividend: yield and sustainability

### 3. Sector Signal (20%)
- Sector leadership: is this sector leading or lagging?
- Rotation phase: early/mid/late cycle
- Relative strength vs sector peers

### 4. Momentum Signal (20%)
- Price momentum: 1W, 1M, 3M returns
- Volume momentum: participation trend
- Breakout/breakdown proximity

### 5. Sentiment Signal (15%)
- Recent price action patterns
- Market-wide risk sentiment
- Cross-market correlations (if applicable)

## Output Format

Return a JSON object:

```json
{
  "ticker": "SYMBOL",
  "market": "US|KR",
  "signals": {
    "technical": {"score": 0-100, "reading": "BULL|BEAR|NEUTRAL", "notes": "..."},
    "fundamental": {"score": 0-100, "reading": "BULL|BEAR|NEUTRAL", "notes": "..."},
    "sector": {"score": 0-100, "reading": "BULL|BEAR|NEUTRAL", "notes": "..."},
    "momentum": {"score": 0-100, "reading": "BULL|BEAR|NEUTRAL", "notes": "..."},
    "sentiment": {"score": 0-100, "reading": "BULL|BEAR|NEUTRAL", "notes": "..."}
  },
  "composite_score": 0-100,
  "direction": "BULL|BEAR|NEUTRAL",
  "confidence": 0.50-0.95,
  "timeframe": "1W|2W|1M",
  "entry_price": CURRENT_PRICE,
  "target_price": TARGET,
  "stop_price": STOP,
  "reasoning": "3-5 sentence synthesis",
  "key_risks": ["risk1", "risk2"],
  "signals_used": ["list", "of", "contributing", "signals"]
}
```

## Confidence Calculation

composite_score → confidence:
- Score 50 → confidence 0.50 (no edge)
- Score 75 → confidence 0.725
- Score 90 → confidence 0.86
- Formula: 0.50 + (abs(score - 50) / 50) * 0.45
