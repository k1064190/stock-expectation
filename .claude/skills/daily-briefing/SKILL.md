---
name: daily-briefing
description: Morning market briefing with stock picks for US and Korean markets. Generates a daily report covering macro environment, sector rotation, top movers, and 3-5 actionable predictions with direction, confidence level, and reasoning. Each pick is logged as a formal prediction for track record tracking. Triggers on keywords like daily briefing, morning report, market overview, today's picks, what should I trade, 오늘 시장, 일일 브리핑.
---

# Daily Briefing

Generate a comprehensive morning market briefing with actionable stock predictions for both US and Korean markets.

## When to Use

- Start of trading day (morning routine)
- When you want today's market overview with specific picks
- When you want predictions logged for track record tracking

## Prerequisites

- `bin/stock-cli` must be executable (uses uv-managed environment)
- No API keys required for Korean data (PyKRX)
- Optional: `FMP_API_KEY` for enhanced US data

## Workflow

All data access and prediction logging go through `bin/stock-cli` via Bash.
Every command returns JSON — parse it, analyze it, then call the next command.

### 1. Gather Market Data

**US indices and sectors:**
```bash
bin/stock-cli price SPY --market US --days 10
bin/stock-cli price QQQ --market US --days 10
bin/stock-cli price DIA --market US --days 10

# Sector ETFs
bin/stock-cli price XLK --market US --days 10
bin/stock-cli price XLF --market US --days 10
bin/stock-cli price XLE --market US --days 10
bin/stock-cli price XLV --market US --days 10
bin/stock-cli price XLI --market US --days 10
bin/stock-cli price XLP --market US --days 10
bin/stock-cli price XLU --market US --days 10
```

**Korean blue chips:**
```bash
bin/stock-cli price 005930 --market KR --days 10   # 삼성전자
bin/stock-cli price 000660 --market KR --days 10   # SK하이닉스
bin/stock-cli price 035420 --market KR --days 10   # NAVER
bin/stock-cli price 051910 --market KR --days 10   # LG화학
bin/stock-cli price 006400 --market KR --days 10   # 삼성SDI
bin/stock-cli price 005380 --market KR --days 10   # 현대자동차
```

**Check existing state:**
```bash
bin/stock-cli predict list --status OPEN --limit 20
bin/stock-cli track-record --days 30
```

### 2. Analyze Market Environment

Assess the following dimensions:

**Macro regime:**
- US: risk-on vs risk-off signals (SPY trend, sector rotation patterns)
- Korean: KOSPI breadth, foreign investor flow direction, won/dollar impact

**Sector rotation:**
- Which sectors are leading/lagging over 1W and 1M?
- US tech vs Korean semis correlation
- Defensive vs cyclical positioning

**Momentum & breadth:**
- Are indices making new highs with broad participation?
- KOSDAQ vs KOSPI relative strength

### 3. Generate and Log Predictions

For each pick, call `predict create` with all fields:

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
  --reasoning "Strong breakout above 20-day MA with volume confirmation. Sector leadership in semis. AI capex narrative intact." \
  --signals technical,momentum,sector \
  --source LIVE
```

For Korean stocks:

```bash
bin/stock-cli predict create \
  --ticker 005930 \
  --market KR \
  --direction BULL \
  --confidence 0.65 \
  --timeframe 2W \
  --entry-price 186200 \
  --target-price 195000 \
  --stop-price 178000 \
  --reasoning "Oversold bounce from key support. US semi recovery tailwind. Foreign net buying returning." \
  --signals technical,cross_market,breadth \
  --source LIVE
```

**Prediction quality rules:**
- Minimum confidence 0.55 (don't predict coin flips)
- Maximum confidence 0.85 for daily picks
- Every prediction must cite at least 2 signals
- Korean stocks default to 2W timeframe (lower liquidity)
- US stocks default to 1W timeframe
- Target must be at least 2x the stop distance (2:1 reward/risk)
- When invoking from interactive session, use `--source INTERACTIVE`
- When invoking from scheduled automation, use `--source LIVE`

### 4. Present the Briefing

Structure output as:

```markdown
# Daily Market Briefing — [DATE]

## Executive Summary
[3-5 bullet points: key themes, risk level, overall bias]

## US Market Overview
[SPY/QQQ/DIA performance, sector leaders/laggards, key levels]

## Korean Market Overview
[KOSPI/KOSDAQ performance, Samsung/SK Hynix, foreign flows]

## Today's Predictions

### US Picks
[2-3 predictions with full details, including prediction IDs from CLI output]

### Korean Picks
[2-3 predictions with full details, including prediction IDs from CLI output]

## Track Record Update
[Current win rate, recent streak, calibration notes from track-record output]

## Key Events Today
[Economic releases, earnings, central bank decisions]
```

Include the prediction IDs returned by `predict create` so the user can
reference them later with `bin/stock-cli predict detail <id>`.
