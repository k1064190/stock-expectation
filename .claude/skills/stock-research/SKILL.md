---
name: stock-research
description: Interactive deep-dive analysis on any US or Korean stock. Performs multi-signal analysis covering technicals, fundamentals, sector context, and momentum. Generates a probability-weighted forecast with option to log as a formal prediction. Triggers on keywords like analyze [ticker], research [ticker], what about [ticker], [ticker] 분석, 종목 분석, should I buy, tell me about.
---

# Stock Research

Perform a comprehensive multi-signal analysis on a single stock, covering fundamentals, technicals, sector context, and momentum. Generate a probability-weighted directional forecast.

## When to Use

- When user asks about a specific stock ("What about NVDA?", "삼성전자 어때?")
- When user wants a deep-dive before making a trading decision
- When user wants to compare a stock's signals before creating a prediction

## Prerequisites

- `bin/stock-cli` must be executable (uses uv-managed environment)
- No API keys required for Korean data
- Optional: `FMP_API_KEY` for enhanced US fundamentals

## Workflow

### 1. Identify Stock and Market

Parse the user's request to determine:
- **Ticker**: Extract from user input (e.g., "NVDA", "005930", "삼성전자")
- **Market**: Determine US or KR. If a Korean company name is given,
  use `search` to find the ticker:

```bash
bin/stock-cli search "삼성전자" --market KR
```

### 2. Gather Data

**Price history (90 days of context):**
```bash
bin/stock-cli price NVDA --market US --days 90
```

From the JSON output, calculate:
- Current price and short-term trend (5-day, 20-day direction)
- 52-week high/low proximity (when enough data)
- Volume trend (above/below recent average)
- Approximate moving averages from close prices (10, 20, 50 day)

**Fundamentals:**
```bash
bin/stock-cli fundamentals NVDA --market US
```

**Context — existing predictions for this ticker:**
```bash
bin/stock-cli predict list --ticker NVDA --limit 5
```

**Context — broader market for sector signal:**
```bash
bin/stock-cli price SPY --market US --days 30
# For a semi stock, also fetch semi ETF
bin/stock-cli price SMH --market US --days 30
```

### 3. Multi-Signal Analysis

Score each dimension 0-100:

**1. Technical Signal (weight: 25%)**
- Trend: price vs 20-day and 50-day MA
- Momentum: accelerating or decelerating moves
- Support/resistance: proximity to key levels
- Volume confirmation: trend moves on expanding volume

**2. Fundamental Signal (weight: 20%)**
- Valuation: P/E relative to sector (from fundamentals output)
- Growth: implied from price trajectory
- Quality: margins, balance sheet (when FMP available)

**3. Sector Signal (weight: 20%)**
- Is the sector leading or lagging?
- Rotation phase: early/mid/late cycle
- Relative strength vs sector peers

**4. Momentum Signal (weight: 20%)**
- Price momentum: 1W, 1M, 3M returns from price data
- Volume momentum: increasing/decreasing participation
- Breakout/breakdown proximity

**5. Sentiment Signal (weight: 15%)**
- Recent price action patterns
- Cross-market correlations
- Market-wide risk sentiment (check SPY/KOSPI context)

### 4. Present the Forecast

```
═══════════════════════════════════════════════
  STOCK RESEARCH: [TICKER] ([NAME])
  Market: [US/KR] | Price: [PRICE] | Date: [DATE]
═══════════════════════════════════════════════

  SIGNAL BREAKDOWN:
  ┌─────────────┬───────────┬──────────┐
  │ Signal      │ Reading   │ Score    │
  ├─────────────┼───────────┼──────────┤
  │ Technical   │ [BULL/..] │ [0-100]  │
  │ Fundamental │ [BULL/..] │ [0-100]  │
  │ Sector      │ [BULL/..] │ [0-100]  │
  │ Momentum    │ [BULL/..] │ [0-100]  │
  │ Sentiment   │ [BULL/..] │ [0-100]  │
  └─────────────┴───────────┴──────────┘

  COMPOSITE SCORE: [0-100]
  DIRECTION: [BULL / BEAR / NEUTRAL]
  CONFIDENCE: [0.50-0.95]
  TIMEFRAME: [1W/2W/1M]

  TARGET: [price] ([+X%])
  STOP: [price] ([-X%])

  REASONING:
  [3-5 sentences synthesizing the signals into a coherent thesis]

  KEY RISKS:
  - [Risk 1]
  - [Risk 2]
═══════════════════════════════════════════════
```

### 5. Offer to Log Prediction

After presenting the analysis, ask:

> "Want me to log this as a formal prediction? It will be tracked for accuracy scoring."

If yes, invoke:

```bash
bin/stock-cli predict create \
  --ticker NVDA \
  --market US \
  --direction BULL \
  --confidence 0.72 \
  --timeframe 1W \
  --entry-price 125.50 \
  --target-price 135.00 \
  --stop-price 120.00 \
  --reasoning "<synthesized thesis>" \
  --signals technical,momentum,sector \
  --source INTERACTIVE
```

Return the prediction ID to the user so they can reference it later.

## Confidence Calculation

Composite score maps to direction:
- **0-35**: BEAR
- **36-64**: NEUTRAL
- **65-100**: BULL

Confidence: `0.50 + (abs(composite - 50) / 50) * 0.45`
- Composite 50 → confidence 0.50 (coin flip)
- Composite 75 → confidence 0.725
- Composite 100 → confidence 0.95

## Korean Stock Specifics

- Default timeframe: **2W** (not 1W) due to lower liquidity
- Widen stop-loss by ~20% vs US equivalent
- Samsung Electronics (005930) and SK Hynix (000660) are proxies for global semi demand
- Consider won/dollar exchange rate impact on export-heavy companies
- Chaebol discount: Korean conglomerates typically trade at lower P/E than global peers
