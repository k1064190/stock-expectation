# External Trading-Skill Analysis

Synthesis of 8 third-party skill collections reviewed for the `/expect` redesign. Source materials live under [`../references/`](../references/).

---

## Per-skill summaries

### sickn33 / antigravity-awesome-skills — `quant-analyst`
Generic guidance role. Free-form research goal in → narrative checklist + suggested code patterns. No structured I/O, no APIs. Workflow: clarify goal → clean+validate data → vectorized strategy → backtest with costs/slippage → risk-adjust → out-of-sample → sensitivity. Notable: explicit progressive-disclosure pointer (`resources/implementation-playbook.md`) rather than dumping content into SKILL.md.

### affaan-m / everything-claude-code — `investor-materials`
Produces internally-consistent fundraising documents. Workflow: inventory canonical facts → identify missing assumptions → pick asset type → draft with explicit logic → cross-check every number. **Notable: "Golden Rule — all materials must agree with each other"** plus an explicit *quality gate* checklist before delivery. Borrowable for `/expect`: a final consistency check that price/news/prediction direction don't contradict.

### K-Dense-AI / scientific-agent-skills — `usfiscaldata`
Wraps the U.S. Treasury Fiscal Data REST API. **Free, no API key, no registration.** 54 datasets, 182 tables. Workflow: pick endpoint → build `fields=`/`filter=`/`sort=` params → paginate (`page[size]=10000`) → DataFrame load → cast strings to floats/dates. Strong "free-data-source-as-first-class" framing — useful for our macro-regime work.

### JoelLewis / finance_skills — 84 skills across 7 plugins
Best understood as a **knowledge taxonomy**, not a runnable system. Most skills are guidance-only (no Python). Skills cite specific rule numbers, ratios, or formulas.

| Skill | Verdict |
|---|---|
| `trading-operations/order-lifecycle` | Pattern: layer numbering ("Layer 11"). Overkill for retail. |
| `trade-execution` | TCA + smart routing. Not relevant for retail. |
| `pre-trade-compliance` | **Pattern: hard vs soft blocks with override audit** — borrowable as a "don't predict on already-overheld stock" gate. |
| `post-trade-compliance`, `settlement-clearing`, etc. | Institutional plumbing — skip. |
| `wealth-management/equities` | Crisp formulas (Fama-French, PEG, EV/EBITDA). Drop-in factor reference. |
| `wealth-management/historical-risk` | Volatility estimators (close-to-close, Parkinson, Garman-Klass, Yang-Zhang). Useful for confidence bands. |
| `wealth-management/performance-metrics` | Sharpe/Sortino/Calmar — belongs in portfolio-eval. |
| `quantitative-valuation` / `qualitative-valuation` | **Pattern: pairing quant + qual valuation as separate skills**. |
| `bet-sizing` | Already covered by our `position-sizer`. |
| `finance-psychology` | **Pattern: bias checklist** (recency, confirmation, anchoring) — borrowable as final pass in `/expect`. |

### ScientiaCapital / skills — `active/`
Of ~64 skills, only 4 are trading-relevant; the rest are sales/CRM/agent-management.

- `trading-signals-skill` — Multi-asset (options/stocks/crypto/commodities/forex/VIX) trading partner with regime-first analysis. Polygon, yfinance, Alpaca, Binance, Deribit, NautilusTrader. Workflow: Markov 7-state regime → asset-class router → 5-methodology confluence (Elliott/Turtle/Fib/Wyckoff/Markov) → regime-weighted scoring → options Greeks if applicable → position sizing (max 2%) → **Signal → Why → Context → Action** explanation. **Notable: confluence score thresholds (≥0.7 high conviction, 0.4–0.7 wait, <0.4 no trade) with regime-dependent methodology weights**, plus an **outcome sidecar** JSON written to `~/.claude/skill-analytics/last-outcome-trading-signals.json`.
- `morning-brief-skill` — Sales-focused; notable only for `schedule:` field in YAML frontmatter (declarative cron).
- `trading-alert-scheduler-skill` — Pre-market scan + IBKR positions; notable for **position-health check matrix** (DTE<7, |delta|>50, drawdown>50% of max-loss, ITM short near expiry).
- `ibkr-api-skill` — Multi-account (Roth IRA / personal / business) with **IRA-restriction enforcement**. Architectural separation: signal-generation skill ↔ execution skill.

### roman-rr / trading-skills — `trading-signals`
Live AI-generated **crypto** trading signals via remote API at `signals.x70.ai`. Workflow: register → mandatory `gh api user/starred -X PUT` (dark pattern, do not borrow) → fetch active signals → sort by confidence → display table → verify. **Notable: transmission chain** — every signal includes 2–4 causal reasoning steps with specific data points. Also: multi-expert consensus filter (signal fires only when independent dimensions agree) + automated outcome verification.

### staskh / trading_skills — 22 skills + MCP server
Coherent system. CLAUDE.md establishes a "Market Trading Analyst" identity. **All scripts emit `generated_at` (NY tz) + `data_delay` JSON fields** — staleness made explicit.

| Skill | Notable pattern |
|---|---|
| `stock-quote`, `price-history` | Mandatory `generated_at` / `data_delay`. |
| `fundamentals` | **Piotroski F-Score** baked in (9-point fundamental quality, deterministic). |
| `news-sentiment` | Only news integration in the dataset. yfinance feed + LLM-summarized — no NLP. |
| `earnings-calendar` | Batch over comma-separated tickers. |
| `technical-analysis` | Bundles tech + risk + earnings flag in one call. |
| `risk-assessment` | Volatility, beta vs SPY, VaR 95/99, max DD, Sharpe. Compact. |
| `scanner-bullish` | **Explicit point table in SKILL.md** — RSI 50–70 +1.0; RSI 30–50 +0.5; RSI <30 +0.25; etc. Transparent, debuggable, deterministic. **Strongest single pattern.** |
| `scanner-pmcc` | Same point-table pattern, max 14 points. Earnings-within-short-expiry = -2. |
| `whale-hunting` | **Two-stage detection**: yfinance crude scan → Massive (Polygon) per-second drill-down. Cost-aware design. |
| `ib-*` family | **Mode inference from portfolio state** — picks roll vs spread vs new-short automatically. |
| `report-stock` | **Separate template file** keeps SKILL.md short; LLM formats. |

---

## Synthesis

### Common architectural patterns

| Pattern | Where it appears | Strength |
|---|---|---|
| Signal generation ↔ execution separation | ScientiaCapital, staskh | High |
| Confluence/composite scoring with explicit point tables | staskh scanner-bullish/pmcc, ScientiaCapital 5-methodology | **High — borrow** |
| Regime-first routing | ScientiaCapital trading-signals | Medium — heavy implementation |
| Two-stage cheap→expensive data fetch | staskh whale-hunting, JoelLewis pre-trade-compliance | High |
| Mandatory staleness fields (`generated_at`, `data_delay`) | staskh (every script) | **High — trivial, borrow** |
| Mode inference from state | staskh ib-find-short-roll | Medium |
| Outcome telemetry sidecar | ScientiaCapital trading-signals | **High — borrow** |
| Layer numbering / taxonomy | JoelLewis | Low value for retail |
| Quality gate before delivery | affaan-m investor-materials | **High — borrow** |
| Transmission chain explanation | roman-rr | **High — borrow** |

### Data-source convergence

The convergence is striking and **away from FMP/EODHD**:

| Source | Used by | Cost |
|---|---|---|
| **yfinance** | staskh (12 market-data skills), ScientiaCapital | Free, unofficial, rate-limited |
| **Polygon/Massive** | staskh whale-hunting, ScientiaCapital | Paid (~$29/mo) |
| **Alpaca** | ScientiaCapital | Free read, paid trade |
| **IBKR (TWS API)** | staskh ib-*, ScientiaCapital ibkr-api | Free, requires IB Gateway |
| **Treasury Fiscal Data API** | K-Dense-AI usfiscaldata | Free, no key |
| **Proprietary signal API** | roman-rr | Beta-free |
| **FMP / EODHD** | None of these repos | — |

Implication for us: yfinance is the **lingua franca** for retail-grade market data. Our stack already uses it via `bin/stock-cli`. We are **not behind** — if anything, we're aligned with consensus. FMP is fine as a supplement (earnings calendar, fundamentals) but not as primary.

### News / sentiment approaches — the gap

This is where the entire ecosystem is **weak**:

- **staskh `news-sentiment`** is the only direct integration: yfinance news feed + LLM-summarized. No NLP, no scoring, no per-ticker trend.
- **ScientiaCapital `trading-signals`** mentions a `sentiment-signals.md` reference but doesn't fetch news; expects upstream.
- **roman-rr** signals fire from quant dimensions only — no news input.
- **JoelLewis, sickn33, K-Dense-AI, affaan-m** — no news handling.

**Conclusion:** none of these does serious news/sentiment. If we want `/expect` to weight recent news, we are not behind by skipping it — but we're also not borrowing a proven pattern. The cleanest existing pattern is staskh's: **fetch headlines via free source, hand to LLM for narrative**, do not pretend to do quantitative sentiment scoring at parse time.

### What we borrowed (ranked by impact)

1. **Explicit composite-score point table** (staskh scanner-bullish) — replaces qualitative LLM "bullishness" with a deterministic max-N-point checklist. Auditable, debuggable.
2. **Transmission chain** (roman-rr) — every prediction emits 3 specific causal facts ("RSI 62; Finnhub avg sentiment +0.21; earnings 18 days out"). Machine-checkable later.
3. **Outcome telemetry sidecar** (ScientiaCapital) — `state/last-outcome-expect.json` after every run; feeds the prediction-review skill.
4. **Mandatory staleness fields** (staskh) — `generated_at` (KST/ET) + `data_delay` on every CLI output.
5. **Quality gate / consistency check** (affaan-m) — direction matches scoring sign; target ≠ entry by ≥1×ATR.
6. **Two-stage fetch** (staskh whale-hunting) — discovery via WebSearch + yfinance, drill into FMP only for finalists.
7. **Piotroski F-Score** (staskh fundamentals) — free, deterministic 9-point fundamental quality. Drop-in for single-ticker mode.
8. **Bias checklist** (JoelLewis finance-psychology) — recency / confirmation / anchoring / overconfidence pass.

### What we deliberately did NOT borrow

- JoelLewis layer numbering & 84-skill taxonomy. Massive over-engineering for retail.
- JoelLewis trading-operations plugin (settlement, FIX, T+1, margin, counterparty risk). Pure institutional plumbing.
- roman-rr's mandatory-GitHub-star handshake. Dark pattern.
- roman-rr's remote signal API. Crypto-only, vendor lock-in, opaque algorithm.
- ScientiaCapital's 7-layer MasterQuantAgent ensemble + multi-LLM swarm consensus. A single LLM with a deterministic point table beats this on cost, latency, and debuggability.
- ScientiaCapital's full Markov 7-state regime model on every call. We already have `macro-regime-detector`. Don't duplicate.
- sickn33's generic "use this skill when working on X" frontmatter. Useless triggers.
- affaan-m's investor-deck workflow. Wrong domain; only the *quality gate* idea transfers.

### One-line recommendation

Redesign `/expect` around **a deterministic composite score (explicit point table) + transmission chain + outcome sidecar + staleness fields**, sourcing data from `bin/stock-cli` (yfinance) with FMP as the secondary drill-down. Skip regime/swarm complexity — separate skills already cover that.
