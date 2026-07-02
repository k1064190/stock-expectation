# Stage 23 — Hard-reject LIVE 1Y predictions

## Why

Measured on live data (2026-07-02, 30-day closed window), LIVE predictions with
timeframe `1Y` are **0 hits / 12 closed** with an average outcome return of
**-23.8%** — by far the worst horizon (6M -7.1%, 1M -3.3%, 1W -0.8%). The
system cannot forecast a year out; every automated 1Y row only poisons the
track record. Decision: stop logging LIVE 1Y entirely, mirroring the existing
LIVE BEAR hard-reject invariant.

## What

- Store-level gate in `insert_prediction`: `source='LIVE'` AND `timeframe='1Y'`
  → `ValueError` citing the 0/12, avg -23.8% evidence. BACKTEST and INTERACTIVE
  1Y remain allowed (evaluation runs / deliberate manual override).
- API-mode scheduler (`log_predictions`) skips 1Y rows explicitly (visible
  no-op, same pattern as the BEAR skip) instead of erroring opaquely.
- All prompt surfaces that instructed the LLM to emit 1Y horizons now say the
  Cycle (1Y) view is narrative/analysis-only and must never be logged:
  expect skill (new RULE C4), scheduler inline prompts (US + KR), and the
  API-mode prompt files (JSON examples switched from 1Y to 6M).
- Tests: 4 new gate tests (reject LIVE 1Y; allow INTERACTIVE 1Y, BACKTEST 1Y,
  LIVE 6M); one existing metrics fixture switched to `source="INTERACTIVE"` to
  preserve its timeframe-filter intent.

6M (-7.1% avg) was reviewed and deliberately KEPT for now — minimal change,
and the June-shock cohort skews that number.

## How

Copied the LIVE BEAR gate pattern exactly: a two-condition check at the top of
`insert_prediction` (the one choke point every mode passes through — CLI,
skills, and API mode), with the error message naming the measured evidence and
the sanctioned overrides. `validate_prediction_dict` was left untouched, same
as the BEAR gate (it validates shape, not policy). The scheduler's explicit
skip keeps API-mode logs clean, and RULE C4 in the expect skill preserves the
Cycle read as an input to RULE C1 while banning the row itself.

## Code locations

- `mcp-prediction-store/models.py` — gate at `insert_prediction` (right after
  the BEAR gate, ~line 556) + updated docstring guard list
- `scheduler/daily_briefing.py` — 1Y skip in `log_predictions` (~line 1052);
  inline US/KR prompt horizon lines (~lines 579, 669)
- `scheduler/prompts/briefing_us.md`, `scheduler/prompts/briefing_kr.md` —
  horizon instructions + JSON examples (1Y → 6M), narrative-only Cycle note
- `.claude/skills/expect/SKILL.md` — RULE C4 (after RULE C3), Step 9 table
  annotation, `horizons_logged` contract, worked example
- `mcp-prediction-store/tests/test_models.py` — `test_live_1y_is_rejected`,
  `test_interactive_1y_is_allowed`, `test_backtest_1y_is_allowed`,
  `test_live_6m_is_allowed`
- `mcp-prediction-store/tests/test_metrics.py` — 1Y calibration fixture now
  INTERACTIVE

## Review

code-reviewer on PR #51: no blockers, no should-fix. Two nits, both applied:
(1) README still described /expect as logging 1W/1M/6M/1Y — updated to
1W/1M/6M with the LIVE-1Y hard-reject noted (INTERACTIVE/BACKTEST allowed);
(2) the API-mode skip condition now checks `pred.source == "LIVE"` explicitly
alongside `timeframe == "1Y"` — functionally identical (API mode only creates
LIVE) but self-documents the gate scope.

## Retrospective

The BEAR gate template made this nearly mechanical — one store gate, one
scheduler skip, prompt surfaces, tests. Worth carrying forward: policy gates
live at `insert_prediction` only; prompt edits are advisory, the store is the
enforcement that survives cron.
