# Stage 1 — Store-time overextension gate refresh

## Why

The store-level overextension gate (RULE R2, `_check_overextension_gate`) hard-rejects
LIVE BULL entries on parabolic blow-offs — but it reads `overextension`/`return_1m`
from the caller-supplied `--components` JSON and fails open when they are absent. A
briefing/expect run that omitted components silently bypassed the one enforcement that
survives cron (the INTEGRATION TODO in `docs/stage-12/blended-funnel-gate.md`).
Entry-time evidence: EXTREME-overextended BULL entries won 29.1% vs 53.5% at NONE.

## What

`stock-cli predict create` for LIVE BULL now backfills missing `overextension` and
`return_1m` into components from fresh price bars before insertion, so the R2 gate
always has real data to act on. Caller-supplied values are never overwritten.
Fail-open on any fetch/compute error (stderr warning). New `--no-gate-refresh`
escape hatch for offline use. INTERACTIVE and BEAR creates are untouched.

## How

New `_refresh_overextension_components()` fetches ~120 calendar days of bars via the
existing provider (`_get_provider().get_price_history`) and reuses
`compute_horizon_metrics` (which already computes `overextension_level` and
`return_1m`); only missing keys are injected. Wired into `cmd_predict_create` between
components parsing and DB insert. TDD: 10 tests written first, then the implementation.

## Code locations

- `stock_cli.py` — `_refresh_overextension_components` (+ `GATE_REFRESH_DAYS`), call
  site in `cmd_predict_create`, `--no-gate-refresh` argparse flag
- `tests/test_gate_refresh.py` — helper unit tests + `cmd_predict_create` integration
  tests (parabolic reject, healthy insert with injected components, escape hatch,
  INTERACTIVE skip, fail-open insert)

## Verification

- `uv run pytest -m "not network"` — 883 passed (10 new)
- Live E2E: `predict create NVDA LIVE BULL` without components against a scratch DB →
  real bars fetched, `{"overextension": "NONE", "return_1m": 0.0133}` injected and stored

## Review loop

- **code-reviewer subagent**: 1 critical (BEAR-path test missing), 2 warnings
  (sparse-bars `return_1m=None` untested; KR ticker path untested), 1 suggestion
  (include exception type in the fail-open warning). All four addressed in
  `fb7f3bd` — 3 tests added (13 total), warning message now includes
  `type(exc).__name__`. No implementation bugs found.
- **Codex (gpt-5.6-sol, high)**: no findings — all four requirements
  (fail-open, no caller-value overwrite, escape hatch, INTERACTIVE/BEAR
  unaffected) judged satisfied.
- **Antigravity (Gemini 3.1 Pro High)**: no correctness bugs; 2 low test gaps —
  BEAR integration test (already added via the code-reviewer finding) and a
  CLI-level "complete components skips fetch" test (added). Confirmed the
  `{"overextension": null}` edge honors caller values.
- **Codex PR bot (PR #61)**: "Didn't find any major issues" — clean in one round.

## Retrospective

The gate hole was closable entirely with existing utilities (provider + indicators);
the only new logic is the merge policy (inject missing keys only) and fail-open.
