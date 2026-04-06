---
name: theme-detector
description: Detect and analyze trending market themes across sectors. Use when user asks about current market themes, trending sectors, sector rotation, thematic investing, what themes are hot or cold, or wants to identify bullish and bearish market narratives with lifecycle analysis.
---

> **Dual-market support**: This skill uses `bin/stock-cli` for data fetching, supporting both US and KR markets. Original FMP scripts preserved in `scripts/` for reference.

# Theme Detector

## Overview

This skill detects and ranks trending market themes by analyzing cross-sector momentum, volume, and breadth signals. It identifies both bullish (upward momentum) and bearish (downward pressure) themes, assesses lifecycle maturity (Emerging/Accelerating/Trending/Mature/Exhausting), and provides a confidence score combining quantitative data with narrative analysis.

**3-Dimensional Scoring Model:**
1. **Theme Heat** (0-100): Direction-neutral strength of the theme (momentum, volume, uptrend ratio, breadth)
2. **Lifecycle Maturity**: Stage classification (Emerging / Accelerating / Trending / Mature / Exhausting) based on duration, extremity clustering, valuation, and ETF proliferation
3. **Confidence** (Low / Medium / High): Reliability of the detection, combining quantitative breadth with narrative confirmation. Script output is capped at Medium; Claude's WebSearch narrative confirmation step can elevate to High.

**Key Features:**
- Cross-sector theme detection using FINVIZ industry data
- Direction-aware scoring (bullish and bearish themes)
- Lifecycle maturity assessment to identify crowded vs. emerging trades
- ETF proliferation scoring (more ETFs = more mature/crowded theme)
- Integration with uptrend-dashboard for 3-point evaluation
- Dual-mode operation: FINVIZ Elite (fast) or public scraping (slower, limited)
- WebSearch-based narrative confirmation for top themes

---

## When to Use This Skill

**Explicit Triggers:**
- "What market themes are trending right now?"
- "Which sectors are hot/cold?"
- "Detect current market themes"
- "What are the strongest bullish/bearish narratives?"
- "Is AI/clean energy/defense still a strong theme?"
- "Where is sector rotation heading?"
- "Show me thematic investing opportunities"

**Implicit Triggers:**
- User wants to understand broad market narrative shifts
- User is looking for thematic ETF or sector allocation ideas
- User asks about crowded trades or late-cycle themes
- User wants to know which themes are emerging vs. exhausted

**When NOT to Use:**
- Individual stock analysis (use us-stock-analysis instead)
- Specific sector deep-dive with chart reading (use sector-analyst instead)
- Portfolio rebalancing (use portfolio-manager instead)
- Dividend/income investing (use value-dividend-screener instead)

---

## Workflow

### Step 1: Discover Themes via WebSearch

Use WebSearch to identify currently trending market themes before pulling price data:

```
Search: "market themes trending [current month] [current year]"
Search: "sector rotation [current month] [current year] stocks"
Search: "KR market themes [current month] [current year]" (for Korean market)
```

Identify 5–10 candidate themes (e.g., AI, defense, clean energy, semiconductors, K-battery).

### Step 2: Find Representative Stocks per Theme

Use `bin/stock-cli search` to find representative tickers for each detected theme:

```bash
# US market — find stocks matching a theme keyword
bin/stock-cli search "artificial intelligence" --market US

# KR market — find stocks matching a theme keyword
bin/stock-cli search "배터리" --market KR
bin/stock-cli search "반도체" --market KR
```

Supplement with tickers from `references/cross_sector_themes.md` theme definitions.

### Step 3: Fetch Price Data for Theme Scoring

```bash
# US — fetch price data for theme representative stocks
bin/stock-cli price-batch NVDA,AMD,MSFT,GOOGL,META --market US --days 90

# KR — fetch price data for theme representative stocks
bin/stock-cli price-batch 006400,373220,207940,000660 --market KR --days 90
```

The original FINVIZ-based script `scripts/theme_detector.py` is preserved for reference (FINVIZ Elite or public scraping mode).

### Step 4: Read and Parse Detection Results

Using the fetched price data, compute theme-level scores:
- Momentum: average % change over 1-week and 1-month windows
- Volume: relative volume vs average
- Breadth: fraction of theme stocks in uptrend (above 50-day MA)
- Direction: majority bullish vs bearish across theme members

### Step 4: Perform Narrative Confirmation via WebSearch

For the top 5 themes (by Theme Heat score), execute WebSearch queries to confirm narrative strength:

**Search Pattern:**
```
"[theme name] stocks market [current month] [current year]"
"[theme name] sector momentum [current month] [current year]"
```

**Evaluate narrative signals:**
- **Strong narrative**: Multiple major outlets covering the theme, analyst upgrades, policy catalysts
- **Moderate narrative**: Some coverage, mixed sentiment, no clear catalyst
- **Weak narrative**: Little coverage, or predominantly contrarian/skeptical tone

Update Confidence levels based on findings:
- Quantitative High + Narrative Strong = **High** confidence
- Quantitative High + Narrative Weak = **Medium** confidence (possible momentum divergence)
- Quantitative Low + Narrative Strong = **Medium** confidence (narrative may lead price)
- Quantitative Low + Narrative Weak = **Low** confidence

### Step 5: Analyze Results and Provide Recommendations

Cross-reference detection results with knowledge bases:

**Reference Documents to Consult:**
1. `references/cross_sector_themes.md` - Theme definitions and constituent industries
2. `references/thematic_etf_catalog.md` - ETF exposure options by theme
3. `references/theme_detection_methodology.md` - Scoring model details
4. `references/finviz_industry_codes.md` - Industry classification reference

**Analysis Framework:**

For **Hot Bullish Themes** (Heat >= 70, Direction = Bullish):
- Identify lifecycle stage (Emerging = opportunity, Mature/Exhausting = caution)
- List top-performing industries within the theme
- Recommend proxy ETFs for exposure
- Flag if ETF proliferation is high (crowded trade warning)

For **Hot Bearish Themes** (Heat >= 70, Direction = Bearish):
- Identify industries under pressure
- Assess if bearish momentum is accelerating or decelerating
- Recommend hedging strategies or sectors to avoid
- Note potential mean-reversion opportunities if lifecycle is Mature/Exhausting

For **Emerging Themes** (Heat 40-69, Lifecycle = Emerging):
- These may represent early rotation signals
- Recommend monitoring with watchlist
- Identify catalyst events that could accelerate the theme

For **Exhausted Themes** (Heat >= 60, Lifecycle = Exhausting):
- Warn about crowded trade risk
- High ETF count confirms excessive retail participation
- Consider contrarian positioning or reducing exposure

### Step 6: Generate Final Report

Present the final report to the user using the report template structure:

```markdown
# Theme Detection Report
**Date:** YYYY-MM-DD
**Mode:** FINVIZ Elite / Public
**Themes Analyzed:** N
**Data Quality:** [note any limitations]

## Theme Dashboard
[Top themes table with Heat, Direction, Lifecycle, Confidence]

## Bullish Themes Detail
[Detailed analysis of bullish themes sorted by Heat]

## Bearish Themes Detail
[Detailed analysis of bearish themes sorted by Heat]

## All Themes Summary
[Complete theme ranking table]

## Industry Rankings
[Top performing and worst performing industries]

## Sector Uptrend Ratios
[Sector-level aggregation if uptrend data available]

## Methodology Notes
[Brief explanation of scoring model]
```

Save the report to `reports/` directory.

---

## Resources

### Scripts Directory (`scripts/`)

**Main Scripts:**
- `theme_detector.py` - Main orchestrator script
  - Coordinates industry data collection, theme classification, and scoring
  - Generates JSON + Markdown output
  - Usage: `python3 theme_detector.py [options]`

- `theme_classifier.py` - Maps industries to cross-sector themes
  - Reads theme definitions from `cross_sector_themes.md`
  - Calculates theme-level aggregated scores
  - Determines direction (bullish/bearish) from constituent industries
  - Display mapping: "bullish" → "LEAD", "bearish" → "LAG" (see report_generator.py::_direction_label())

- `finviz_industry_scanner.py` - FINVIZ industry data collection
  - Elite mode: CSV export with full stock data per industry
  - Public mode: Web scraping with rate limiting
  - Extracts: performance, volume, change%, avg volume, market cap

- `calculators/lifecycle_calculator.py` - Lifecycle maturity assessment
  - Duration scoring, extremity clustering, valuation analysis
  - ETF proliferation scoring from thematic_etf_catalog.md
  - Stage classification: Emerging / Accelerating / Trending / Mature / Exhausting

- `report_generator.py` - Report output generation
  - Markdown report from template
  - JSON structured output
  - Theme dashboard formatting

### References Directory (`references/`)

**Knowledge Bases:**
- `cross_sector_themes.md` - Theme definitions with industries, ETFs, stocks, and matching criteria
- `thematic_etf_catalog.md` - Comprehensive thematic ETF catalog with counts per theme
- `finviz_industry_codes.md` - Complete FINVIZ industry-to-filter-code mapping
- `theme_detection_methodology.md` - Technical documentation of the 3D scoring model

### Assets Directory (`assets/`)

- `report_template.md` - Markdown template for report generation with placeholder format

---

## Important Notes

### FINVIZ Mode Differences

| Feature | Elite Mode | Public Mode |
|---------|-----------|-------------|
| Industry coverage | All ~145 industries | All ~145 industries |
| Stocks per industry | Full universe | ~20 stocks (page 1) |
| Rate limiting | 0.5s between requests | 2.0s between requests |
| Data freshness | Real-time | 15-min delayed |
| API key required | Yes ($39.50/mo) | No |
| Execution time | ~2-3 minutes | ~5-8 minutes |

### Direction Detection Logic

Theme direction is determined by majority vote of constituent industries' relative rank:

1. **Industry ranking**: All ~145 industries are ranked by multi-timeframe momentum score
2. **Rank-based direction**: Industries in the top half of the ranked list are classified as "bullish"; bottom half as "bearish"
3. **Theme majority vote**: `_majority_direction()` counts bullish vs. bearish industries within each theme; the majority wins

Display mapping: "bullish" → **LEAD**, "bearish" → **LAG** (see `report_generator.py::_direction_label()`)

A LEAD theme indicates relative outperformance of its constituent industries. A LAG theme may still have positive absolute returns — it indicates relative underperformance, not a short signal.

### Known Limitations

1. **Survivorship bias**: Only analyzes currently listed stocks and ETFs
2. **Lag**: FINVIZ data may lag intraday moves by 15 minutes (public mode)
3. **Theme boundaries**: Some stocks fit multiple themes; classification uses primary industry
4. **ETF proliferation**: Catalog is static and may not capture very new ETFs
5. **Narrative scoring**: WebSearch-based and inherently subjective
6. **Public mode limitation**: ~20 stocks per industry may miss small-cap signals

### Disclaimer

**This analysis is for educational and informational purposes only.**
- Not investment advice
- Past thematic trends do not guarantee future performance
- Theme detection identifies momentum, not fundamental value
- Conduct your own research before making investment decisions

---

**Version:** 1.0
**Last Updated:** 2026-02-16
**API Requirements:** FINVIZ Elite (recommended) or public mode (free); FMP API optional
**Execution Time:** ~2-8 minutes depending on mode
**Output Formats:** JSON + Markdown
**Themes Covered:** 14+ cross-sector themes
