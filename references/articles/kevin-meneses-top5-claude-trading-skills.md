# Top 5 Claude Code Skills for Algorithmic Trading

**Author:** Kevin Meneses González
**Source:** [DataDrivenInvestor](https://medium.datadriveninvestor.com/top-5-claude-code-skills-for-algorithmic-trading-49620fa2b02c) (originally published as a LinkedIn pulse, April 2026)
**Captured:** 2026-05-10
**License:** content quoted under fair use for internal research.

---

## Thesis

The bottleneck in algorithmic trading is **implementation speed, not strategy quality**. SKILL.md files (Claude Code skills) function as automated recipes that encode decision workflows — the bottleneck shifts from "writing the code" to "deciding which strategies are worth testing".

## The 5 Skills

### 1. Backtesting Expert — Systematic Strategy Testing
- **Source:** [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills)
- **Workflow:** confirms strategy rules → fetches historical OHLCV → computes indicators → generates entry/exit signals → vectorized backtest → equity curve + summary → flags overfitting risks.
- **Data source:** EODHD REST API.
- **Pattern:** enforces explicit signal logic — RSI is computed from delta gains/losses, not imported. No black-box library calls.

### 2. Market Data Pipeline — EODHD Integration
- **Source:** JoelLewis/finance_skills (trading-operations plugin), `npx skills add JoelLewis/finance_skills --plugin trading-operations`
- **Workflow:** identifies data type → selects EODHD endpoint → builds request → normalizes to DataFrame → applies corporate-action adjustments → caches → returns production-ready data.
- **Data source:** EODHD (70+ exchanges, free tier, paid plans from €19.99/month).
- **Pattern:** standardized columns `[open, high, low, close, adjusted_close, volume]`.

### 3. Signal Generator — Strategy Logic to Executable Code
- **Source:** [ScientiaCapital/skills](https://github.com/scientiacapital/skills) `active/signal-generation-skill`
- **Workflow:** parses strategy rules → maps to pandas/numpy → vectorized indicators → builds entry/exit Series → applies session filters → DataFrame with signals → verifies no lookahead bias.
- **Pattern:** the EMA crossover example computes EMAs with `ewm().mean()` and ATR manually rather than importing pandas-ta — keeps the logic auditable.

### 4. Risk Manager — Position Sizing and Exposure Control
- **Source:** JoelLewis/finance_skills (wealth-management / risk-measurement)
- **Workflow:** ATR-based stop distance → fixed-fractional sizing (default 1% account risk) → portfolio-heat check (flags > 5%) → 95% Historical VaR → outputs entry, stop, shares, dollar risk, R-multiple, kill-switch threshold.
- **Pattern:** `shares = (equity * risk_pct / 100) / risk_per_share`.

### 5. Live Signal Monitor — Real-Time Alerts
- **Source:** [roman-rr/trading-skills](https://github.com/roman-rr/trading-skills) `trading-signals`
- **Workflow:** real-time quote + recent EOD bars → rolling window in memory → recompute indicators per bar → evaluate signal conditions → log + alert (print/Telegram/email) → **signal only, no order execution**.
- **Pattern:** 60-second polling loop appending live price as today's bar. Unsuitable for sub-minute strategies — use WebSocket for faster feeds.

## Architectural Principles

> "Bad data is the silent killer of backtests. Survivorship bias, unadjusted prices, missing corporate actions — these don't throw errors. They just make your strategy look better than it is."

1. **Signal/execution separation.** Never execute orders directly from monitoring code; output signals only.
2. **Unified data layer.** EODHD as a single source reduces API complexity and ensures consistent corporate-action adjustments.
3. **Workflow shift.** Implementation becomes cheap; the new bottleneck is *strategy evaluation*.

## Application to `/expect`

What we borrowed (see [docs/external-skills-analysis.md](../../docs/external-skills-analysis.md)):
- The signal/execution separation principle (we output BUY/WATCH/SELL labels, never trades).
- The "verifies no lookahead bias" check, applied as a quality gate in `/expect`.

What we did **not** borrow:
- EODHD as primary. We already use yfinance via `bin/stock-cli` and FMP for fundamentals.
- The 60-second polling. Our flow is on-demand (`/expect` is invoked by Doctor Cho), not a background loop.
