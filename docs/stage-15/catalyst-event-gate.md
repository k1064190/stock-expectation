# Stage 15 — Catalyst Event Gate (R3)

## Why

The /expect score (ALGO + NEWS + LLM_CONTEXT) and its R1 (regime) / R2
(overextension) gates are all **state**-based — none of them can see a *dated
binary event*. A technically strong, macro-confirmed setup is still a bad entry
if the next trading day is an earnings print or a Fed decision: the outcome is a
coin-flip gap, not the thesis. Backtest intuition: BULL calls logged the day
before a report gap on the print regardless of setup quality. R3 closes that
blind spot with a deterministic, fail-open event-risk gate.

## What

- **Unified forward catalyst timeline** merging two FMP calendars:
  - `/earning_calendar` → per-ticker earnings events (US-listed only).
  - `/economic_calendar` → market-wide High-impact macro (FOMC / CPI / NFP).
- **RULE R3 gate** (`EventGate`): per-ticker earnings cap/trim (US) + a
  market-wide macro trim (US + KR). KR is **macro-only** — it consumes the US
  macro stream (transmits via FX / SOXL) but gets no per-ticker earnings cap
  (FMP has no forward KR EPS feed).
- **CLI**: `bin/stock-cli catalyst timeline TICKERS --market {US,KR} [--days 14]
  [--include-macro]` and `bin/stock-cli catalyst gate TICKERS --market {US,KR}`.
- **New `catalyst-event-gate` skill** documenting the timeline structure, the R3
  rule table, the CLI, the KR-vs-US matrix, and R1+R2+R3 stacking.
- **`/expect` wiring**: a Step 4 `catalyst gate` fetch, RULE R3 in the Step 7
  gate section, and the ALGO 'Earnings event' row now fed by R3.

### R3 thresholds (trading days)

| Band | Action |
|---|---|
| earnings `td ≤ 2` (`EARNINGS_WATCH_DAYS`) | cap label → WATCH |
| earnings `2 < td ≤ 5` (`EARNINGS_TRIM_DAYS`) | confidence_trim = 0.05 |
| earnings `td > 5` | none |
| High-impact FOMC/CPI/NFP `td ≤ 2` (`MACRO_TRIM_DAYS`) | macro_trim = 0.05 (every pick in market) |

Stacking: a WATCH cap from R1/R2/R3 wins the label; confidence is pulled down by
the strictest cap then the R3 trims (both R3 trims can stack on one pick); **R3
never raises the BUY bar** (only R1/R2 do).

## How

- Pure core is stdlib-only and fetcher-injectable, so the entire R3 logic tests
  offline: `build_timeline` / `evaluate_gate` accept optional `fetch_earnings`
  / `fetch_macro` callables (default to the real httpx FMP wrappers; tests pass
  stubs).
- `trading_days_between` counts Mon–Fri boundaries crossed (same-day = 0,
  weekend-aware). Holidays are intentionally ignored — documented caveat; the
  only effect is making an event look ~1 day farther away, the safe direction
  for a fail-open risk gate.
- **Quota protection**: `evaluate_gate` / the CLI fetch the earnings + macro
  windows **once per call** and iterate all tickers in memory (FMP free tier is
  250/day). KR skips the earnings fetch entirely.
- **FAIL-OPEN** mirrors the rest of `mcp-market-data`: missing `FMP_API_KEY`
  **or** any fetch exception → neutral `EventGate(gate_unavailable=True)` with
  zero caps/trims; CLI still exits 0. Matches the NEVER-raise provider contract.

## Code locations

- `mcp-market-data/events.py` — `CatalystEvent`, `EventGate`,
  `trading_days_between`, `build_timeline`, `evaluate_gate`, thresholds, and the
  thin `_fetch_earnings_window` / `_fetch_macro_window` httpx wrappers.
- `stock_cli.py` — `cmd_catalyst_timeline`, `cmd_catalyst_gate`,
  `_catalyst_tickers`, the `catalyst` nested subparser, `events` import, and the
  added `os` / `timedelta` imports.
- `mcp-market-data/tests/test_events.py` — 17 offline tests (stubbed fetchers):
  `trading_days_between` boundaries, R3 earnings/macro thresholds, KR
  macro-only, single-fetch quota guard, timeline merge/grouping, and both
  fail-open paths (missing key + simulated fetch exception).
- `.claude/skills/catalyst-event-gate/SKILL.md` — new skill doc.
- `.claude/skills/expect/SKILL.md` — Step 4 fetch, Step 5 ALGO row, Step 7
  RULE R3 + R1+R2+R3 stacking.

## INTEGRATION TODO (A.2) — daily-briefing wiring (owned by WT-A.2)

Deliberately **not** done here (that worktree owns the briefing wiring). When
A.2 lands, wire R3 into the briefing:

1. **`scheduler/daily_briefing.py`** — after the candidate ticker set is known
   (the screened/discovered picks for each market), call
   `evaluate_gate(asof, candidate_tickers, market)` **once per market** (one
   earnings + one macro fetch, fail-open). Inject the result as an
   `{event_gate}` block into the briefing prompt context alongside the existing
   regime/breadth blocks, e.g.:
   ```python
   from events import evaluate_gate
   gate = evaluate_gate(asof, candidate_tickers, market)
   context["event_gate"] = asdict(gate)   # by_ticker caps/trims + macro_trim + gate_unavailable
   ```
   Keep it fail-open: if `gate.gate_unavailable`, render a "(event gate
   unavailable)" line and do not alter picks.

2. **`scheduler/prompts/briefing_*.md`** — add an R3 block that mirrors
   /expect Step 7: a WATCH cap (earnings `td ≤ 2`) downgrades a would-be BUY to
   WATCH; the earnings trim and `macro_trim` shave confidence; KR is macro-only.
   Surface the per-pick earnings date + `trading_days_until` in the reasoning.

3. **`.claude/skills/daily-briefing/SKILL.md`** — document the new `{event_gate}`
   input and the R3 lines, parallel to how R1/R2 are described there.

## Validation note (deferred)

Per-stage R3 backtest validation is **not** built here. Once this branch merges,
event-gate efficacy should be validated by reusing `scheduler/asof_backtest.py`
(the shared as-of harness) — do **not** extend `backfill_eval` or stand up a
separate backtest harness.

## Retrospective

- Went well: fetcher injection made the whole R3 ruleset testable offline (17
  green tests, 0.04s, no network); fail-open + single-fetch quota guard fell out
  cleanly from copying the existing `us.py` provider contract.
- Carry forward: KR is structurally macro-only until a forward KR EPS feed
  exists — revisit if a KR earnings calendar source is added. The holiday-naive
  `trading_days_between` is fine as a fail-open gate but would need a market
  holiday calendar if R3 is ever used for hard date math.
