# Stage 29 — ISA Monthly Briefing, Scheduler, NAV Track Record

## Why

Stages 26-28 built the data, the selection, and the decision core. What remained to
close the ISA loop: a monthly record of how the book actually performs (NAV vs
benchmarks), and an automated monthly briefing that turns the CLI data into an
actionable Korean contribution plan — with the LLM confined to proposing tilts that
the stage-28 CLI clamps and logs.

## What

- `isa snapshot` — pure-Python NAV track record: book value, cumulative net
  contributions (BUY − SELL cost), and benchmark closes into a new `isa_nav` table.
  `isa status` now surfaces `recent_snapshots` (last 3), `contributions_cum_krw`,
  and `since_inception_return_pct`.
- `scheduler/isa_briefing.py` — monthly runner: snapshot → status/rebalance JSON →
  prompt (macro block included) → claude-code/codex-cli → Telegram. `--amount` is
  always explicit; `--dry-run` prints the prompt with zero side effects.
- `.claude/skills/isa-briefing/SKILL.md` — the monthly contribution skill (Korean
  output; hard rules below).
- Crontab entry: `37 7 1 * *` (1st of month 07:37 KST; comment says adjust day and
  amount to the user's schedule).
- 12 new tests (2 store, 5 CLI, 5 scheduler), all offline.

## How

**Benchmarks:** both fetched via the **US provider's yfinance path** — verified
2026-07-10: `^GSPC` and `^KS11` (KOSPI) work there, while the KR provider's ticker
normalization mangles index symbols into `0^KS11` (404 on pykrx and FDR). Each
benchmark is stored as `{"close": float|null, "session": "YYYY-MM-DD"|null}`:
closes are **prior-session values** and market calendars diverge (a KST morning
run sees yesterday's ^GSPC but possibly today's ^KS11), so the session date
disambiguates which trading day each close belongs to. A failed fetch stores
nulls with a visible note (fail-open). Same-day snapshot re-runs replace that
day's row (with a visible "replaced same-day snapshot" note) so debugging never
pollutes the monthly history.

**Reuse, not copy:** the scheduler imports `call_claude_code` / `call_codex_cli` /
`_macro_block` from `scheduler/daily_briefing.py` and `send_briefing` from
`scheduler/telegram_sender.py`. Data steps go through `bin/stock-cli isa ...`
subprocesses so the tilt clamp and decision logging stay in one place.

**Dry-run semantics:** `--dry-run` skips the snapshot AND the decision-logging
`isa rebalance` (the status payload's band check stands in, with a note) — zero
writes, no LLM, no Telegram.

**RISK_OFF interaction (explicitly resolved in the skill):** stage-24's RISK_OFF
switch gates new stock BULL predictions, NOT the ISA monthly contribution. Long-term
DCA continues through risk-off by design; risk-off is a reason to keep tilts at 0,
never to skip the month.

**Return metric:** `since_inception_return_pct` is simple net-contribution return
((nav − contributions)/contributions); a money-weighted refinement is future work
(documented in the helper docstring). The annual tilt-decision evaluation from the
spec is DATA-READY via `isa_decisions` + `isa_nav` but not built — no data exists to
evaluate yet.

## Code locations

- `portfolio/isa_store.py` — `isa_nav` table, `save_nav_snapshot`,
  `list_nav_snapshots`
- `stock_cli.py` — `cmd_isa_snapshot`, `_isa_cum_contributions`, `ISA_BENCHMARKS`,
  status track-record keys, `snapshot` subparser
- `scheduler/isa_briefing.py` — `build_prompt`, `_stock_cli_json`, `main`
- `scheduler/crontab.example` — monthly entry
- `.claude/skills/isa-briefing/SKILL.md`
- `portfolio/tests/test_isa_store.py`, `tests/test_isa_cli.py`,
  `scheduler/tests/test_isa_briefing.py`

## Retrospective

Probing the benchmark tickers against the real providers before writing tests avoided
baking in a KR-provider path that 404s. Confining dry-run to a truly write-free path
(status only) keeps the flag honest — anything that logs is skipped, visibly.

## Review

Round 1 (internal: clean, 3 non-blocking observations; Gemini/antigravity: real
findings; Codex: deferred — quota exhausted, noted on the PR), addressed in
`fix(isa): explicit SELL-only contribution sign, same-day snapshot replace,
benchmark session dates (review round 1)`:

**Fixed (Gemini)**
- (BLOCKER) `_isa_cum_contributions`' `ELSE -quantity * price` treated ANY
  non-BUY side as a sell. The schema CHECK-constrains side to BUY/SELL today,
  but the SQL is now explicit and future-proof: `WHEN 'BUY' → +`,
  `WHEN 'SELL' → −`, `ELSE 0` (a future side must opt in rather than silently
  counting as a sell). BUY/SELL sign behavior pinned by the existing
  cum-contribution tests.
- Scheduler dry-run substituted `status["rebalance"]`, which lacks
  `min_contribution_to_restore` — breaking the prompt contract SKILL.md
  expects. The dry-run block now keeps the key (null) with the visible note
  "dry-run: remedy not computed (rebalance skipped to avoid decision
  logging)"; test asserts the prompt shape.
- Same-day `isa snapshot` re-runs appended duplicate rows. `save_nav_snapshot`
  now replaces an existing row from the same calendar date
  (delete-then-insert on `date(snapped_at)`) with a visible "replaced
  same-day snapshot" note; monthly cadence unaffected. Tests: same-day rerun
  → 1 row with latest values + note; different days → 2 rows. (The internal
  reviewer had flagged the same as an observation.)
- Benchmarks stored close-only, so a stale `^KS11` close was indistinguishable
  when calendars diverge. Each benchmark is now
  `{"close": float|null, "session": "YYYY-MM-DD"|null}` with the close's
  actual session date captured from the price series; semantics documented
  here and in the README line. This also resolves the internal reviewer's
  benchmark-timezone observation.
- Added the missing test: `isa snapshot` with a missing target → rc 1 early
  exit and no `isa_nav` row written.

**Dismissed**
- (Gemini nit) importing `_macro_block` etc. from `daily_briefing`:
  intentional, documented reuse inside one repo — extracting a shared module
  is refactoring beyond this change's need (internal reviewer concurred).
