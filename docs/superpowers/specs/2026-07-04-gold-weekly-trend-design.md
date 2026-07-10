# Weekly Gold Trend Analysis — Design Spec

**Date:** 2026-07-04
**Author:** Claude (with Doctor Cho)
**Status:** Approved for planning
**Branch:** `feature/gold-weekly-trend`

## Why

Doctor Cho is dollar-cost-averaging into **KRX 금현물** (physical gold, currently holds 2g) and
wants a recurring weekly checkpoint — analogous to the existing stock crontab jobs — that answers
two questions each week:

1. **Is now a reasonable spot to add this week's tranche?** (short-term entry timing)
2. **Is gold structurally biased to rise long-term?** (the "장기적으로 오를 확률" read)

A backing deep-research pass (WGC / IMF / Fed / J.P. Morgan, 2026-07-03) established the factual
macro picture: central-bank buying is structurally elevated (~1,000t/yr since 2022 vs ~500t prior
decade), the worst historical gold bears required Volcker-style punitive real rates (currently
absent), and the "dollar collapse" narrative is overstated (USD reserve share ~58%, flat since
2022). Those findings seed the macro scorecard below.

## What (scope)

A weekly, mostly-deterministic Python scheduler job that computes a KRX-gold trend + macro read and
delivers a Korean verdict (`ACCUMULATE` / `HOLD` / `PAUSE`) to Telegram plus a report file.

**In scope**
- Deterministic technical scoring on KRX gold (via ACE KRX금현물 ETF `411060` as the tradeable proxy).
- Deterministic macro scorecard (4 factors), scores computed by Python rules from a mix of live data
  (FX, real yield, gold price) and a quarterly-refreshed config file (central-bank tonnage, dollar share).
- A single short LLM-generated Korean summary paragraph grounded **only** in the computed numbers.
- Optional personal 2g position P&L line.
- Telegram delivery + report file + 12-week rolling state for week-over-week deltas.

**Out of scope (YAGNI)**
- Writing predictions into `predictions.db` (would pollute the stock track record — gold uses its own
  `state/gold_trend.json`).
- Real-time / intraday alerts (this is a weekly EOD-ish checkpoint).
- Automatic order execution.
- Re-running the deep-research harness weekly (too expensive; structural factors move slowly and live
  in the config file instead).

## How (architecture)

Follows the established `scheduler/` weekly-job pattern (`weekly_calibration.py` is the closest
template): `argparse → fetch → compute → render → deliver → roll state`, with `main() -> int`.

### Components

| Component | Role |
|---|---|
| `scheduler/gold_trend.py` | **NEW.** The weekly job. Orchestration + all scoring functions (kept in one file to match `weekly_calibration.py` convention, but as small, independently-testable pure functions). |
| `data/gold_macro_factors.yaml` | **NEW.** Slow structural macro inputs + scoring weights/thresholds + optional personal position. Seeded from the 2026-07-03 research. Hand-edited quarterly. |
| `tests/test_gold_trend.py` | **NEW.** Unit tests for scoring, config load, position line, state roll, fail-open. |
| `scheduler/crontab.example` | **EDIT.** Add one weekly cron line + header note. |
| Reused | KR provider (`411060` price history), `yfinance` (GLD, `KRW=X`), FRED public CSV (real yield), `scheduler/telegram_sender.send_briefing()`, the `claude -p` subprocess pattern from `daily_briefing.py`. |
| `reports/gold-trend-YYYY-MM-DD.md` | Report artifact (git-ignored like other reports). |
| `state/gold_trend.json` | Rolling last-12-weeks history for deltas + trend. |

### Data sources & fail-open

| Datum | Primary source | Fail-open fallback |
|---|---|---|
| KRX gold OHLCV (~400 sessions for MA200) | KR provider `get_price_history("411060", 400)` | Direct `pykrx.get_market_ohlcv_by_date`; if still empty → derive trend from GLD and flag "KRX proxy unavailable". |
| USD gold context | `yfinance` `GLD` 1y | Skip USD/FX decomposition block, note it. |
| USD/KRW | `yfinance` `KRW=X` 1y | Skip FX factor → use neutral FX sub-score, flag estimated. |
| US 10y real yield | FRED public CSV `fredgraph.csv?id=DFII10`, latest non-empty | `real_rate.assumed_pct` from yaml (seeded), mark "(estimated)". |
| Central-bank tonnage, dollar reserve share | `gold_macro_factors.yaml` | Hard-required config; if file missing → use built-in seeded defaults + warn. |
| LLM summary paragraph | `claude -p` (claude-code mode) | Omit the summary line, print "(요약 생략: LLM 호출 실패)". Deterministic report still sends. |

**Fail-open principle:** no single fetch failure aborts the run. The report always sends; degraded
inputs are explicitly labelled in the output (matches this repo's "visible fail-open" ethos).

## Scoring logic

Two independent 0–100 scores, then a combined verdict. Weights, the real-rate thresholds, and the
central-bank tonnage baseline live in yaml; the scoring band breakpoints (drawdown/RSI/dollar bands)
are in code. Scorecard dot rendering = `round(score / 20)`, clamped 0–5.

### Technical score (weekly entry timing)
`technical = 0.40·trend + 0.30·pullback + 0.30·momentum`

- **trend** (price vs MA200/MA50 with MA200 slope over trailing 20 sessions):
  - price ≥ MA200 and MA200 rising → 100 (Stage-2 uptrend)
  - price ≥ MA200, MA200 flat/falling → 60
  - price < MA200 → 30
- **pullback** (drawdown from 52-week high — rewards buying dips, penalizes chasing and broken structure):
  - 0 to −5% → 40 (near high, chasing)
  - −5% to −20% → 100 (healthy accumulation zone)
  - −20% to −35% → 70 (deep correction)
  - < −35% → 30 (possible structural break)
- **momentum** (RSI-14): <30 → 100; 30–45 → 80; 45–60 → 60; 60–70 → 40; >70 → 15

Label: ≥60 양호 · 40–60 보통 · <40 비권장.

### Macro score (long-term structural bias)
`macro = 0.35·central_bank + 0.30·real_rate + 0.20·dollar + 0.15·fx` (weights from yaml)

- **central_bank** (config `trailing_4q_tonnes` vs `baseline_tonnes`): ≥900 → 100; 650–900 → 70; 500–650 → 50; <500 → 30
- **real_rate** (live DFII10 vs yaml thresholds): ≤`supportive_below_pct` → 100; between → 60; ≥`restrictive_above_pct` → 25 **and sets `restrictive` hard flag**
- **dollar** (config `reserve_share_pct`, falling share = supportive drift): <55 → 70; 55–60 → 55; >60 → 40
- **fx** (live USD/KRW vs its own 200d MA — double-edged for a KRW buyer): won very weak (>+5% above 200dMA) → 30 (reversion headwind, expensive entry); within ±5% → 60; won strong (<−5%) → 80 (cheap entry, FX tailwind ahead)

Label: ≥65 높음 · 45–65 중립 · <45 낮음. Reported honestly as a **bias heuristic**, not a probability
(no false precision).

### Verdict
Evaluated in order:
1. **🔴 PAUSE** if any hard flag: RSI > 75 (overbought spike) **OR** real-rate `restrictive` flag **OR** yaml `risk_off: true` (manual macro kill-switch).
2. **🟢 ACCUMULATE** if `macro ≥ 55` **AND** `technical ≥ 50`. Annotate "적극 분할" when `macro ≥ 65` and `technical ≥ 75`.
3. **🟡 HOLD** otherwise (bias/timing mixed).

## Config schema — `data/gold_macro_factors.yaml`

```yaml
# Slow structural macro inputs for the weekly gold trend job. Refresh quarterly.
# Seeded 2026-07-04 from deep-research (WGC / IMF / Fed / J.P. Morgan, 2026-07-03).
last_reviewed: 2026-07-04

central_bank:
  trailing_4q_tonnes: 950      # ~1,000t/yr structural since 2022 (2025 = 863t)
  baseline_tonnes: 500         # prior-decade average

dollar:
  reserve_share_pct: 58.0      # IMF COFER; flat since 2022

real_rate:
  supportive_below_pct: 1.0
  restrictive_above_pct: 2.0
  assumed_pct: 1.9             # fallback when FRED fetch fails

scoring:
  weights: { central_bank: 0.35, real_rate: 0.30, dollar: 0.20, fx: 0.15 }

risk_off: false                # manual kill-switch → forces PAUSE

position:                      # optional personal holding; null cost → P&L line omitted
  grams: 2.0
  avg_cost_krw_per_g: null     # set to your actual KRW/g buy price to enable P&L
```

## Output format

Telegram + `reports/gold-trend-YYYY-MM-DD.md`, Korean, e.g.:

```
[금 주간 분석] 2026-07-05 (KST)
판정: 🟢 ACCUMULATE — 이번 주 2g 분할 진행 권장

장기 상승 편향: 높음 (macro 68/100, 지난주 +3)
이번 주 진입:  양호 (technical 61/100, 지난주 −4)

▸ KRX 금현물(411060): 28,740원 | 고점대비 −24% | RSI 47 | MA200 −3%
▸ 달러금 3M −13.6% vs KRW금 −9.0% → 원화 쿠션(환 +4.6%p)
▸ 매크로 스코어카드 (macro 68 = 0.35·100 + 0.30·60 + 0.20·55 + 0.15·30)
  중앙은행 매수   ●●●●●  구조적 강세(연 ~950t, 100)
  실질금리 방아쇠 ●●●○○  비긴축·중립(DFII10 1.9%, 60)
  달러 신뢰도     ●●●○○  완만한 드리프트(58%, 55)
  환(원/달러)     ●●○○○  1,544 약세 → 되돌림 위험(30)
▸ 내 포지션: 2g @ 152,000원 · 평가 −1.2% (설정 시)
▸ 요약: (LLM 2–3문장, 위 수치에만 근거)
```

- Week-over-week deltas come from `state/gold_trend.json`.
- The position line renders only when `avg_cost_krw_per_g` is set.
- Degraded inputs are labelled inline (e.g., "(DFII10 estimated)").

## Delivery, schedule, CLI

- **CLI flags:** `--llm-mode {claude-code,none}` (default `claude-code`; `none` = deterministic only,
  used by tests/offline), `--no-telegram` (write report file only), `--config PATH` (default
  `data/gold_macro_factors.yaml`).
- **Cron (Sunday 21:00 KST, before the 22:00 weekly calibration):**
  ```
  0 21 * * 0 cd $PROJECT && uv run python scheduler/gold_trend.py >> $LOG_DIR/gold_trend.log 2>&1
  ```

## Testing

- **Unit (no network):** technical/macro/verdict scoring over a fixed case table (incl. all three
  PAUSE flags and boundary thresholds); yaml loader (valid / missing keys → defaults / `risk_off`);
  position P&L line (null cost omitted, set cost correct); state roll (append, cap 12, delta calc);
  fail-open branches (real-yield fallback labelled, `claude -p` failure omits summary, `411060`
  empty → GLD fallback).
- **Network-marked (`@pytest.mark.network`):** live `411060`, GLD, `KRW=X`, FRED fetches.
- Must pass `uv run pytest -m "not network"` cleanly.

## Dependencies

- `pyyaml` (currently under the `skills` extra) must be available to the cron runtime — move it into
  the base project dependencies (small, pure-Python) so the scheduler doesn't require `--extra skills`.
- No other new dependencies (`yfinance`, `pykrx`, `httpx` already present).

## File inventory

- **NEW** `scheduler/gold_trend.py`
- **NEW** `data/gold_macro_factors.yaml`
- **NEW** `tests/test_gold_trend.py`
- **EDIT** `scheduler/crontab.example` (cron line + header note)
- **EDIT** `pyproject.toml` (move `pyyaml` to base deps)
- **EDIT** `CLAUDE.md` (one line documenting the new weekly job, per docs-in-sync rule)

## Open / deferred

- **Real per-gram price:** the report uses the ETF proxy for *trend/timing*; it does not print the
  literal KRX 금현물 원/g trade price. Deferred — add later only if Doctor Cho wants exact entry pricing.
- **Central-bank tonnage auto-refresh:** stays manual (quarterly yaml edit). A future job could scrape
  WGC, but not now.
- **Verdict backtest / calibration:** the 12-week state file lays groundwork; formally scoring whether
  past `ACCUMULATE` calls led to gains is a future enhancement.
