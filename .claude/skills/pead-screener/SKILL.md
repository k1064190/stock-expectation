---
name: pead-screener
description: Screen post-earnings gap-up stocks for PEAD (Post-Earnings Announcement Drift) patterns. Analyzes weekly candle formation to detect red candle pullbacks and breakout signals. Supports two input modes - FMP earnings calendar (Mode A) or earnings-trade-analyzer JSON output (Mode B). Use when user asks about PEAD screening, post-earnings drift, earnings gap follow-through, red candle breakout patterns, or weekly earnings momentum setups.
---

> **Dual-market support**: This skill uses `bin/stock-cli` for data fetching, supporting both US and KR markets. Original FMP scripts preserved in `scripts/` for reference.

# PEAD Screener - Post-Earnings Announcement Drift

Screen post-earnings gap-up stocks for PEAD (Post-Earnings Announcement Drift) patterns using weekly candle analysis to detect red candle pullbacks and breakout signals.

## When to Use

- User asks for PEAD screening or post-earnings drift analysis
- User wants to find earnings gap-up stocks with follow-through potential
- User requests red candle breakout patterns after earnings
- User asks for weekly earnings momentum setups
- User provides earnings-trade-analyzer JSON output for further screening

## Prerequisites

- `bin/stock-cli` available in the project root
- For Mode B: earnings-trade-analyzer JSON output file with schema_version "1.0"

## Workflow

### Step 1: Fetch Price Data for Weekly Candle Analysis

Fetch 90 days of price history per ticker to construct weekly candles and analyze PEAD patterns:

```bash
# US market — single ticker
bin/stock-cli price NVDA --market US --days 90

# KR market — single ticker
bin/stock-cli price 005930 --market KR --days 90
```

> **Note:** For KR markets, use WebSearch to find Korean earnings announcement dates (e.g., search "005930 실적 발표일" or "Samsung Electronics earnings date"), as KR earnings calendars are not available through `bin/stock-cli`.

Repeat for each post-earnings gap-up candidate identified via an earnings calendar (use the earnings-calendar skill or WebSearch for earnings dates).

The original FMP-based script is preserved in `scripts/screen_pead.py` for reference.

**Mode B (earnings-trade-analyzer JSON input):** Use the candidate tickers from the earnings-trade-analyzer output as your ticker list, then fetch price data for each using the command above.

### Step 2: Review Results

1. Read the generated JSON and Markdown reports
2. Load `references/pead_strategy.md` for PEAD theory and pattern context
3. Load `references/entry_exit_rules.md` for trade management rules

### Step 3: Present Analysis

For each candidate, present:
- Stage classification (MONITORING, SIGNAL_READY, BREAKOUT, EXPIRED)
- Weekly candle pattern details (red candle location, breakout status)
- Composite score and rating
- Trade setup: entry, stop-loss, target, risk/reward ratio
- Liquidity metrics (ADV20, average volume)

### Step 4: Provide Actionable Guidance

Based on stages and ratings:
- **BREAKOUT + Strong Setup (85+):** High-conviction PEAD trade, full position size
- **BREAKOUT + Good Setup (70-84):** Solid PEAD setup, standard position size
- **SIGNAL_READY:** Red candle formed, set alert for breakout above red candle high
- **MONITORING:** Post-earnings, no red candle yet, add to watchlist
- **EXPIRED:** Beyond monitoring window, remove from watchlist

## Output

- `pead_screener_YYYY-MM-DD_HHMMSS.json` - Structured results with stage classification
- `pead_screener_YYYY-MM-DD_HHMMSS.md` - Human-readable report grouped by stage

## Resources

- `references/pead_strategy.md` - PEAD theory and weekly candle approach
- `references/entry_exit_rules.md` - Entry, exit, and position sizing rules
