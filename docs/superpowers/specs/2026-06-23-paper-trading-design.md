# Paper Trading System — Design Spec (2026-06-23)

## Purpose

Daily paper-trading ("모의투자") of the prediction algorithm to measure and improve it.
Two isolated books — KR seeded ₩100,000,000, US seeded $100,000 — run one cycle per day,
log NAV + returns over ≥1 month, and feed a weekly advisory improvement report. Bootstrap by
replaying the last ~2-3 months of already-logged LIVE predictions, then run forward via cron.

Settled with Doctor Cho (2026-06-23): signal source = logged LIVE predictions; fills =
realistic with simple costs; bootstrap = replay history + forward; improvement loop = advisory
report → human approves. Defaults adopted: long-only v1; index-benchmark column kept; risk
1%/trade, max 20% NAV/position, max 5 new/day, fallback stop 8%.

## Isolation

Separate `data/paper_trading.db` (SQLite, WAL) and a new `paper_trading/` package — never
mixed with the real Toss portfolio (`portfolio/`, `data/portfolio.db`). Each book is
single-currency (KR=KRW, US=USD); returns reported per-book as %, no cross-FX.

## Components

- **`paper_trading/models.py`** — schema + dataclasses + CRUD.
  - `accounts(id, market, base_currency, initial_capital, cash, created_at)`
  - `positions(id, account_id, ticker, qty, avg_cost, opened_at, prediction_id,
    target_price, stop_price, horizon_end_date)`
  - `trades(id, account_id, ticker, side, qty, price, gross, fees, tax, slippage,
    net_cash_delta, executed_at, reason, prediction_id)` — reason ∈
    entry / target_hit / stop_hit / horizon_exit.
  - `nav_history(id, account_id, date, cash, positions_value, nav, daily_return,
    cumulative_return, n_positions, benchmark_nav)` — one row/day/account; UNIQUE(account_id, date).
- **`paper_trading/costs.py`** — documented, configurable constants: US commission $0,
  KR commission 0.015%, KR sell transaction tax 0.18%, slippage 5 bps. `apply_buy_cost` /
  `apply_sell_proceeds` helpers returning net cash deltas.
- **`paper_trading/strategy.py`** — pure, no-I/O decision logic.
  - `decide_entries(account_nav, cash, held_tickers, predictions, prices, params)
    -> list[EntryOrder]` — from the day's fresh BULL BUY/WATCH LIVE predictions for the
    market: risk-based sizing (risk_per_trade × NAV / (entry − stop); fallback stop = entry ×
    (1 − fallback_stop_pct)); cap qty by max_position_pct × NAV and by available cash; at most
    max_new_positions_per_day, highest confidence first; skip tickers already held.
  - `decide_exits(positions, prices, today) -> list[ExitOrder]` — exit a lot when the day's
    high ≥ target (target_hit), low ≤ stop (stop_hit), or date ≥ horizon_end_date
    (horizon_exit). Mirrors `outcome_tracker` HIT/MISS/EXPIRED semantics, long-only.
- **`paper_trading/engine.py`** — `run_day(conn, account, as_of_date, price_lookup, params)`:
  fetch prices for held + candidate tickers → execute exits → execute entries (apply costs) →
  mark-to-market at close → record NAV. Fill price = next-session open (no look-ahead).
  Transactional per cycle; idempotent per (account, date).
- **`scheduler/paper_trading_run.py`** — cron CLI: `--market US|KR|ALL`, `--as-of DATE`,
  `--replay FROM..TO`. Reuses outcome_tracker's US/KR providers + holiday/timezone handling and
  `asof_backtest.py` for historical OHLCV. Seeds accounts on first run. Optional Telegram summary.
- **`scheduler/paper_trading_review.py`** — weekly advisory report
  `reports/paper-trading-review-YYYY-MM-DD.md`: P&L attributed by label / confidence-bucket /
  signal / components; Sharpe, max drawdown, win rate, avg hold; vs benchmark; concrete tuning
  recommendations. Advisory — Doctor Cho applies via PR. Extends the weekly-calibration pattern.

## Data flow

predictions.db (day's LIVE BULL BUY/WATCH) → strategy.decide_entries → engine executes with
costs → paper_trading.db (trades/positions) → daily mark-to-market → nav_history. Weekly:
nav_history + trades ⋈ predictions → review report → (human) tuning of daily_briefing/expect params.

## Bootstrap (replay)

`paper_trading_run.py --replay 2026-04-01..today` iterates each trading day: feed that day's
predictions, fill at next session open, mark to market — producing ~2-3 months of nav_history
immediately. Then cron runs forward daily.

## Error handling

- Missing price (holiday/halt/gap) → skip that action, carry positions, log a gap; no fabricated fills.
- No predictions for a market that day → no entries; still mark-to-market + record NAV.
- Provider failure → skip that account's cycle, retry next run; never corrupt cash/positions
  (per-cycle transaction).
- Re-running a date is idempotent (UNIQUE(account_id, date) on nav_history; trades deduped per cycle).

## Testing

- `strategy`: pure unit tests — entry sizing/caps, fallback stop, each exit reason, insufficient cash.
- `engine`: integration with a fake price lookup + in-memory DB — full cycle buy→hold→target-exit,
  NAV math including costs; idempotency.
- `models`: CRUD + UNIQUE/idempotency.
- `review`: P&L attribution math on a seeded book.
- replay smoke test over a small synthetic prediction set.
- TDD throughout; `uv run pytest -m "not network"` must stay green.

## Cron additions

Daily `paper_trading_run.py --market ALL` after outcome_tracker (~00:30 KST); weekly
`paper_trading_review.py` after weekly_calibration (Sunday).

## Scope guards (YAGNI)

Long-only v1 (BEAR/AVOID = don't hold); no shorting / options / margin / intraday; daily-close
marks only. One strategy (prediction-driven); index benchmark (KODEX 200 / SPY) logged only as
context. Default params in one config block.
