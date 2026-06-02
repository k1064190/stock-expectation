# FinceptTerminal — External Analysis (ideas-only extraction)

Clone analyzed: `/home/cwh/projects/stock-expectation/research/external/FinceptTerminal`
Target project: `/home/cwh/projects/stock-expectation` (Python CLI prediction + track-record system)
Date: 2026-06-02

## Overview & license

FinceptTerminal is a **C++20/Qt6 desktop "Bloomberg-style" terminal** (~53% C++, ~46% Python
helper scripts launched by the Qt app via a `PythonRunner` bridge). It is **dual-licensed
AGPL-3.0 / commercial**, with an explicit, aggressive commercial-license requirement for any
business/internal use, including forks (`LICENSE`, `docs/COMMERCIAL_LICENSE.md`).

**Hard constraint: IDEAS ONLY. No code, no prompt text, no config files may be copied.** Even the
Python scripts are AGPL and live behind the same dual-license notice. Anything we adopt must be
independently re-implemented in our own words/code. The bulk of the repo (Qt UI, the in-process
`DataHub` pub/sub bus, the C++ MCP server, vessel/AIS tracking, crypto wallet connect, the Qlib +
RDAgent ML stack) is **not portable** to a Python CLI + Claude-skills system and should be ignored.

What *is* conceptually valuable: how they organize **investor-persona agents**, a **Bull/Bear/Judge
debate** pattern, and their **data-source catalog** (most of which is paid, but a few free ones we
don't yet use).

## Persona-agent framework (concept)

The README advertises "30+ agents," but most are **geopolitics agents** (19: Brzezinski "Grand
Chessboard," Marshall "Prisoners of Geography," Kissinger "World Order") and **hedge-fund-shop
agents** (8: Bridgewater, Citadel, Renaissance, Two Sigma, etc.). Those are thematic/marketing
flavor, not directly useful for stock-level prediction.

The genuinely useful part is the **legendary-investor persona set** — 11 personas defined declaratively
in a single config file (`fincept-qt/scripts/agents/TraderInvestorsAgent/configs/agent_definitions.json`,
776 lines): Buffett, Graham, Lynch, Munger, Klarman, Howard Marks, Joel Greenblatt, Einhorn, Bill
Miller, Eveillard, Marty Whitman.

**Each persona is a self-contained data structure**, not bespoke code. The fields per persona:

- `instructions` — a long, *opinionated* system prompt that encodes the investor's discipline as
  hard rules. E.g. the Buffett spec demands "every recommendation has a named moat source, a
  returns-on-capital number, a management-quality check, and a valuation. No exceptions," forces a
  circle-of-competence gate, and lists explicit DO-NOTs ("don't call something 'wonderful' without
  naming the moat"). Graham's is "a numbers machine. Narratives lose to quantitative screens."
  Lynch's enforces "no PEG, no recommendation." Marks's places the asset in a cycle and sizes risk.
- `scoring_weights` — numeric weights summing to 1.0 that turn the qualitative lens into a single
  score. Buffett: moat 0.30, earnings predictability 0.25, financial strength 0.20, management 0.15,
  valuation 0.10. Greenblatt (Magic Formula): ROC 0.5 + earnings-yield 0.5. Klarman: risk 0.4 +
  margin-of-safety 0.3 + value 0.2 + liquidity 0.1.
- `output_schema` — a fixed output contract (`signal: bullish|neutral|bearish`, `confidence 0-1`,
  per-dimension 0-10 sub-scores, `reasoning`). This makes outputs comparable across personas.
- `analysis_rules` / `thresholds` — point allocations (e.g. "ROE ≥ 15% for 7/10 yrs → +N points")
  and bullish/bearish score cutoffs, so the same input yields a deterministic-ish label.
- `tools` + `data_sources` — declares what the persona needs (line items, years of history, period).

Conceptual takeaway: **a persona = {discipline-as-prompt} + {weighted rubric} + {fixed output
schema}**. Swapping personas = swapping a config blob, not rewriting an analyzer. The same ticker run
through Buffett vs. Lynch vs. Marks yields three structured, comparable verdicts — directly attacking
our known "single-perspective analysis bias" problem.

### Multi-agent debate pattern

Separate from the personas, `fincept-qt/scripts/agno_trading/core/debate_orchestrator.py` implements
a **Bull → Bear → Analyst-judge** sequence:

1. Bull agent: build the strongest BUY case from the same market context.
2. Bear agent: build the strongest SELL/AVOID case from the *same* context.
3. Analyst/judge agent: receives both arguments + raw data, emits a structured decision
   (`DECISION / CONFIDENCE / ENTRY / STOP / TAKE-PROFIT / POSITION-SIZE / REASONING`), which is then
   parsed into fields and persisted.

This is a lightweight, prompt-only adversarial framework — no special infra required. It is the most
directly portable structural idea in the repo.

## Data-source catalog

We already use: yfinance, FMP, Finnhub, AlphaVantage, OpenDART, Naver.

Connectors live in `fincept-qt/src/screens/data_sources/connectors/*.cpp` (C++ UI registry) and the
DataHub phase docs (`fincept-qt/docs/datahub-phases/`). Inventory of what they integrate:

**Market data (mostly redundant or paid):** Yahoo Finance ✔(have), Alpha Vantage ✔(have), Finnhub
✔(have), Polygon.io, Tiingo, Twelve Data, Marketstack, IEX Cloud, Quandl/Nasdaq Data Link,
Nasdaq TotalView, Databento — all of these are paid or freemium-with-tight-limits; none compelling
enough to add over our current FMP/yfinance stack.

**Crypto (free, not relevant to our US/KR equity scope):** CoinGecko, CoinMarketCap, Binance,
Coinbase, Kraken. Skip unless we ever add crypto.

**Alternative data (essentially all paid/enterprise — IGNORE):** RavenPack, Refinitiv Tick History,
Bloomberg Second Measure, Earnest Research, Thinknum, Orbital Insight, SafeGraph/Placer.ai, Revelio
Labs (web traffic, foot traffic, card transactions, headcount/hiring). Conceptually interesting
"alt-data lenses" but none free.

**FREE sources we do NOT use — worth flagging:**

| Source | What it gives | Free? | Verdict for us |
|---|---|---|---|
| **DBnomics** | Single REST aggregator over **FRED, World Bank, IMF, Eurostat, OECD, BIS, ECB, BLS** macro series | Yes, **no API key** | **Add.** One connector unlocks global macro for our regime/macro skills without juggling 7 keys. |
| **FRED** (direct) | US macro series (rates, CPI, unemployment, spreads) | Yes (free key) | Useful, but DBnomics fronts it key-free. |
| **World Bank / IMF / OECD / Eurostat** | International macro, country indicators | Yes | Relevant for KR-vs-US cross-market context; reachable via DBnomics. |
| **SEC EDGAR** | US filings / fundamentals (full-text + structured financial facts API) | Yes, no key | **Consider.** Free fundamentals backstop for US when FMP free-tier (250/day) is exhausted. |

DBnomics is the standout: it directly serves our `macro-regime-detector`, `daily-briefing`, and
`korean-market-analysis` skills (won/dollar, KR vs US rates, global liquidity) with **zero new API
keys**.

## PORTABLE IDEAS FOR OUR PROJECT (ranked)

### 1. Investor-persona "analytical lenses" as config-driven prompts — directly fixes single-perspective bias
- **Idea:** Re-implement (in our own words) a small set of named investor lenses — Buffett (quality/moat),
  Graham (deep value), Lynch (GARP/PEG), Marks (cycle/risk) — each as a structured rubric: weighted
  sub-scores + fixed output schema (`signal / confidence / per-dimension scores / reasoning`). Run a
  ticker through 2-4 lenses and surface where they agree/disagree.
- **Touches:** new persona definitions consumed by `.claude/skills/expect/` and
  `.claude/skills/stock-research/`; lens specs stored as data (e.g. a YAML/JSON under the skill dir)
  rather than code. Optionally a thin `stock_cli.py` helper to assemble the fundamentals each lens needs.
- **Effort:** M. **Impact:** High — this is the most direct cure for our stated bias problem and adds
  comparable, multi-angle verdicts to predictions we already log.

### 2. Bull/Bear/Judge debate step inside `/expect`
- **Idea:** Before emitting the final BUY/WATCH/HOLD/AVOID/SELL label, generate an explicit bull case
  and bear case from the *same* gathered data, then have a judge step weigh both and output the
  structured decision (action, confidence, entry/stop/target, reasoning). Persist the bull/bear text
  alongside the prediction for later postmortem.
- **Touches:** `.claude/skills/expect/SKILL.md` (add an adversarial sub-step); prediction record in
  `mcp-prediction-store/` to optionally store bull/bear rationale.
- **Effort:** S-M (prompt-structure change, no new infra). **Impact:** High — cheap, reduces
  one-sided confidence, and the stored bear case feeds `signal-postmortem`/`prediction-review`.

### 3. DBnomics macro connector (key-free global macro)
- **Idea:** Add a `macro` data path that pulls FRED/World Bank/IMF/OECD/ECB/BLS series via the single
  key-free DBnomics REST API. Re-implement as our own provider; do not copy their wrapper.
- **Touches:** new provider under `mcp-market-data/providers/` + a `stock-cli macro <series>`
  subcommand; consumed by `.claude/skills/macro-regime-detector/`, `daily-briefing`,
  `korean-market-analysis`.
- **Effort:** M. **Impact:** Medium-High — broad, free macro coverage with zero key management;
  enriches regime/briefing skills that currently lack first-class macro series.

### 4. Per-persona weighted scoring rubric pattern for our existing scorer
- **Idea:** Adopt the "discipline = {weighted sub-scores → single 0-1 confidence} + explicit
  thresholds + DO-NOT rules" pattern to make our `expect` technical/news scoring more transparent and
  tunable, with named weights instead of opaque heuristics.
- **Touches:** the scoring logic referenced by `.claude/skills/expect/` (and any scoring helper in
  `stock_cli.py`/`mcp-prediction-store/`).
- **Effort:** S-M. **Impact:** Medium — improves explainability and makes calibration adjustments
  (Stage 6 weekly loop) target concrete weights.

### 5. SEC EDGAR free-fundamentals backstop (US)
- **Idea:** Use SEC EDGAR's free, key-less company-facts / filings API as a fallback when FMP free
  tier is exhausted, for US fundamentals the persona lenses need (10y revenue, FCF, ROE, debt).
- **Touches:** US provider in `mcp-market-data/providers/`.
- **Effort:** M (EDGAR's XBRL facts shape needs mapping). **Impact:** Medium — resilience + free 10y
  history that the Buffett/Graham-style lenses depend on.

### Explicitly ignore
Qt/C++ desktop UI, the DataHub pub/sub bus, C++ MCP server, AIS/maritime + geopolitics agents, crypto
wallet connect, paid alt-data (RavenPack/Refinitiv/Thinknum/etc.), paid market vendors
(Polygon/Tiingo/Databento/IEX), and the entire Qlib + RDAgent ML/RL "AI Quant Lab" stack — the
factor-discovery concept is interesting but is a heavyweight ML platform, far out of scope for a
prediction CLI.
