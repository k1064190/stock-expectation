# Stage 13 — position-exit-manager skill

## Why

The portfolio toolset could report P&L, risk, and generic advice, but had no
per-holding **exit discipline**: when to cut a loser, where the trailing stop
sits, when to take partial profit, and when a holding's own prediction (thesis)
has been invalidated. This skill adds that decision layer so a daily review can
end with a concrete EXIT / TRIM / ADD / WATCH / HOLD per position.

## What

- **`compute_atr(bars, period=14)`** — Wilder true-range ATR in
  `mcp-market-data/indicators.py`, dual dict/object bar handling, `None` when
  `< period+1` bars. Feeds the chandelier trailing stop.
- **`compute_exit_actions(...)`** — pure, never-raises decision engine in
  `portfolio/exit_manager.py`. Five-rule precedence ladder (EXIT > TRIM > ADD >
  WATCH > HOLD), missing inputs downgrade to WATCH (never silent HOLD).
  Advisory only: no prediction mutation, no auto-sell.
- **`portfolio exit-check --market {US,KR} [--atr-mult] [--tp-rr]`** CLI command
  (`cmd_portfolio_exit_check`) mirroring `cmd_portfolio_advice`.
- **Skill** `position-exit-manager` (SKILL.md + references/exit_rules.md) with
  KR+EN triggers, decision table, Korean-first output, and the ATR/chandelier
  math.

## How

- ATR mirrors the existing `compute_rsi` seed-then-Wilder-smooth structure;
  added `_high`/`_low` helpers alongside `_close` for the same dual bar shape.
- `compute_exit_actions` evaluates each position independently behind a
  try/except that degrades to WATCH, satisfying the never-raise contract.
- Chandelier trailing stop = `swing_high(~22 bars) − atr_mult × ATR`; effective
  stop for R:R = `max(linked stop, trailing stop)`. The fixed-22-bar swing high
  is an intentional approximation of the textbook since-entry high-watermark
  (documented in references/exit_rules.md and the function docstring).
- Linking: latest OPEN prediction by `created_at` for normal rules; a separate
  latest-overall lookup powers the MISS thesis-invalidation EXIT (the spec lists
  `status==MISS` as a trigger, which is impossible under OPEN-only linking — so
  the two lookups are kept distinct).
- TDD throughout: ATR tests and one-per-rule + precedence + missing-data tests
  written first, then the implementation made them green.

## Code locations

- `mcp-market-data/indicators.py` — `compute_atr` (~L221-290), `_high`/`_low`.
- `mcp-market-data/tests/test_indicators.py` — `test_atr_*` (4 tests).
- `portfolio/exit_manager.py` — `compute_exit_actions`, `_evaluate_position`,
  `_chandelier_stop`, `_latest_open_prediction`, `_latest_prediction`.
- `portfolio/tests/test_exit_manager.py` — 21 tests (per-rule, precedence,
  missing-data WATCH).
- `stock_cli.py` — `cmd_portfolio_exit_check` (after `cmd_portfolio_advice`),
  `exit-check` subparser (after `advice`), `compute_atr`/`compute_exit_actions`
  imports.
- `.claude/skills/position-exit-manager/SKILL.md` + `references/exit_rules.md`.

## Retrospective

- The spec's "link to latest OPEN prediction" vs "EXIT on status==MISS" is
  internally contradictory (a MISS is not OPEN); resolved with a second
  latest-overall lookup rather than loosening the linkage. Worth a confirmation.
- The R:R formula naturally stops firing once the trailing stop ratchets above
  cost (negative denominator) — this is the correct "let winners run" behavior,
  and several test fixtures had to widen ATR so the trailing stop stayed below
  cost to exercise the R:R TRIM path. Carry forward: R:R take-profit only bites
  while the position still has real downside risk.
- Tests: `442 passed, 23 deselected` (full non-network suite);
  `90 passed, 6 skipped` for the targeted ATR + exit-manager set.

## Review loop

- **code-reviewer-pro:** clean — no blocking findings on the original stage diff.
- **codex (gpt-5.5) + gemini (pro):** surfaced three actionable items missed by
  the first pass:
  - **P0 — direction-awareness (both reviewers).** The exit rules applied
    BULL/long logic to a linked prediction's stop/target/MISS regardless of
    direction. A BEAR-linked prediction has its stop ABOVE entry and a BEAR
    MISS means price ROSE (bullish for a long), so the original code would
    spuriously EXIT a healthy long on the BEAR stop and "invalidate" the long
    on a BEAR MISS.
  - **P1 — prediction lookup (codex).** `cmd_portfolio_exit_check` used one
    mixed-status `limit=20` query that could drop the relevant OPEN thesis.
  - **P0/P1 — per-ticker robustness (both).** The per-ticker price/metric
    fetch loop was unwrapped, so one provider failure aborted the whole
    command.

### What was fixed

- **Direction-awareness** (`portfolio/exit_manager.py`): only a **BULL** linked
  OPEN prediction now supplies `pred_stop`/`pred_target`/`rr_progress`/
  `effective_stop`. A BEAR link is a *contradiction signal* — surfaced in a new
  `flags` field and nudged to WATCH, never a long-stop EXIT or TRIM. The
  MISS-invalidation EXIT now fires only when the latest-overall prediction is
  BULL. Six new direction-aware tests cover: BEAR stop-above-entry → no EXIT,
  BEAR → no TRIM on a profitable long, BEAR → WATCH+flag, BEAR MISS → no EXIT,
  and BULL stop/MISS regressions still fire. `flags` was added to all result
  shapes (normal + never-raise fallback) and `test_action_entry_has_all_fields`.
- **Prediction lookup** (`stock_cli.py`): replaced the single `limit=20` query
  with two `limit=1` queries — latest `status="OPEN"` (for stop/target/R:R) and
  latest-overall (for the MISS check) — deduped by id, consistent with how
  `exit_manager` links.
- **Per-ticker robustness** (`stock_cli.py`): each ticker's fetch/compute is now
  wrapped in try/except; a failure records the error, clears any partial state
  for that ticker (so it degrades to WATCH), and the error note is appended to
  that holding's `triggered_rules` rather than crashing the command.

- Tests after fixes: `96 passed, 6 skipped` for
  `portfolio/tests/test_exit_manager.py mcp-market-data/tests`; full
  `portfolio/tests` non-network suite `96 passed`.
