---
name: korean-market-analysis
description: Korean stock market specialist covering KOSPI, KOSDAQ, chaebols, Korean semiconductor supply chain, and cross-market correlations with US markets. Analyzes Korean-specific patterns like chaebol discount, won/dollar impact, foreign investor flows, and KOSDAQ growth dynamics. Triggers on keywords like Korean market, KOSPI, KOSDAQ, 한국 시장, 코스피, 코스닥, Korean stocks, chaebol, 재벌, Samsung supply chain.
---

# Korean Market Analysis

Specialist analysis for the Korean stock market covering KOSPI blue chips, KOSDAQ growth stocks, chaebol dynamics, and cross-market correlations with US equities.

## When to Use

- When user asks about Korean market conditions
- When analyzing Korean stocks that may be affected by US market moves
- When evaluating Samsung supply chain or Korean semiconductor sector
- When assessing KOSPI/KOSDAQ divergence

## Prerequisites

- `bin/stock-cli` must be executable (uses uv-managed environment)
- No API keys required (PyKRX for Korean data)

## Korean Market Structure

### Key Indices
- **KOSPI**: Large-cap, blue-chip index (~800 stocks, dominated by chaebols)
- **KOSDAQ**: Growth/tech index (~1,600 stocks, higher volatility)

### Chaebol Dominance
Top 5 chaebols by KOSPI weight (approximate):
1. Samsung Group: ~25% (005930 Samsung Electronics, 006400 Samsung SDI, 028260 Samsung C&T)
2. SK Group: ~10% (000660 SK Hynix, 034730 SK Inc.)
3. Hyundai Motor Group: ~8% (005380 Hyundai Motor, 012330 Hyundai Mobis)
4. LG Group: ~5% (051910 LG Chem, 066570 LG Electronics)
5. NAVER/Kakao: ~5% (035420 NAVER, 035720 Kakao)

### Cross-Market Correlations

**US Tech → Korean Semis:**
- When NVDA/AMD/INTC move, Samsung (005930) and SK Hynix (000660) follow with ~1-day lag
- Correlation is strongest during earnings season
- HBM (High Bandwidth Memory) narrative directly links SK Hynix to AI capex

**US Rates → Korean Won:**
- Fed hawkish → USD/KRW rises → pressure on KOSPI (foreign selling)
- Export-heavy companies (Samsung, Hyundai) benefit from weak won operationally but face valuation headwinds

**China Recovery → Korean Industrials:**
- Chinese demand recovery → KOSPI materials/industrials rally
- Key proxies: POSCO (005490), LG Chem (051910), Hyundai Motor (005380)

## Workflow

All data access goes through `bin/stock-cli` via Bash.

### 1. Gather Korean Market Data

**Core blue chips:**
```bash
bin/stock-cli price 005930 --market KR --days 30   # 삼성전자
bin/stock-cli price 000660 --market KR --days 30   # SK하이닉스
bin/stock-cli price 035420 --market KR --days 30   # NAVER
bin/stock-cli price 051910 --market KR --days 30   # LG화학
bin/stock-cli price 006400 --market KR --days 30   # 삼성SDI
bin/stock-cli price 005380 --market KR --days 30   # 현대자동차
bin/stock-cli price 012330 --market KR --days 30   # 현대모비스
bin/stock-cli price 055550 --market KR --days 30   # 신한지주
bin/stock-cli price 005490 --market KR --days 30   # POSCO홀딩스
bin/stock-cli price 035720 --market KR --days 30   # 카카오
```

**KOSDAQ growth leaders:**
```bash
bin/stock-cli price 247540 --market KR --days 30   # 에코프로비엠
bin/stock-cli price 086520 --market KR --days 30   # 에코프로
bin/stock-cli price 403870 --market KR --days 30   # HPSP
bin/stock-cli price 196170 --market KR --days 30   # 알테오젠
```

### 2. Fetch US Reference Points

```bash
bin/stock-cli price NVDA --market US --days 30    # semi demand proxy
bin/stock-cli price SMH --market US --days 30     # semi ETF
bin/stock-cli price SPY --market US --days 30     # broad US market
```

Compare to Korean semis:
- Are Korean semis following US semi moves?
- Lag time: same day or 1-day delay?
- Divergence: if NVDA up but SK Hynix flat → investigate why

### 3. Korean-Specific Assessment

**Won/Dollar Impact:**
- Weak won (USD/KRW > 1,350): benefits exporters, hurts importers
- Strong won (USD/KRW < 1,250): attracts foreign capital, supports KOSPI valuation

**Foreign Investor Flows:**
- Track net buying/selling patterns (from price + volume data)
- Foreign selling + won weakening = risk-off signal
- Foreign buying + KOSDAQ outperformance = risk-on signal

**Chaebol Discount:**
- Korean conglomerates trade at 30-50% P/E discount vs global peers
- Samsung trades at ~10x P/E vs TSMC at ~20x
- This discount narrows during "Korea discount reform" narratives

**KOSDAQ vs KOSPI Ratio:**
- Rising ratio: risk appetite increasing, growth favored
- Falling ratio: flight to quality, defensive positioning

### 4. Output Format

```markdown
# Korean Market Analysis — [DATE]

## Market Status
- KOSPI blue chips performance summary
- Key movers up/down
- USD/KRW direction (if inferrable)

## Cross-Market Signals
- US semi move yesterday: [description]
- Expected Korean reaction: [thesis]
- Correlation confidence: [high/medium/low]

## Sector Heat Map
| Sector | 1D | 1W | 1M | Signal |
|--------|-----|-----|-----|--------|
| Semiconductors | | | | |
| EV/Battery | | | | |
| Internet/Platform | | | | |
| Financials | | | | |
| Auto/Transport | | | | |

## Top Opportunities
[2-3 Korean stock ideas with reasoning]

## Risk Factors
- [Won direction risk]
- [Foreign flow risk]
- [China demand risk]
- [Geopolitical risk]
```

### 5. (Optional) Log Predictions

If the user wants to act on the analysis, log predictions via:

```bash
bin/stock-cli predict create \
  --ticker 000660 \
  --market KR \
  --direction BULL \
  --confidence 0.65 \
  --timeframe 2W \
  --entry-price 876000 \
  --target-price 920000 \
  --stop-price 840000 \
  --reasoning "US semi recovery + oversold bounce from 1.04M high" \
  --signals cross_market,technical,mean_reversion \
  --source INTERACTIVE
```
