# Stage 19 — Paper-trading ("모의투자") system

## Why

Doctor Cho wants to measure and improve the prediction algorithm by trading it
with fake money: one simulated book per market (US $100,000, KR ₩100,000,000),
a daily cycle, ≥1 month of logged NAV/returns, and a periodic advisory report
that recommends algorithm tuning. Realized P&L is the ultimate scoreboard the
HIT/MISS track record and calibration only approximate.

## What

A new isolated `paper_trading/` package + two scheduler entry points, trading the
logged LIVE **BULL** predictions long-only:

- **models.py** — `data/paper_trading.db` schema (accounts / positions / trades /
  nav_history) + dataclasses + CRUD. CRUD does not commit; the caller owns the
  transaction so a daily cycle is atomic.
- **costs.py** — slippage (5 bps), commission (US 0 / KR 0.015%), KR sell tax 0.18%.
- **strategy.py** — pure `decide_entries` (risk 1%/trade, ≤20% NAV/position, ≤5
  new/day, fallback stop 8%, confidence floor 0.60, one lot/ticker/day) and
  `decide_exits` (target / stop / horizon, stop-priority when both touched).
- **engine.py** — `run_day`: exits → entries → mark-to-market → record NAV, as one
  committed transaction; idempotent per (account, date); carry-forward marks on data gaps.
- **review.py** — pure metrics: drawdown, Sharpe (sample stdev), round-trip P&L
  pairing by prediction_id, confidence attribution.
- **scheduler/paper_trading_run.py** — cron + `--replay FROM..TO`; reuses the US/KR
  providers; benchmark = SPY / KODEX 200 buy-and-hold.
- **scheduler/paper_trading_review.py** — weekly advisory report
  `reports/paper-trading-review-YYYY-MM-DD.md` with tuning recommendations.
- Cron: daily tick 06:30 KST, weekly review Sunday 22:30 KST.

**Bootstrapped result** (replay 2026-04-04 → 06-22, ~54/52 trading days): US **+1.80%**
(Sharpe 0.61, maxDD −6.94%, 48 round-trips, 40% win) vs SPY +13.4%; KR **−1.92%**
(Sharpe −0.05, maxDD −12.5%, 63 round-trips, 38% win) vs KODEX 200 +81%. The headline
finding: stop-outs (0% win, all losses) outnumber target-hits, and fixed target exits
cap upside — the long signal badly lags passive indices in a melt-up.

## How

Signal source = predictions.db LIVE BULL rows (all carry target+stop). Fills at the
signal day's session close (no look-ahead); a prediction's exchange-local date
(`signal_local_date`, UTC→market tz) maps to the first trading session on/after it
(`effective_entry_date`), so a pre-open KR 07:00 KST signal trades the right session.
Pure decision/metric logic is unit-tested; the live replay smoke-tests the wiring.
Built TDD (41 new tests; full suite 641 passing).

## Code locations

- `paper_trading/{models,costs,strategy,engine,review}.py`
- `scheduler/paper_trading_run.py`, `scheduler/paper_trading_review.py`
- `paper_trading/tests/test_pt_*.py`, `scheduler/tests/test_paper_trading_run.py`
- `scheduler/crontab.example` (daily tick + weekly review lines)
- Spec: `docs/superpowers/specs/2026-06-23-paper-trading-design.md`

## Review loop

- **code-reviewer-pro**: 0 critical; 1 warning (idempotency fragility on mid-cycle
  crash) — fixed by making `run_day` one transaction (CRUD no longer commits). 1
  suggestion (defensive duplicate-ticker guard in `decide_entries`) — applied.
- **Codex (gpt-5.5, high)**: [BLOCKER] KR UTC date mis-mapping → fixed with
  `signal_local_date`/`effective_entry_date` (KR cumulative shifted +2.53%→−1.92%,
  confirming the bias was real); [BLOCKER] non-atomic cycle → same transaction fix;
  [MAJOR] stale `avg_cost` mark on gaps → carry-forward `marks`; [MINOR] Sharpe
  population→sample stdev. All four fixed with tests.
- **Gemini**: unavailable (free-tier CLI deprecated — `IneligibleTierError`).

## Retrospective

- The replay paid off immediately: instead of waiting a month, ~2.5 months of NAV
  came from already-logged predictions, and the review surfaced a concrete,
  actionable finding (target exits + stop-outs lag a trending tape).
- The Codex timezone catch mattered — it flipped KR from positive to negative.
  Lesson: always convert stored UTC timestamps to exchange-local before dating fills.
- Carry forward: the advisory recommendations (raise confidence floor, ATR trailing
  stop, signal quality over sizing) are the first concrete inputs to the
  algorithm-improvement loop; next is acting on one via PR and measuring the delta.
