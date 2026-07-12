---
name: expect
description: "Stock expectation analysis. Combines algorithmic technical scoring + structured news/sentiment + LLM macro/narrative context + multi-horizon directional predictions to emit a deterministic BUY / WATCH / HOLD / AVOID / SELL recommendation per stock. Modes: (1) /expect KR or /expect US auto-discover 5 trending stocks, (2) /expect NVDA or /expect 삼성전자 single-ticker deep dive, (3) /expect NVDA,AMD,AVGO multi-ticker batch, (4) /expect ALL or /expect runs both markets. Triggers: expect, expectation, stock picks, what should I buy, hot stocks, trending stocks, buy or sell, 기대값, 종목 추천, 뭐 사야돼, 매수, 매도, 핫한 종목"
---

# Expect — Multi-Horizon Buy/Sell Recommender

## What this produces

Per stock:
- A deterministic **BUY / WATCH / HOLD / AVOID / SELL** label, derived from a fixed point table augmented by an LLM-judged macro/narrative score
- A **transmission chain** of exactly 3 facts: one technical, one news/fundamental, one risk
- A **composite score** in the range -11 .. +13 with the components broken out (ALGO + NEWS + LLM_CONTEXT)
- An **LLM context score** (-5 .. +3) with reasoning text capturing macro regime, sector lifecycle, and narrative that the deterministic table can't see
- Independent **multi-horizon predictions** (1W / 1M / 6M — the Cycle 1Y call is analysis-only, never logged; see RULE C4) logged to `data/predictions.db`
- An **outcome-telemetry sidecar** at `state/last-outcome-expect.json` for the weekly calibration loop

The label is the headline. The horizon predictions are the audit trail. The sidecar feeds future skill improvement.

## Modes

```
/expect KR              5 trending KR stocks
/expect US              5 trending US stocks
/expect 삼성전자         single-stock deep dive (resolve KR name → ticker)
/expect NVDA            single-stock deep dive (US ticker)
/expect NVDA,AMD,AVGO   multi-ticker batch
/expect ALL  or  /expect    full scan (US 5 + KR 5 = 10 stocks)
```

| Input shape | Mode |
|---|---|
| Bare `KR` / `US` / `ALL` / empty | Discovery + scan |
| Korean name | Single (resolve via `bin/stock-cli search "<name>" --market KR`) |
| English ticker | Single |
| Comma-separated tickers | Batch (skip discovery) |

---

## Workflow

### Step 1 — Pre-flight: track record + calibration (1 call)

Always run before scoring, even in single-stock mode:

```bash
bin/stock-cli track-record --days 30
bin/stock-cli calibration
bin/stock-cli regime --market US   # and/or --market KR for the market(s) you score
```

Surface to yourself:
- Recent overconfidence buckets (e.g. "0.70-0.80 confidence → 45% actual win rate" → cap output confidence at 0.65 for that band)
- Per-signal performance (if `news` signal is hitting <50% lately, weight news_score lower until calibration recovers)
- Open predictions for the same tickers (skip re-prediction; reference the existing one)
- **Market regime** (`regime` output `label`): the hard BULL gate — see RULE R1 below. Fetch once per run per market and reuse for every candidate in that market.

### Step 2 — Discovery (Market Scan and ALL modes only)

Per market, use **WebSearch** to surface 10 candidates:

```
US:  WebSearch: "trending US stocks today site:finviz.com OR site:reuters.com"
US:  WebSearch: "most active stocks [current month] [current year]"
KR:  WebSearch: "한국 증시 급등 종목 오늘 거래량"
KR:  WebSearch: "코스피 코스닥 주목 종목 [current month] [current year]"
```

Pick stocks that appear in ≥2 results, with selection bias toward:
- Volume surge or news frequency
- Market cap ≥ $2B (US) or ≥ 1T KRW (KR) — avoid micro-cap noise
- No earnings event within ±2 days (handled later as a hard penalty if present)

For Korean names returned, resolve to ticker codes via `bin/stock-cli search "<name>" --market KR` before Step 3.

In **single-ticker** and **batch** modes, skip Step 2. In **ALL** mode, run discovery for both markets in sequence.

### Step 3 — Bulk technical metrics (1 call per market)

```bash
bin/stock-cli horizon-metrics-batch NVDA,AMD,AVGO,MSFT,AAPL --market US --days 400
bin/stock-cli fundamentals-batch    NVDA,AMD,AVGO,MSFT,AAPL --market US

bin/stock-cli horizon-metrics-batch 005930,000660,035420,005380,051910 --market KR --days 400
bin/stock-cli fundamentals-batch    005930,000660,035420,005380,051910 --market KR
```

Each `horizon-metrics-batch` row gives you `ma20, ma50, ma200, rsi14, return_1w, return_1m, return_6m, return_1y, pct_from_52w_high, pct_from_52w_low, max_drawdown_1y, cycle_risk_flag, vol_ratio, overextension_level`. Use these directly — do not recompute. `overextension_level` (NONE/ELEVATED/EXTREME) drives RULE R2.

### Step 4 — News + disclosure fetch (per ticker)

```bash
# US: Finnhub primary + Alpha Vantage sentiment merge + FMP/yfinance fallbacks
bin/stock-cli news NVDA --market US --limit 5 --since-days 7

# KR: Naver Finance scrape + Open DART regulatory disclosures
bin/stock-cli news 005930        --market KR --limit 5 --since-days 7
bin/stock-cli disclosure 005930              --since-days 7

# Catalyst event gate (R3) — one call per market, all candidates at once.
# Returns per-ticker earnings cap/trim (US) + market-wide macro_trim (US+KR).
bin/stock-cli catalyst gate NVDA,AMD,AVGO,MSFT,AAPL --market US
bin/stock-cli catalyst gate 005930,000660,035420,005380,051910 --market KR
```

Each `news` call returns a `generated_at` timestamp + items with optional `sentiment_score` (US Alpha Vantage only) and `sentiment_label`. Disclosures (`KR` only) include `report_nm` to scan for material flags (감자 / 유상증자 / 관리종목 / 거래정지).

The `catalyst gate` call returns `by_ticker[SYM]` = `{cap_label, confidence_trim, next_earnings_date, trading_days_until}` plus a market-wide `macro_trim`, `gate_unavailable`, and the availability fields `earnings_source` / `macro_available` / `notes`. Fetch it **once per market** (it fetches the FMP earnings + macro windows once and iterates all tickers in memory; a failed FMP earnings fetch falls back to keyless per-ticker yfinance earnings dates — `earnings_source` names the feed used). This is the input to RULE R3 (Step 7) and the ALGO 'Earnings event' row (Step 5). If `gate_unavailable` is true (no source produced data), treat every cap/trim as zero and proceed — the gate is fail-open by design. On a partial outage the gate stays live: `macro_available: false` means only `macro_trim` is unavailable (per-ticker earnings caps still apply); check `notes` for what failed.

If Finnhub returns nothing and the fallback chain produces an empty list, treat news_score's sentiment + volume components as 0 — do not invent.

### Step 5 — Compute the algorithmic score (deterministic point table)

**ALGO_SCORE — sums to a max of +7.0, floor -3.0 (-4.0 with the earnings penalty).** Buckets within each component are mutually exclusive — evaluate top-to-bottom and stop at the first match (`if/elif`):

| Component | Bucket (evaluate in order) | Points | Notes |
|---|---|---|---|
| **Trend** | MA20 > MA50 > MA200 (full bull stack) | +3.0 | |
| | MA20 > MA50 *and* MA50 ≤ MA200 | +1.0 | |
| | MA50 < MA200 *and* MA20 ≤ MA50 (full bear stack) | -1.0 | |
| | otherwise (mixed) | 0 | |
| **Momentum** | RSI14 ∈ [50, 70] | +1.0 | down-weighted 2026-07: momentum signal 24.6% 30d win |
| | RSI14 ∈ [30, 50) | +0.5 | |
| | RSI14 > 70 | +0.5 | overbought, half credit |
| | RSI14 < 30 | -0.5 | |
| **Return 1M** | `return_1m` ≥ +0.05 (i.e. ≥ +5%) | +1.5 | |
| | 0 < `return_1m` < +0.05 | +0.5 | |
| | -0.10 < `return_1m` ≤ 0 | 0 | |
| | `return_1m` ≤ -0.10 | -0.5 | |
| **Volume** | `vol_ratio` > 1.3 (from horizon-metrics-batch; 5d avg / 50d avg) | +0.5 | down-weighted 2026-07: volume signal 23.3% 30d win |
| | `vol_ratio` is None or ≤ 1.3 | 0 | None happens on listings with < 50 bars or all-zero windows |
| **Cycle** | `pct_from_52w_high` ≥ -0.10 | +1.0 | within 10% of 52-week high |
| | `max_drawdown_1y` ≤ -0.25 | -1.0 | |
| | else | 0 | |
| **Earnings event** *(penalty only)* | next earnings within 7 days | -1.0 | fed by RULE R3 — apply -1.0 when `catalyst gate` reports the ticker's `trading_days_until ≤ 5` (cap or trim band). R3 then also caps/trims per Step 7. |
| | else | 0 | |

Max positive sum: 3 + 1.0 + 1.5 + 0.5 + 1 + 0 = **7.0**. Max negative drag: -1 + -0.5 + -0.5 + -1 = **-3.0**. Effective floor with earnings event also: **-4.0**.

> **2026-07 down-weight rationale**: 30-day weekly calibration (2026-07-05) grades
> momentum (24.6%) and volume (23.3%) statistically dead, while sector (52.6%) and
> fundamental (67.5%) are the only signals beating coin-flip. The ALGO ceiling drops
> 8.0 → 7.0, so BUY (COMPOSITE ≥ 8.0) now always requires at least +1.0 of combined
> news/LLM-context confirmation — mechanics alone can no longer print a BUY.

When a `horizon-metrics` field is `null` (insufficient bars on a young listing), assign 0 to that component and append `(N/A: <field>)` to the transmission chain's RISK slot.

### Step 5b — LLM macro/narrative context score (mitigates momentum bias)

The deterministic ALGO_SCORE rewards stocks that are already trending (full bull MA stack, RSI 50-70, return_1m ≥ +5%, near 52W high). This is intentional (momentum factor) but creates a structural bias: **a parabolic blow-off top scores identically to a healthy mid-stage uptrend**. The 5/15/2026 KOSPI key reversal day exposed this — 000660 SK하이닉스 would have scored ALGO 7.0 + NEWS 1.0 = BUY threshold under the pre-2026-07 weights despite +925% YoY and a confirmed macro top signal.

`LLM_CONTEXT_SCORE` is your explicit channel to encode macro/narrative context the table can't see. **Range -5.0 to +3.0** (asymmetric — bigger negative range mitigates the algorithmic momentum bias which dominates upside).

**When to evaluate**: Always compute, but spend more effort when `ALGO_SCORE >= 6.0` (i.e., the stock would otherwise be WATCH+ on raw mechanics). For `ALGO_SCORE < 6.0`, default to 0 unless something specific is obvious.

**Inputs to consider** (gather before scoring):

1. **Macro detector outputs** (call when `ALGO_SCORE >= 6.0`):
   - `market-top-detector` — distribution days, defensive rotation, leadership breakdown
   - `ftd-detector` — rally attempt / FTD confirmed / post-FTD health
   - `macro-regime-detector` — Concentration / Broadening / Contraction / Inflationary / Transitional
2. **Sector lifecycle** (`theme-detector` or web search):
   - Early-stage breakout / mid-stage run / late-stage parabolic / decay
3. **Recent narrative themes** from news + web search:
   - Foreign capital flow direction (KR: 외인 순매수/매도 trend)
   - FX context (USD/KRW > 1500 = capital outflow stress)
   - Supply chain disruptions (strikes, geopolitics)
   - Regulatory / antitrust
   - Earnings season binary events nearby
4. **Cross-asset confirmation**: bonds, related sectors, SOXL vs SPY divergence

**Bull / Bear / Judge — commit a thesis before scoring** (staged reasoning, adapted from AutoHedge's Director→Quant→Risk + a Fincept-style adversarial debate). The deterministic ALGO/NEWS tables are the *Quant* stage (already computed). This is the *Director/Judge* stage; the C2/C3 gates in Step 9 are the *Risk* stage. Run these four micro-steps in order and do not skip ahead:

1. **Director thesis (commit first).** Before building either case, write ONE line: your directional lean (BULL / BEAR / NEUTRAL) and the single biggest risk to it, derived only from the inputs above. Committing first reduces motivated reasoning — the debate must then test this thesis, not rationalize the ALGO score.
2. **Bull case.** The 2-3 strongest *specific* reasons price goes up (cite numbers: MA stack, return_1m, FTD status, sector stage, sentiment). No vague adjectives.
3. **Bear case.** The 2-3 strongest *specific* reasons price goes down — genuinely adversarial, not a strawman. Pull from macro top signals, late-stage sector, FX/flow stress, event risk, valuation. If you cannot write a real bear case, say so explicitly.
4. **Judge.** Net the two sides into `llm_context_score` using the rubric below. State which side won and why in `llm_context_reasoning`, and **carry the surviving bear point into the reasoning text** so it is preserved for the postmortem. If the bear case materially contradicts the Director thesis, the thesis loses — do not anchor on it.

This debate *produces* `LLM_CONTEXT_SCORE`; it does not add a second override channel. The bounded -5.0..+3.0 range and the composite math are unchanged — a more rigorous score is the only output.

**Rigor gate (deterministic).** Structure the debate as JSON and lint it before
trusting a strong score:

```bash
bin/stock-cli lint-llm-context '{"score":-3.0,"winner":"bear",
  "bull_points":[{"claim":"...","evidence":"return_1m=+7%","signal_type":"technical"}],
  "bear_points":[{"claim":"KOSPI parabolic","evidence":"+25%/22d","signal_type":"macro"}]}'
```

The linter enforces: score ∈ [-5,+3]; `winner` agrees with the score's sign; a
real (non-empty) `bear_points` always present; and when **|score| ≥ 2.0** the
winning side has ≥1 point with non-empty `evidence` and a `signal_type` in
{macro, sector, event, flow, valuation, technical, narrative}. If the lint is
not clean, **dampen the score toward 0** until it is — a strong score with no
cited, typed evidence is exactly the noise that made `llm_context` underperform.
Pass the final score into Step 9's `--components` so its marginal value is
measured (`component-contribution`).

**Scoring rubric** (anchors — pick the nearest):

| Score | Anchor | Example |
|---|---|---|
| **+3.0** | Major macro tailwind aligned with this stock | FTD confirmed last week + sector early breakout + positive narrative |
| **+1.5** | Moderate tailwind | Sector in mid-stage uptrend, no top signal, supportive macro regime |
| **+0.5** | Mild tailwind | Favorable macro regime, neutral sector |
| **0** | Neutral / no specific context | LLM has no strong reason to adjust either way |
| **-1.5** | Mild headwind | Late-stage sector, neutral macro |
| **-3.0** | Strong headwind | Macro top signal + late-stage sector + specific event risk (e.g., earnings pre-binary) |
| **-5.0** | Crisis | Confirmed bear market entry + sector blow-off + fundamentals deteriorating |

Interpolate between anchors (e.g., -2.5 is allowed and common).

**Required output** — emit both:

```json
"llm_context_score": -3.0,
"llm_context_reasoning": "KOSPI 5/15 key reversal day -6.12% off ATH 8047 + 외인 5.56조 매도 + USD/KRW 1500 돌파 = 매크로 톱 확인. 반도체 섹터 late-stage parabolic (+25% / 22d). 삼성 노조 파업 5/21 임박. 펀더 자체는 견고 (NVDA Blackwell 수요)이므로 -5는 아님."
```

**Reasoning text rules** (apply to ALL non-zero scores):
- Quote specific numbers, not adjectives ("외인 -5.56조" not "외인 매도 심함")
- Reference at least one **named** macro signal source ("market-top-detector triggered defensive rotation", "ftd-detector still in rally attempt", "USD/KRW 1500 stress", "distribution day count = 4")
- Explain why this anchor (and not adjacent ones) — e.g., "펀더는 견고 (NVDA Blackwell 수요)이므로 -5는 아님"
- Korean OK for KR market analysis, English for US
- When `LLM_CONTEXT_SCORE == 0.0`, emit reasoning text anyway ("Neutral — no specific macro/narrative context.") and **do NOT** include `llm_context` in `signals_used` for the horizon predictions (it would pollute calibration)
- Vague reasoning at `|score| >= 2.0` is a Quality Gate failure (Step 11 Rule 5) — dampen the score toward 0 if you cannot cite a concrete signal

**Calibration honesty**:
- Track record shows valuation/cycle/mean_reversion signals at 0% win rate. LLM_CONTEXT_SCORE is your channel to encode these qualitative signals more carefully. Weekly calibration will measure this signal separately under `--signals llm_context` — be conservative until 6-8 weeks of data confirms it adds alpha.
- When ALGO_SCORE is high (+6 or +7), be wary of confirming with a positive LLM_CONTEXT_SCORE (avoid double-counting bullish momentum). Most algorithmic BUY candidates should get LLM_CONTEXT in [-2.0, +1.0] range.
- **Signal re-weighting (2026-07)**: 30-day calibration shows sector (52.6%) and fundamental (67.5%) are the only signals beating coin-flip; technical/momentum/news/volume/cross_market grade dead. Ground positive LLM_CONTEXT scores in **sector-RS confirmation or a concrete fundamental catalyst** — never award positive context on technical/momentum grounds alone (those are already in ALGO and empirically dead).

**Recursion guard**: this skill can call `market-top-detector`, `ftd-detector`, `macro-regime-detector`, `theme-detector`. None of those currently call `/expect`. If that changes, add a `--no-gate-recursion` flag to suppress the macro context call.

### Step 6 — Compute the news score (deterministic point table)

**NEWS_SCORE — sums to a max of +3.0, can be hard-capped negative.** Read the
`signal` block in the `news` output (deduped, recency-weighted) — do **not**
re-derive these from raw `items`. Sentiment buckets are mutually exclusive and
use `signal.recency_weighted_sentiment` (fresh headlines dominate a week-old
blip); if it is null (no AV score — KR or US without key), Sentiment = 0.

| Component | Bucket (evaluate in order) | Points |
|---|---|---|
| **Sentiment** (`signal.recency_weighted_sentiment` available) | > +0.15 | +2.0 |
| | 0 < rw ≤ +0.15 | +1.0 |
| | -0.15 ≤ rw ≤ 0 | -1.0 |
| | rw < -0.15 | -2.0 |
| **Sentiment** (null) | n/a | 0 |
| **Headline volume** | `signal.unique_count` ≥ 3 (deduped) | +1.0 |
| | else | 0 |

Then apply hard caps **after** summing the above:

- **Negative catalyst.** If `signal.has_negative_catalyst` is true (the module
  scans for bankrupt/fraud/lawsuit/downgrade/investigation/recall/delist/guidance
  cut/etc.) → set `NEWS_SCORE = min(NEWS_SCORE, -2)`.
- **KR disclosure flag.** If any disclosure `report_nm` contains: 감자, 유상증자, 관리종목, 거래정지, 상장폐지 → set `NEWS_SCORE = min(NEWS_SCORE, -2)`.

Hard caps override even strongly positive sentiment — that is the whole point of the cap. Pass `signal.event_tags` into Step 5b: a hard catalyst (earnings/guidance/M&A/regulatory) is exactly the kind of specific, dated fact the LLM_CONTEXT bear/bull debate should weigh.

Max positive sum: 2 + 1 = **3.0**. Floor without hard caps: -2 + 0 = -2.0. With a hard cap firing: **-2.0** (caps clamp, they don't stack).

### Step 7 — Composite + decision label

```
COMPOSITE = ALGO_SCORE + NEWS_SCORE + LLM_CONTEXT_SCORE
  range:  -11.0 (worst case)  ..  +13.0 (perfect)
  (algo floor -4.0 with earnings penalty + news floor -2.0 + llm_context floor -5.0)

Label mapping (contiguous half-open ranges — every score lands in exactly one bucket):
  COMPOSITE >= 8.0          →  BUY     high-conviction long
  6.0 <= COMPOSITE < 8.0    →  WATCH   wait for pullback or confirmation
  3.0 <= COMPOSITE < 6.0    →  HOLD    no action; keep if already held
  0.0 <= COMPOSITE < 3.0    →  AVOID   do not initiate
  COMPOSITE < 0.0           →  SELL    exit if held; never short
```

Round COMPOSITE to one decimal in the output.

**RULE R1 — Hard regime gate (applies to BULL labels/horizons before logging).**
Read the `regime` verdict (Step 1) for the candidate's market and apply:
- **RISK_OFF** (`label == "RISK_OFF"`): do **not** issue new BUY or log BULL
  horizons. Cap the label at WATCH and resolve would-be BULL horizons to
  NEUTRAL. Emit "⚠️ REGIME RISK_OFF — new longs gated" in the per-stock detail.
- **NEUTRAL**: raise the BUY bar from 8.0 to **9.0** (a NEUTRAL regime demands a
  stronger setup) and trim logged confidence one extra step (cap at 0.60). Emit
  "⚠️ REGIME NEUTRAL — higher bar".
- **RISK_ON**: no change.

Rationale: backfilled over the June 2026 drawdown, BULL calls issued while the
worse-of-SPY/QQQ (US) or KODEX 200 (KR) regime was NEUTRAL/RISK_OFF won only
~3%; the gate suppresses or hardens exactly those. It is a market-level gate —
per-stock overextension is RULE R2. The gate does **not** raise the bar for
BEAR/NEUTRAL calls (those already clear RULE C3).

**RULE R2 — Per-stock overextension gate (parabolic/blow-off entries).** Read
`overextension_level` from the candidate's `horizon-metrics` row:
- **EXTREME**: do **not** issue BUY or log BULL horizons — cap the label at
  WATCH, resolve would-be BULL horizons to NEUTRAL. Emit "⚠️ OVEREXTENDED
  (EXTREME)".
- **ELEVATED**: raise the BUY bar by +1.0 (BUY needs COMPOSITE ≥ 9.0) and trim
  logged confidence one step (cap 0.60). Emit "⚠️ OVEREXTENDED (ELEVATED)".
- **NONE**: no change.

Rationale: on 377 closed BULL, entries with RSI14 > 75 won 26% (vs 47% at
RSI < 60) and entries > 15% above MA20 won 30% (vs 66% in the 3–8% band). A
blow-off entry is a strong anti-signal that the broad regime gate (R1) cannot
see.

**RULE R3 — Event-risk gate (imminent binary catalysts).** Read the `catalyst
gate` output (Step 4) for the candidate's market. R3 is a risk gate only — it
never raises the BUY bar and never issues a BUY.
- **Earnings (per-ticker, US only).** From `by_ticker[SYM]`:
  - `cap_label == "WATCH"` (next earnings `trading_days_until ≤ 2`): cap the
    label at WATCH, resolve would-be BULL horizons to NEUTRAL. Emit "⚠️ EARNINGS
    in N td — entry gated". Don't hold a directional thesis through a coin-flip
    gap.
  - `confidence_trim == 0.05` (`2 < td ≤ 5`): trim logged confidence by 0.05.
    Emit "⚠️ earnings in N td — confidence trimmed".
- **Macro (market-wide, US + KR).** If `macro_trim == 0.05` (a High-impact
  FOMC/CPI/NFP within 2 td), trim **every** pick in that market by 0.05 and emit
  "⚠️ macro event (<name>) in N td". **KR consumes the US macro stream** (it
  transmits via FX / SOXL); KR never gets a per-ticker earnings cap (no forward
  KR EPS feed).
- **Macro risk-off switch (market-wide, US + KR).** Run `bin/stock-cli
  macro-news` once per run and read `risk.risk_level` — a deterministic keyword
  tripwire over global macro/geopolitical headlines (war/invasion, oil supply
  shock, emergency central-bank action, market crash / circuit-breaker /
  sovereign default, tariff/sanctions escalation).
  - `RISK_OFF`: log **no** new BULL predictions this run — cap every label at
    WATCH, resolve would-be BULL horizons to NEUTRAL. Emit "⚠️ MACRO RISK_OFF"
    citing the matched evidence headlines.
  - `ELEVATED`: trim **every** pick's logged confidence by an additional 0.05
    (stacks with the R3 earnings/macro trims above). Emit "⚠️ MACRO ELEVATED".
  - Fail-open: if the fetch errors or no headlines are available, the output
    says NORMAL with a note — treat as NORMAL.
- **Fail-open, visibly.** If `gate_unavailable` is true (no source produced
  data), treat all caps/trims as zero. If only `macro_available` is false
  (partial outage — e.g. the FMP economic calendar 402s), only `macro_trim` is
  unavailable; per-ticker earnings caps still apply (`earnings_source` names
  the feed used — "fmp" or the keyless "yfinance" fallback; `notes` says what
  failed).

Rationale: the deterministic ALGO/NEWS/LLM_CONTEXT score cannot see dated binary
events. A BUY logged the day before the report or the Fed gaps on the print, not
the thesis — exactly the entries R3 caps or trims.

**R1 + R2 + R3 stacking (explicit):** apply all three gates cumulatively.
- A WATCH cap from *any* rule wins: if R1 is RISK_OFF **or** R2 is EXTREME **or**
  R3 earnings is WATCH, the label is WATCH (no BULL logged), full stop.
- The BUY-bar raises are **additive** and come **only from R1/R2**: base 8.0,
  +1.0 if R1 NEUTRAL, +1.0 if R2 ELEVATED → e.g. NEUTRAL regime **and** ELEVATED
  stock requires COMPOSITE ≥ 10.0. **R3 never raises the bar.**
- Confidence is pulled **down** by the strictest of all applicable caps/trims:
  take the **minimum** of the R1/R2 caps (each 0.60) and then subtract the R3
  earnings trim (−0.05) and the R3 macro trim (−0.05) that apply to the pick
  (a pick can eat both R3 trims at once).

Threshold rationale: BUY at 8.0 with the ALGO ceiling at 7.0 (2026-07 down-weight) means even a **perfect** algorithmic setup needs at least **+1.0 of combined** news/LLM-context confirmation (e.g. NEWS ≥ +1 or LLM_CONTEXT ≥ +1) — and a strongly bearish LLM_CONTEXT (-3) is sufficient on its own to downgrade an otherwise-BUY signal to HOLD. This is the explicit anti-momentum-bias circuit.

**Worked example — 000660 SK하이닉스 on 2026-05-14 (pre-crash)**:
- ALGO = 6.5 (trend +3, momentum +0.5, return_1m +1.5, volume +0.5, cycle +1.0; 2026-07 weights)
- NEWS = 1.0 (5 headlines/7d, no AV sentiment, no hard cap)
- LLM_CONTEXT = -3.0 (KOSPI parabolic +25% / 22d, magazine cover "1만피" target, FX 1500 stress, sector late-stage)
- COMPOSITE = **4.5 → HOLD** (macro context overrides an otherwise WATCH-adjacent setup)

Under the pre-2026-07 weights the system would have labelled this BUY at the very top of a blow-off without LLM_CONTEXT; with the down-weighted table it sits at WATCH (7.5) on mechanics alone, and the context score pushes it firmly to HOLD — two independent layers now block the same failure mode.

### Step 8 — Transmission chain (exactly 3 facts)

For every BUY / WATCH / SELL stock, emit a transmission chain — three lines, one each from technical, news/fundamental, and risk slots:

```
TECH:  RSI14=62 above midline; MA20 > MA50 > MA200 stack
NEWS:  Finnhub avg sentiment +0.21 across 5 articles in last 7d
RISK:  earnings 18d out — no event risk; cycle_risk_flag=False
```

Format rules:
- **Specific numbers, not adjectives.** "RSI14=62" not "RSI is bullish."
- One line per slot. If a slot has no relevant fact, write "—" rather than padding.
- Quote the data field name when relevant (`return_1m=+7.4%`) so a future weekly aggregator can parse it.
- For HOLD / AVOID labels, the transmission chain is optional but recommended.
- The 3 transmission chain slots (tech/news/risk) are **separate from** `llm_context_reasoning` — do NOT merge the macro narrative into the RISK slot. RISK should cover stock-specific risks (earnings event, cycle_risk_flag, drawdown); macro/sector lifecycle lives in `llm_context_reasoning` only.

### Step 9 — Multi-horizon predictions (existing logic, preserved)

Independent of the composite label, produce 4 horizon-level direction calls per stock so the prediction DB tracks calibration over time:

| Horizon | Timeframe | Inputs | Direction logic |
|---|---|---|---|
| Short | 1W | RSI14, MA20 position, `return_1w`, news sentiment | Momentum |
| Medium | 1M | MA50 position, `return_1m`, P/E, upcoming earnings | Trend |
| Long | 6M | MA200 position, `return_6m`, sector | Trend confirmation |
| Cycle | 1Y | `return_1y`, `pct_from_52w_high`, `max_drawdown_1y`, valuation | Mean-reversion (analysis-only — never logged; RULE C4) |

Direction = BULL / BEAR / NEUTRAL. Confidence ∈ [0.50, 0.85] using the 4-signal-alignment rule (4 aligned → 0.75-0.85, 3 → 0.60-0.74, 2 → 0.50-0.59, mixed → 0.50 NEUTRAL).

**Conflict gate (RULE C1 — Cycle vs Short):** If `cycle.direction == BEAR` and `short.direction == BULL`, cap composite confidence at 0.60 and emit "⚠️ CYCLE RISK" in the per-stock detail.

**Risk/edge gate (RULE C2 — reward:risk floor):** Before saving any directional horizon, compute reward:risk from the entry/target/stop you would log: `reward = |target − entry|`, `risk = |entry − stop|`. If `reward / risk < 1.5`, the edge is too thin — **do not log a directional row**; downgrade that horizon to NEUTRAL (which is not saved). This kills low-edge predictions that dilute the track record.

**BEAR edge bar (RULE C3 — higher bar for downside calls):** Our measured BEAR hit rate is ~6% vs ~61% for BULL — the system cannot reliably forecast declines. Therefore a BEAR horizon may only be logged when **all** of: (a) ≥3-signal alignment, (b) macro/cycle confirmation present (`LLM_CONTEXT_SCORE ≤ −2.0` **or** `cycle_risk_flag == True`), and (c) reward:risk ≥ 2.0. Otherwise emit NEUTRAL instead of BEAR. Note: under `--source LIVE` the prediction store **hard-rejects BEAR rows** (they error out), so for scheduler/cron runs always resolve a would-be BEAR to NEUTRAL rather than attempting to save it.

**1Y logging gate (RULE C4 — Cycle horizon is analysis-only):** Our measured LIVE 1Y track record is 0/12 hits with average outcome return −23.8% (vs 6M −7.1%, 1M −3.3%, 1W −0.8%) — the system cannot forecast a year out. Still compute the Cycle (1Y) direction (it feeds RULE C1 and the per-stock narrative), but **never save a 1Y prediction row** — cap the longest logged horizon at 6M. Note: the prediction store **hard-rejects LIVE 1Y rows** (they error out); INTERACTIVE remains a deliberate manual override.

Save each horizon ≥ 0.60 confidence (after RULE C2/C3/C4) as a separate prediction row, all sharing the same `--analysis-group-id` UUID per stock. **Pass your raw confidence and add `--recalibrate`** — the CLI maps it through the isotonic recalibration curve deterministically before storing (e.g. raw 0.62 in an overconfident band → logged ~0.50), so logged confidence reflects observed accuracy rather than a near-constant ~0.6. Do **not** hand-apply the map yourself; the flag does it consistently (and is a safe no-op until ≥30 closed predictions of that source exist):

```bash
GROUP_ID=$(uv run python -c "import uuid; print(uuid.uuid4())")

bin/stock-cli predict create \
  --ticker NVDA --market US --direction BULL \
  --confidence 0.72 --timeframe 1W \
  --entry-price 130.50 --target-price 138 --stop-price 125 \
  --reasoning "RSI 62, MA20+7.3%, Finnhub sentiment +0.21, no earnings risk" \
  --signals technical,news,momentum \
  --components '{"algo":7.0,"news":1.0,"llm_context":-1.5,"overextension":"NONE","regime":"RISK_ON","news_signal":{"unique_count":4,"mean_sentiment":0.15,"recency_weighted_sentiment":0.21,"event_tags":["earnings"],"has_positive_catalyst":false,"has_negative_catalyst":false}}' \
  --recalibrate \
  --analysis-group-id "$GROUP_ID"
```

Always pass `--components` with the per-pillar contributions for this call
(`algo` = ALGO_SCORE, `news` = NEWS_SCORE, `llm_context` = LLM_CONTEXT_SCORE,
`overextension` = the R2 level, `regime` = the R1 label). It is stored for
capability-contribution analysis (`bin/stock-cli component-contribution`) and a
future blended confidence — so the three capabilities can be measured separately.

**Also include `news_signal`** — from the `signal` block of the Step 4 `news`
output, copy exactly these six keys (unchanged values) into `--components` as
`news_signal`: `unique_count`, `mean_sentiment`, `recency_weighted_sentiment`,
`event_tags`, `has_positive_catalyst`, `has_negative_catalyst`. Omit every
other key (`raw_count`, etc.). The
collapsed NEWS_SCORE is graded near-dead; persisting the raw fields lets
`bin/stock-cli news-tag-performance` learn which catalyst tags actually predict.

The JSON output echoes `raw_confidence` and `recalibration_applied` so you can confirm the transform. `--signals` should be a comma-separated subset of: `technical`, `news`, `fundamental`, `momentum`, `volume`, `disclosure`, `llm_context`. **Do not record `cycle`, `valuation`, or `mean_reversion`** — these are graded statistically dead (0% hit rate) and only pollute per-signal calibration; route any such qualitative read through `LLM_CONTEXT_SCORE` instead. Include `llm_context` whenever `LLM_CONTEXT_SCORE` is non-zero so the weekly calibration aggregator can isolate its contribution to forecast accuracy. The weekly calibration aggregator (Stage 6) decomposes these to find which signals over- or under-perform.

Generate a fresh `GROUP_ID` for each stock — never reuse across tickers.

### Step 10 — Outcome telemetry sidecar (mandatory)

After the run completes (all stocks scored, all predictions saved), write `state/last-outcome-expect.json`:

```json
{
  "run_id": "<UUID>",
  "generated_at": "2026-05-10T19:42:11+09:00",
  "mode": "ALL",
  "markets": ["US", "KR"],
  "track_record_snapshot": {
    "win_rate_30d": 0.58,
    "brier_30d": 0.21,
    "overconfident_buckets": ["0.70-0.80"]
  },
  "picks": [
    {
      "ticker": "NVDA",
      "market": "US",
      "label": "BUY",
      "composite": 10.0,
      "algo_score": 7.0,
      "algo_components": {"trend": 3.0, "momentum": 1.0, "return_1m": 1.5, "volume": 0.5, "cycle": 1.0, "earnings": 0},
      "news_score": 3.0,
      "news_components": {"sentiment": 2.0, "headline_volume": 1.0, "neg_keyword_cap": false, "disclosure_cap": false},
      "llm_context_score": 0.0,
      "llm_context_reasoning": "Neutral macro: FTD post-confirmation health intact, AI sector mid-stage, no specific event risk. Algo + news already capture the bull case so no further uplift warranted.",
      "transmission_chain": {
        "tech": "RSI14=62 above midline; MA20>MA50>MA200 stack",
        "news": "Finnhub avg sentiment +0.21 across 5 articles in last 7d",
        "risk": "earnings 18d out — no event risk; cycle_risk_flag=False"
      },
      "horizons_logged": ["1W", "1M"],
      "analysis_group_id": "<UUID>"
    }
  ]
}
```

Use Bash + `jq` (or a small Python heredoc) to construct it. The sidecar is the input to Stage 6's weekly calibration — every component score is captured so we can attribute drift later.

**Field names are part of the contract — use them exactly.** Stage 6 (and any future aggregator) parses these JSON keys by name, so the freeform tendency to shorten `algo_score`/`news_score` to `algo`/`news`, or `horizons_logged` to `horizons`, will silently break the downstream consumer. The schema is:

| Top-level | Type | Notes |
|---|---|---|
| `run_id` | str (UUID) | one per `/expect` invocation |
| `generated_at` | ISO-8601 with offset | e.g. `"2026-05-11T10:42:11+09:00"` |
| `mode` | `"US"` / `"KR"` / `"ALL"` / `"single"` / `"batch"` | matches invocation form |
| `markets` | list of `"US"` / `"KR"` | which markets were scanned |
| `track_record_snapshot` | object | see example above |
| `picks` | list of pick objects | one per ticker (BUY/WATCH/HOLD/AVOID/SELL all included) |

Each pick object:

| Field | Type | Required? |
|---|---|---|
| `ticker` | str | yes |
| `market` | `"US"` / `"KR"` | yes |
| `label` | `"BUY"` / `"WATCH"` / `"HOLD"` / `"AVOID"` / `"SELL"` | yes |
| `composite` | float, 1 decimal | yes |
| `algo_score` | float | yes — NOT `algo` |
| `news_score` | float | yes — NOT `news` |
| `algo_components` | object with `trend`, `momentum`, `return_1m`, `volume`, `cycle`, `earnings` | yes |
| `news_components` | object with `sentiment`, `headline_volume`, `neg_keyword_cap`, `disclosure_cap` | yes |
| `llm_context_score` | float in [-5.0, +3.0], 1 decimal | yes — NOT `llm_score` or `context_score` |
| `llm_context_reasoning` | str, 1-3 sentences citing macro/sector/narrative signals | yes (use "Neutral; no specific context." when score is 0) |
| `transmission_chain` | object with `tech`, `news`, `risk` | yes for BUY/WATCH/SELL; optional for HOLD/AVOID |
| `horizons_logged` | list of `"1W"`/`"1M"`/`"6M"` (`"1Y"` is never logged — RULE C4) | yes (empty list if none ≥ 0.60 conf) |
| `analysis_group_id` | UUID str or null | yes — null when no horizons were logged |

Before writing the file, mental-check the keys against this table. If you used `algo` instead of `algo_score`, fix it before serialising — downstream parsers don't gracefully degrade.

### Step 11 — Quality gate (run before output)

Before printing the final markdown, sanity-check each pick. If any check fails, fix the inconsistency and re-derive — do not paper over:

1. **Sign agreement:** label = BUY/WATCH ⇒ composite ≥ 6; label = SELL/AVOID ⇒ composite ≤ 2. Inconsistencies indicate a scoring bug.
2. **Target separation:** if a horizon prediction is logged, `target_price` and `stop_price` differ from `entry_price` by at least one ATR-equivalent (use `max_drawdown_1y / 12` as a rough proxy if ATR not available).
3. **Transmission chain hygiene:** each slot quotes a specific number or named fact. No bare adjectives.
4. **Fresh-data check:** every `news` and `horizon-metrics-batch` `generated_at` is within 1 hour of now. If older, re-fetch.
5. **LLM_CONTEXT_SCORE justification:** if `|llm_context_score| >= 2.0`, `llm_context_reasoning` must cite at least one specific macro/sector/narrative signal (named detector output, FX number, sector lifecycle phase, etc.) — no vague "market feels risky" justifications. If reasoning is weak, dampen score toward 0.
6. **Double-counting guard:** if `algo_score >= 6.0` and `llm_context_score >= +2.0`, recheck — you may be double-counting bullish momentum already captured by trend/momentum/cycle algo components. Most algorithmic BUY candidates should get `llm_context_score` in `[-2.0, +1.0]`.

### Step 12 — Bias checklist (informs LLM_CONTEXT_SCORE; final audit)

The 4 retail biases below are inputs into your LLM_CONTEXT_SCORE — they should already be reflected in the score and reasoning text. This step is a final cross-check that the score wasn't infected by these biases:

- **Recency:** am I weighting yesterday's news above the 1Y trend? If yes, my LLM_CONTEXT_SCORE may be over-negative (or over-positive on a 1-day pop). Dampen toward 0.
- **Confirmation:** did I read past contradicting headlines? If only positives went into the reasoning, recheck and rebalance.
- **Anchoring:** is the prediction anchored to the discovery price rather than current price? Cross-check with `entry_price` field.
- **Overconfidence:** is the composite ≥ 10 with weak news + thin volume + minimal LLM_CONTEXT justification? Step down to WATCH.

Note in the per-stock detail if any of these fired and what was adjusted in the LLM_CONTEXT_SCORE.

---

## Output format

```markdown
## [US|KR|ALL] Market Expectation — YYYY-MM-DD

### Headline picks

| # | Ticker | Price | Label | Composite | Algo / News / Context | Horizons logged |
|---|--------|-------|-------|-----------|------------------------|-----------------|
| 1 | NVDA   | $130  | **BUY**   | 10.0 | 7.0 / 3.0 / 0.0 | 1W, 1M |
| 2 | 000660 | ₩1.82M| HOLD  | 4.5 | 6.5 / 1.0 / **-3.0** | — (LLM context downgraded a WATCH-adjacent setup) |
| 3 | XYZ    | $42   | SELL  | -1.5 | -0.5 / -1.0 / 0.0 | — |

Track record: 58% win rate over last 30 days, Brier 0.21.
Overconfidence flag in the 0.70-0.80 bucket — output confidence reduced 5%.

### Per-stock detail

#### 1. NVDA ($130) — BUY (composite 10.0)

**Transmission chain:**
- TECH: RSI14=62 above midline; MA20>MA50>MA200 stack
- NEWS: Finnhub avg sentiment +0.21 across 5 articles in last 7d
- RISK: earnings 18d out — no event risk; cycle_risk_flag=False

**Algo (7.0/7):** Trend +3.0, Momentum +1.0, Return_1M +1.5, Volume +0.5, Cycle +1.0
**News (3.0/3):** Sentiment +2.0, Headline_volume +1.0, no hard caps fired
**LLM Context (0.0):** Neutral macro — FTD post-confirmation health intact, AI sector mid-stage. No specific event risk.

**Horizons:**
- Short (1W) — BULL 0.72 → logged
- Medium (1M) — BULL 0.65 → logged
- Long (6M) — NEUTRAL 0.55 → not logged
- Cycle (1Y) — BULL 0.62 → narrative only (RULE C4: LIVE 1Y store-gated)

**Bias check:** none triggered.

#### 2. 000660 SK하이닉스 (₩1,819,000) — HOLD (composite 4.5, LLM_CONTEXT downgrade)

**Transmission chain:**
- TECH: MA20>MA50>MA200 stack; RSI14=71.9 OVERBOUGHT; return_1m=+64.9%
- NEWS: 5 headlines/7d including "검은 월요일" macro panic; treasury share disposal 5/13
- RISK: cycle_risk_flag=True; parabolic blow-off context; US semis -4% Fri

**Algo (6.5/7):** Trend +3.0, Momentum +0.5, Return_1M +1.5, Volume +0.5, Cycle +1.0
**News (1.0/3):** Headline_volume +1.0
**LLM Context (-3.0):** KOSPI 5/15 key reversal day -6.12% off ATH 8047 + 외인 -5.56조 매도 + USD/KRW 1500 돌파 = market-top-detector defensive. Sector late-stage parabolic (+25% / 22d). Samsung 노조 파업 5/21 imminent. Fundamentals intact (NVDA Blackwell demand, HBM cycle), so -3.0 (not -5.0).

**Under pre-2026-07 weights this printed a raw BUY at the very top** (now WATCH 7.5 on mechanics alone). Anti-momentum-bias circuit fired correctly.

#### 3. MU ($426) — WATCH (composite 7.0, capped by RULE C1)
...
```

After printing, sidecar JSON path is shown to the user:
> `state/last-outcome-expect.json` written.

### Output verbosity by mode

To prevent long outputs in `/expect ALL` (10 stocks × 15 lines = ~150 lines of detail), scale the per-stock detail by mode:

| Mode | Per-stock detail |
|---|---|
| `/expect <single ticker>` | Full block as shown above (transmission chain + components + horizons + bias check). |
| `/expect <comma-list>` (≤ 5 tickers) | Full block. |
| `/expect KR` / `/expect US` (5 each) | Full block. |
| `/expect ALL` (10 stocks) | **Abbreviated block per stock**: 1-line transmission chain (semicolon-joined), 1-line component summary, 1-line horizons summary. Reserve full per-stock detail for any pick that triggered RULE C1 or has a hard-cap news flag. |

In `ALL` mode, also drop AVOID rows below the table (a one-line note per skipped ticker is enough — the sidecar JSON has the details for anyone who wants them).

---

## Important notes

1. **No news available.** If `bin/stock-cli news` returns 0 items and the fallback chain is exhausted, set sentiment + headline-volume components to 0 and flag in the bias check ("Insufficient news — relying on technical analysis only").
2. **KR small caps.** Market cap under 500B KRW → annotate "Liquidity risk" and cap label at WATCH (no BUY).
3. **Earnings within ±2 days.** Cap label at WATCH; the -1 algorithmic penalty is not enough on its own for short horizons.
4. **Duplicate predictions.** If `predict list --status OPEN --ticker <t>` returns a row, do not create a new horizon prediction for that timeframe; reference the existing one in the per-stock detail.
5. **Korean text encoding.** Stock CLI emits UTF-8. If passing through tools that mangle Korean characters, prefer the ticker code over the company name.
6. **Never short.** SELL means "exit if held," never "open short position." This is a long-only system.

---

## Calling skills from inside `/expect` (composition, not absorption)

`/expect` orchestrates other active skills as gates rather than re-implementing their logic. These now feed **into** `LLM_CONTEXT_SCORE` (Step 5b), so the call threshold has moved earlier in the pipeline:

- `market-top-detector` and `ftd-detector`: invoke when `ALGO_SCORE >= 6.0` (i.e., any candidate that would land WATCH+ on mechanics). Their outputs are the primary input to a negative `llm_context_score` when defensive signals fire.
- `macro-regime-detector`: invoke once per `/expect` run (or once per market) and cite the regime explicitly in `llm_context_reasoning` for every pick.
- `theme-detector`: if a stock's primary theme is in late-stage decay, push `llm_context_score` toward -1.5 or worse and note the theme phase.
- `prediction-review`: surface specific tickers with poor recent calibration — adjust their confidence floor (separate from LLM_CONTEXT_SCORE).
- `position-sizer`: link out for actual share-count math; do not output share counts directly.

**Latency budget**: macro detector calls are amortised across all picks in a market scan — call once per `/expect` invocation, reuse the output for each ticker. The goal is `/expect ALL` finishing in under 5 minutes even with macro context enabled.

**Recursion guard:** none of the gate skills above currently call `/expect`. If any of them are extended in future to invoke `/expect`, this composition pattern would loop — track this in a CLAUDE.md note before adding a back-reference, or add a `--no-gate-recursion` flag to suppress the macro-context call.
