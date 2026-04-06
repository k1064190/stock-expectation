---
name: market-breadth-analyzer
description: Quantifies market breadth health using TraderMonty's public CSV data. Generates a 0-100 composite score across 6 components (100 = healthy). No API key required. Use when user asks about market breadth, participation rate, advance-decline health, whether the rally is broad-based, or general market health assessment.
---

> **Dual-market support**: This skill uses `bin/stock-cli` for data fetching, supporting both US and KR markets. Original FMP scripts preserved in `scripts/` for reference.

# Market Breadth Analyzer Skill

## Purpose

Quantify market breadth health using a data-driven 6-component scoring system (0-100). Uses TraderMonty's publicly available CSV data to measure how broadly the market is participating in a rally or decline.

**Score direction:** 100 = Maximum health (broad participation), 0 = Critical weakness.

**No API key required** - uses freely available CSV data from GitHub Pages.

## When to Use This Skill

**English:**
- User asks "Is the market rally broad-based?" or "How healthy is market breadth?"
- User wants to assess market participation rate
- User asks about advance-decline indicators or breadth thrust
- User wants to know if the market is narrowing (fewer stocks participating)
- User asks about equity exposure levels based on breadth conditions

**Japanese:**
- 「マーケットブレッドスはどうですか？」「市場の参加率は？」
- 「上昇は広がっている？」「一部の銘柄だけの上昇？」
- ブレッドス指標に基づくエクスポージャー判断
- 市場の健康度をデータで確認したい

## Market Baskets (for bin/stock-cli breadth computation)

Use these representative baskets when TraderMonty's CSV is unavailable or for KR analysis:

**US basket (top 30 S&P 500 by market cap):**
AAPL, MSFT, NVDA, AMZN, META, GOOGL, GOOG, BRK-B, LLY, AVGO, TSLA, WMT, JPM, V, UNH, XOM, ORCL, MA, COST, HD, PG, JNJ, NFLX, ABBV, BAC, KO, CRM, CVX, MRK, CSCO

**KR basket (top 20 KOSPI by market cap):**
005930, 000660, 005380, 005490, 051910, 035420, 006400, 068270, 105560, 055550,
003550, 207940, 096770, 010130, 018260, 000270, 028260, 086790, 316140, 003670

## Prerequisites

- `bin/stock-cli` available in the project root (for dual-market breadth)
- **Internet access** to reach GitHub Pages URLs (for TraderMonty CSV, US-only)
- **No API keys required** for either data source

## Difference from Breadth Chart Analyst

| Aspect | Market Breadth Analyzer | Breadth Chart Analyst |
|--------|------------------------|----------------------|
| Data Source | CSV (automated) | Chart images (manual) |
| API Required | None | None |
| Output | Quantitative 0-100 score | Qualitative chart analysis |
| Components | 6 scored dimensions | Visual pattern recognition |
| Repeatability | Fully reproducible | Analyst-dependent |

---

## Execution Workflow

### Phase 1: Fetch Data

**Option A: TraderMonty CSV (US market, free, most accurate — ~2,800 stocks):**

```bash
uv run python .claude/skills/market-breadth-analyzer/scripts/market_breadth_analyzer.py \
  --detail-url "https://tradermonty.github.io/market-breadth-analysis/market_breadth_data.csv" \
  --summary-url "https://tradermonty.github.io/market-breadth-analysis/market_breadth_summary.csv"
```

**Option B: bin/stock-cli price-batch (US or KR, representative basket):**

```bash
# US basket
bin/stock-cli price-batch AAPL,MSFT,NVDA,AMZN,META,GOOGL,GOOG,BRK-B,LLY,AVGO,TSLA,WMT,JPM,V,UNH,XOM,ORCL,MA,COST,HD,PG,JNJ,NFLX,ABBV,BAC,KO,CRM,CVX,MRK,CSCO --market US --days 90

# KR basket
bin/stock-cli price-batch 005930,000660,005380,005490,051910,035420,006400,068270,105560,055550,003550,207940,096770,010130,018260,000270,028260,086790,316140,003670 --market KR --days 90
```

> **Note:** Computed breadth from a 20–30 stock basket is less precise than TraderMonty's ~2,800-stock CSV but enables dual-market analysis. Expected runtime ~5 seconds.

The original script `scripts/market_breadth_analyzer.py` (TraderMonty CSV) is preserved for US-only analysis.

**Both options:**
1. Validate data freshness (warn if > 5 days old)
2. Calculate all 6 component scores (with automatic weight redistribution if any component lacks data)
3. Generate composite score with zone classification
4. Track score history and compute trend (improving/deteriorating/stable)
5. Output JSON and Markdown reports

### Phase 2: Present Results

Present the generated Markdown report to the user, highlighting:
- Composite score and health zone
- Strongest and weakest components
- Recommended equity exposure level
- Key breadth levels to watch
- Any data freshness warnings

---

## 6-Component Scoring System

| # | Component | Weight | Key Signal |
|---|-----------|--------|------------|
| 1 | Breadth Level & Trend | **25%** | Current 8MA level + 200MA trend direction + 8MA direction modifier |
| 2 | 8MA vs 200MA Crossover | **20%** | Momentum via MA gap and direction |
| 3 | Peak/Trough Cycle | **20%** | Position in breadth cycle |
| 4 | Bearish Signal | **15%** | Backtested bearish signal flag |
| 5 | Historical Percentile | **10%** | Current vs full history distribution |
| 6 | S&P 500 Divergence | **10%** | Multi-window (20d + 60d) price vs breadth divergence |

**Weight Redistribution:** If any component lacks sufficient data (e.g., no peak/trough markers detected), it is excluded and its weight is proportionally redistributed among the remaining components. The report shows both original and effective weights.

**Score History:** Composite scores are persisted across runs (keyed by data date). The report includes a trend summary (improving/deteriorating/stable) when multiple observations are available.

## Health Zone Mapping (100 = Healthy)

| Score | Zone | Equity Exposure | Action |
|-------|------|-----------------|--------|
| 80-100 | Strong | 90-100% | Full position, growth/momentum favored |
| 60-79 | Healthy | 75-90% | Normal operations |
| 40-59 | Neutral | 60-75% | Selective positioning, tighten stops |
| 20-39 | Weakening | 40-60% | Profit-taking, raise cash |
| 0-19 | Critical | 25-40% | Capital preservation, watch for trough |

---

## Data Sources

**Detail CSV:** `market_breadth_data.csv`
- ~2,500 rows from 2016-02 to present
- Columns: Date, S&P500_Price, Breadth_Index_Raw, Breadth_Index_200MA, Breadth_Index_8MA, Breadth_200MA_Trend, Bearish_Signal, Is_Peak, Is_Trough, Is_Trough_8MA_Below_04

**Summary CSV:** `market_breadth_summary.csv`
- 8 aggregate metrics (average peaks, average troughs, counts, analysis period)

Both are publicly hosted on GitHub Pages - no authentication required.

## Output Files

- JSON: `market_breadth_YYYY-MM-DD_HHMMSS.json`
- Markdown: `market_breadth_YYYY-MM-DD_HHMMSS.md`
- History: `market_breadth_history.json` (persists across runs, max 20 entries)

## Reference Documents

### `references/breadth_analysis_methodology.md`
- Full methodology with component scoring details
- Threshold explanations and zone definitions
- Historical context and interpretation guide

### When to Load References
- **First use:** Load methodology reference for framework understanding
- **Regular execution:** References not needed - script handles scoring
