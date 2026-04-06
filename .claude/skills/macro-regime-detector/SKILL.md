---
name: macro-regime-detector
description: Detect structural macro regime transitions (1-2 year horizon) using cross-asset ratio analysis. Analyze RSP/SPY concentration, yield curve, credit conditions, size factor, equity-bond relationship, and sector rotation to identify regime shifts between Concentration, Broadening, Contraction, Inflationary, and Transitional states. Run when user asks about macro regime, market regime change, structural rotation, or long-term market positioning.
---

> **Dual-market support**: This skill uses `bin/stock-cli` for data fetching, supporting both US and KR markets. Original FMP scripts preserved in `scripts/` for reference.

# Macro Regime Detector

Detect structural macro regime transitions using monthly-frequency cross-asset ratio analysis. This skill identifies 1-2 year regime shifts that inform strategic portfolio positioning.

## When to Use

- User asks about current macro regime or regime transitions
- User wants to understand structural market rotations (concentration vs broadening)
- User asks about long-term positioning based on yield curve, credit, or cross-asset signals
- User references RSP/SPY ratio, IWM/SPY, HYG/LQD, or other cross-asset ratios
- User wants to assess whether a regime change is underway

## Cross-Asset Instruments

### US
RSP (equal-weight S&P), SPY (S&P 500), IWM (Russell 2000), HYG (high yield),
LQD (investment grade), TLT (long treasury), XLE (energy), XLU (utilities), XLY/XLP (cyclical/defensive)

### KR
069500 KODEX 200 (KOSPI proxy), 229200 KODEX KOSDAQ 150, 005930 Samsung (large cap proxy),
253250 KODEX Total Bond (bond proxy)
Note: KR credit ETF equivalents limited — skip HYG/LQD component for KR analysis.

## Workflow

1. Load reference documents for methodology context:
   - `references/regime_detection_methodology.md`
   - `references/indicator_interpretation_guide.md`

2. Fetch cross-asset price data using `bin/stock-cli`:

   **US market:**
   ```bash
   bin/stock-cli price-batch RSP,SPY,IWM,HYG,LQD,TLT,XLE,XLU,XLY,XLP --market US --days 600
   ```

   **KR market:**
   ```bash
   bin/stock-cli price-batch 069500,229200,005930,253250 --market KR --days 600
   ```

   The original script `scripts/macro_regime_detector.py` (FMP API required) is preserved for reference.

3. Apply the 6-component scoring methodology to the fetched data and generate the regime report.

4. Provide additional context using `references/historical_regimes.md` when user asks about historical parallels.

## Prerequisites

- `bin/stock-cli` available in the project root

## 6 Components

| # | Component | Ratio/Data | Weight | What It Detects |
|---|-----------|------------|--------|-----------------|
| 1 | Market Concentration | RSP/SPY | 25% | Mega-cap concentration vs market broadening |
| 2 | Yield Curve | 10Y-2Y spread | 20% | Interest rate cycle transitions |
| 3 | Credit Conditions | HYG/LQD | 15% | Credit cycle risk appetite |
| 4 | Size Factor | IWM/SPY | 15% | Small vs large cap rotation |
| 5 | Equity-Bond | SPY/TLT + correlation | 15% | Stock-bond relationship regime |
| 6 | Sector Rotation | XLY/XLP | 10% | Cyclical vs defensive appetite |

## 5 Regime Classifications

- **Concentration**: Mega-cap leadership, narrow market
- **Broadening**: Expanding participation, small-cap/value rotation
- **Contraction**: Credit tightening, defensive rotation, risk-off
- **Inflationary**: Positive stock-bond correlation, traditional hedging fails
- **Transitional**: Multiple signals but unclear pattern

## Output

- `macro_regime_YYYY-MM-DD_HHMMSS.json` — Structured data for programmatic use
- `macro_regime_YYYY-MM-DD_HHMMSS.md` — Human-readable report with:
  1. Current Regime Assessment
  2. Transition Signal Dashboard
  3. Component Details
  4. Regime Classification Evidence
  5. Portfolio Posture Recommendations

## Relationship to Other Skills

| Aspect | Macro Regime Detector | Market Top Detector | Market Breadth Analyzer |
|--------|----------------------|--------------------|-----------------------|
| Time Horizon | 1-2 years (structural) | 2-8 weeks (tactical) | Current snapshot |
| Data Granularity | Monthly (6M/12M SMA) | Daily (25 business days) | Daily CSV |
| Detection Target | Regime transitions | 10-20% corrections | Breadth health score |
| API Calls | ~10 | ~33 | 0 (Free CSV) |

## Script Arguments

```bash
python3 macro_regime_detector.py [options]

Options:
  --api-key KEY       FMP API key (default: $FMP_API_KEY)
  --output-dir DIR    Output directory (default: current directory)
  --days N            Days of history to fetch (default: 600)
```

## Resources

- `references/regime_detection_methodology.md` — Detection methodology and signal interpretation
- `references/indicator_interpretation_guide.md` — Guide for interpreting cross-asset ratios
- `references/historical_regimes.md` — Historical regime examples for context
