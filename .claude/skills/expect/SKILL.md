---
name: expect
description: "Stock expectation analysis. Combines algorithmic technical scoring + structured news/sentiment + multi-horizon directional predictions to emit a deterministic BUY / WATCH / HOLD / AVOID / SELL recommendation per stock. Modes: (1) /expect KR or /expect US auto-discover 5 trending stocks, (2) /expect NVDA or /expect 삼성전자 single-ticker deep dive, (3) /expect NVDA,AMD,AVGO multi-ticker batch, (4) /expect ALL or /expect runs both markets. Triggers: expect, expectation, stock picks, what should I buy, hot stocks, trending stocks, buy or sell, 기대값, 종목 추천, 뭐 사야돼, 매수, 매도, 핫한 종목"
---

# Expect — Multi-Horizon Buy/Sell Recommender

## What this produces

Per stock:
- A deterministic **BUY / WATCH / HOLD / AVOID / SELL** label, derived from a fixed point table
- A **transmission chain** of exactly 3 facts: one technical, one news/fundamental, one risk
- A **composite score** in the range -3 .. +15 with the components broken out
- Independent **multi-horizon predictions** (1W / 1M / 6M / 1Y) logged to `data/predictions.db`
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
```

Surface to yourself:
- Recent overconfidence buckets (e.g. "0.70-0.80 confidence → 45% actual win rate" → cap output confidence at 0.65 for that band)
- Per-signal performance (if `news` signal is hitting <50% lately, weight news_score lower until calibration recovers)
- Open predictions for the same tickers (skip re-prediction; reference the existing one)

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

Each `horizon-metrics-batch` row gives you `ma20, ma50, ma200, rsi14, return_1w, return_1m, return_6m, return_1y, pct_from_52w_high, pct_from_52w_low, max_drawdown_1y, cycle_risk_flag`. Use these directly — do not recompute.

### Step 4 — News + disclosure fetch (per ticker)

```bash
# US: Finnhub primary + Alpha Vantage sentiment merge + FMP/yfinance fallbacks
bin/stock-cli news NVDA --market US --limit 5 --since-days 7

# KR: Naver Finance scrape + Open DART regulatory disclosures
bin/stock-cli news 005930        --market KR --limit 5 --since-days 7
bin/stock-cli disclosure 005930              --since-days 7
```

Each `news` call returns a `generated_at` timestamp + items with optional `sentiment_score` (US Alpha Vantage only) and `sentiment_label`. Disclosures (`KR` only) include `report_nm` to scan for material flags (감자 / 유상증자 / 관리종목 / 거래정지).

If Finnhub returns nothing and the fallback chain produces an empty list, treat news_score's sentiment + volume components as 0 — do not invent.

### Step 5 — Compute the algorithmic score (deterministic point table)

**ALGO_SCORE — sums to a max of +8.0, can go as low as -4.5.** Buckets within each component are mutually exclusive — evaluate top-to-bottom and stop at the first match (`if/elif`):

| Component | Bucket (evaluate in order) | Points | Notes |
|---|---|---|---|
| **Trend** | MA20 > MA50 > MA200 (full bull stack) | +3.0 | |
| | MA20 > MA50 *and* MA50 ≤ MA200 | +1.0 | |
| | MA50 < MA200 *and* MA20 ≤ MA50 (full bear stack) | -1.0 | |
| | otherwise (mixed) | 0 | |
| **Momentum** | RSI14 ∈ [50, 70] | +1.5 | |
| | RSI14 ∈ [30, 50) | +0.5 | |
| | RSI14 > 70 | +0.5 | overbought, half credit |
| | RSI14 < 30 | -0.5 | |
| **Return 1M** | `return_1m` ≥ +0.05 (i.e. ≥ +5%) | +1.5 | |
| | 0 < `return_1m` < +0.05 | +0.5 | |
| | -0.10 < `return_1m` ≤ 0 | 0 | |
| | `return_1m` ≤ -0.10 | -0.5 | |
| **Volume** | `vol_ratio` > 1.3 (from horizon-metrics-batch; 5d avg / 50d avg) | +1.0 | |
| | `vol_ratio` is None or ≤ 1.3 | 0 | None happens on listings with < 50 bars or all-zero windows |
| **Cycle** | `pct_from_52w_high` ≥ -0.10 | +1.0 | within 10% of 52-week high |
| | `max_drawdown_1y` ≤ -0.25 | -1.0 | |
| | else | 0 | |
| **Earnings event** *(penalty only)* | next earnings within 7 days | -1.0 | optional — requires `earnings-calendar` lookup |
| | else | 0 | |

Max positive sum: 3 + 1.5 + 1.5 + 1 + 1 + 0 = **8.0**. Max negative drag: -1 + -0.5 + -0.5 + -1 + -1 = **-4.0**. Effective floor with earnings event also: **-5.0**.

When a `horizon-metrics` field is `null` (insufficient bars on a young listing), assign 0 to that component and append `(N/A: <field>)` to the transmission chain's RISK slot.

### Step 6 — Compute the news score (deterministic point table)

**NEWS_SCORE — sums to a max of +3.0, can be hard-capped negative.** Sentiment buckets are mutually exclusive:

| Component | Bucket (evaluate in order) | Points |
|---|---|---|
| **Sentiment** (US, Alpha Vantage `sentiment_score` available) | average across items > +0.15 | +2.0 |
| | 0 < avg ≤ +0.15 | +1.0 |
| | -0.15 ≤ avg ≤ 0 | -1.0 |
| | avg < -0.15 | -2.0 |
| **Sentiment** (no AV score — KR market or US without key) | n/a | 0 |
| **Headline volume** | ≥ 3 items returned in `--since-days 7` | +1.0 |
| | else | 0 |

Then apply hard caps **after** summing the above:

- **Negative keyword scan.** If any headline contains a case-insensitive match for any of: `bankrupt`, `fraud`, `lawsuit`, `downgrade`, `SEC investigation`, `recall`, `delist` → set `NEWS_SCORE = min(NEWS_SCORE, -2)`.
- **KR disclosure flag.** If any disclosure `report_nm` contains: 감자, 유상증자, 관리종목, 거래정지, 상장폐지 → set `NEWS_SCORE = min(NEWS_SCORE, -2)`.

Hard caps override even strongly positive sentiment — that is the whole point of the cap.

Max positive sum: 2 + 1 = **3.0**. Floor without hard caps: -2 + 0 = -2.0. With a hard cap firing: **-2.0** (caps clamp, they don't stack).

### Step 7 — Composite + decision label

```
COMPOSITE = ALGO_SCORE + NEWS_SCORE
  range:  -7.0 (worst case)  ..  +11.0 (perfect)

Label mapping (contiguous half-open ranges — every score lands in exactly one bucket):
  COMPOSITE >= 8.0          →  BUY     high-conviction long
  6.0 <= COMPOSITE < 8.0    →  WATCH   wait for pullback or confirmation
  3.0 <= COMPOSITE < 6.0    →  HOLD    no action; keep if already held
  0.0 <= COMPOSITE < 3.0    →  AVOID   do not initiate
  COMPOSITE < 0.0           →  SELL    exit if held; never short
```

Round COMPOSITE to one decimal in the output.

Threshold rationale: BUY requires ≥73% of the +11 max, which forces both technicals and news to be confirming. WATCH (≥55%) is for technical strength without news support, or vice versa. The HOLD floor at 3.0 keeps mediocre setups from being labelled AVOID.

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

### Step 9 — Multi-horizon predictions (existing logic, preserved)

Independent of the composite label, produce 4 horizon-level direction calls per stock so the prediction DB tracks calibration over time:

| Horizon | Timeframe | Inputs | Direction logic |
|---|---|---|---|
| Short | 1W | RSI14, MA20 position, `return_1w`, news sentiment | Momentum |
| Medium | 1M | MA50 position, `return_1m`, P/E, upcoming earnings | Trend |
| Long | 6M | MA200 position, `return_6m`, sector | Trend confirmation |
| Cycle | 1Y | `return_1y`, `pct_from_52w_high`, `max_drawdown_1y`, valuation | Mean-reversion |

Direction = BULL / BEAR / NEUTRAL. Confidence ∈ [0.50, 0.85] using the 4-signal-alignment rule (4 aligned → 0.75-0.85, 3 → 0.60-0.74, 2 → 0.50-0.59, mixed → 0.50 NEUTRAL).

**Conflict gate (RULE C1 — Cycle vs Short):** If `cycle.direction == BEAR` and `short.direction == BULL`, cap composite confidence at 0.60 and emit "⚠️ CYCLE RISK" in the per-stock detail.

Save each horizon ≥ 0.60 confidence as a separate prediction row, all sharing the same `--analysis-group-id` UUID per stock:

```bash
GROUP_ID=$(uv run python -c "import uuid; print(uuid.uuid4())")

bin/stock-cli predict create \
  --ticker NVDA --market US --direction BULL \
  --confidence 0.72 --timeframe 1W \
  --entry-price 130.50 --target-price 138 --stop-price 125 \
  --reasoning "RSI 62, MA20+7.3%, Finnhub sentiment +0.21, no earnings risk" \
  --signals technical,news,momentum \
  --analysis-group-id "$GROUP_ID"
```

`--signals` should be a comma-separated subset of: `technical`, `news`, `fundamental`, `momentum`, `volume`, `cycle`, `valuation`, `mean_reversion`, `disclosure`. The weekly calibration aggregator (Stage 6) decomposes these to find which signals over- or under-perform.

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
      "composite": 11.0,
      "algo_score": 8.0,
      "algo_components": {"trend": 3.0, "momentum": 1.5, "return_1m": 1.5, "volume": 1.0, "cycle": 1.0, "earnings": 0},
      "news_score": 3.0,
      "news_components": {"sentiment": 2.0, "headline_volume": 1.0, "neg_keyword_cap": false, "disclosure_cap": false},
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
| `transmission_chain` | object with `tech`, `news`, `risk` | yes for BUY/WATCH/SELL; optional for HOLD/AVOID |
| `horizons_logged` | list of `"1W"`/`"1M"`/`"6M"`/`"1Y"` | yes (empty list if none ≥ 0.60 conf) |
| `analysis_group_id` | UUID str or null | yes — null when no horizons were logged |

Before writing the file, mental-check the keys against this table. If you used `algo` instead of `algo_score`, fix it before serialising — downstream parsers don't gracefully degrade.

### Step 11 — Quality gate (run before output)

Before printing the final markdown, sanity-check each pick. If any check fails, fix the inconsistency and re-derive — do not paper over:

1. **Sign agreement:** label = BUY/WATCH ⇒ composite ≥ 6; label = SELL/AVOID ⇒ composite ≤ 2. Inconsistencies indicate a scoring bug.
2. **Target separation:** if a horizon prediction is logged, `target_price` and `stop_price` differ from `entry_price` by at least one ATR-equivalent (use `max_drawdown_1y / 12` as a rough proxy if ATR not available).
3. **Transmission chain hygiene:** each slot quotes a specific number or named fact. No bare adjectives.
4. **Fresh-data check:** every `news` and `horizon-metrics-batch` `generated_at` is within 1 hour of now. If older, re-fetch.

### Step 12 — Bias checklist (final pass before output)

Briefly self-audit for the 4 retail biases JoelLewis's `finance-psychology` highlights:

- **Recency:** am I weighting yesterday's news above the 1Y trend? If yes, dampen.
- **Confirmation:** did I read past contradicting headlines? If only positives, recheck.
- **Anchoring:** is the prediction anchored to the discovery price rather than current price?
- **Overconfidence:** is the composite ≥ 9 with weak news + thin volume? Step down to WATCH.

Note in the per-stock detail if any of these fired and what was adjusted.

---

## Output format

```markdown
## [US|KR|ALL] Market Expectation — YYYY-MM-DD

### Headline picks

| # | Ticker | Price | Label | Composite | Algo / News | Horizons logged |
|---|--------|-------|-------|-----------|-------------|-----------------|
| 1 | NVDA   | $130  | **BUY**   | 11.0 | 8.0 / 3.0 | 1W, 1M |
| 2 | MU     | $426  | WATCH | 7.0 | 5.0 / 2.0 | 1W (capped by RULE C1) |
| 3 | XYZ    | $42   | SELL  | -1.5 | -0.5 / -1.0 | — |

Track record: 58% win rate over last 30 days, Brier 0.21.
Overconfidence flag in the 0.70-0.80 bucket — output confidence reduced 5%.

### Per-stock detail

#### 1. NVDA ($130) — BUY (composite 11.5)

**Transmission chain:**
- TECH: RSI14=62 above midline; MA20>MA50>MA200 stack
- NEWS: Finnhub avg sentiment +0.21 across 5 articles in last 7d
- RISK: earnings 18d out — no event risk; cycle_risk_flag=False

**Algo (8.0/8):** Trend +3.0, Momentum +1.5, Return_1M +1.5, Volume +1.0, Cycle +1.0
**News (3.0/3):** Sentiment +2.0, Headline_volume +1.0, no hard caps fired

**Horizons:**
- Short (1W) — BULL 0.72 → logged
- Medium (1M) — BULL 0.65 → logged
- Long (6M) — NEUTRAL 0.55 → not logged
- Cycle (1Y) — BULL 0.62 → logged

**Bias check:** none triggered.

#### 2. MU ($426) — WATCH (composite 7.0, capped by RULE C1)
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

`/expect` orchestrates other active skills as gates rather than re-implementing their logic:

- `market-top-detector` and `ftd-detector`: invoke when COMPOSITE ≥ 9 in a market; if either fires a defensive reading, downgrade BUY → WATCH and note in the bias check.
- `macro-regime-detector`: cite the current regime in the bias check ("Concentration regime — top mega-caps overweighted in news").
- `theme-detector`: if a stock's primary theme is in late-stage decay, cap label at WATCH.
- `prediction-review`: surface specific tickers with poor recent calibration — adjust their confidence floor.
- `position-sizer`: link out for actual share-count math; do not output share counts directly.

Do **not** invoke these for every stock — only when COMPOSITE ≥ 8.0 (BUY threshold) or for the macro context print at the top. Composition costs latency; the goal is `/expect ALL` finishing in under 5 minutes.

**Recursion guard:** none of the gate skills above currently call `/expect`. If any of them are extended in future to invoke `/expect`, this composition pattern would loop — track this in a CLAUDE.md note before adding a back-reference, or add a `--no-gate-recursion` flag to suppress the macro-context call.
