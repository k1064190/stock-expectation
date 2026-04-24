---
name: portfolio-eval
description: Evaluate portfolio holdings — P&L report, risk analysis, prediction comparison, and trading advice. Use when user asks to evaluate, review, or analyze their portfolio. Triggers on "포트폴리오", "내 포트폴리오", "보유 종목", "evaluate portfolio", "portfolio review".
---

# Portfolio Evaluation

Comprehensive evaluation of the user's stock portfolio with four analysis dimensions.

## Prerequisites

Portfolio must exist. If not, create one first:
```bash
./bin/stock-cli portfolio create --market KR --name "Toss KR"
./bin/stock-cli portfolio create --market US --name "Toss US"
```

## Recording Trades

When the user mentions buying or selling stocks, record via CLI:
```bash
# Buy
./bin/stock-cli portfolio buy {TICKER} --qty {N} --price {PRICE} --market {US|KR} --date {YYYY-MM-DD}

# Sell
./bin/stock-cli portfolio sell {TICKER} --qty {N} --price {PRICE} --market {US|KR} --date {YYYY-MM-DD}
```

- For KR tickers: 6-digit codes (005930 for Samsung Electronics)
- For US tickers: alphabetic symbols (NVDA, AAPL)
- `--date` defaults to today if omitted
- `--note` for optional memo about the trade rationale
- `--thesis-id` to link to a trader-memory-core thesis

## Evaluation Workflow

When asked to evaluate a portfolio, run these commands and synthesize results:

### Step 1: Get current positions
```bash
./bin/stock-cli portfolio positions --market {US|KR}
```

### Step 2: Run all four evaluations
```bash
./bin/stock-cli portfolio report --market {US|KR}
./bin/stock-cli portfolio risk --market {US|KR}
./bin/stock-cli portfolio vs-predictions --market {US|KR}
./bin/stock-cli portfolio advice --market {US|KR}
```

### Step 3: Synthesize

Combine all four outputs into a comprehensive analysis:

1. **Performance Summary**: Total return, best/worst performers, realized vs unrealized
2. **Risk Assessment**: Concentration issues, sector imbalances, overweight warnings
3. **Prediction Alignment**: Are holdings consistent with your predictions? Flag mismatches.
4. **Action Items**: Based on advice signals, suggest specific actions:
   - Stocks hitting stop-loss levels
   - Stocks below key moving averages
   - Thesis review dates approaching

Present in a clear, structured format. Use tables for holdings data.

## Other Commands

```bash
# Transaction history
./bin/stock-cli portfolio transactions --market {US|KR} [--ticker {TICKER}]

# Import from CSV
./bin/stock-cli portfolio import {FILE} --market {US|KR} [--dry-run]

# Delete a transaction (typo fix)
./bin/stock-cli portfolio delete-tx {ID}

# Portfolio summary (quick view)
./bin/stock-cli portfolio summary --market {US|KR}
```
