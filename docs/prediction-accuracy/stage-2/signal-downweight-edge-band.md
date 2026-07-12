# Stage 2 — Dead-signal down-weight + low-edge-band tagging

## Why

30-day weekly calibration (2026-07-05) grades momentum (24.6%), volume (23.3%),
technical (26.5%), news (21.1%) statistically dead; only sector (52.6%) and
fundamental (67.5%) beat coin-flip. Separately, both paper books show the
0.60-0.70 raw-confidence band has negative realized edge (US +1.0%, KR -0.1%
avg round-trip) yet the bulk of predictions log there.

## What

- `/expect` ALGO table: Momentum +1.5→+1.0, Volume +1.0→+0.5 → ALGO ceiling
  8.0→7.0, so BUY (≥8.0) now always requires ≥ +1.0 of combined news/LLM-context
  confirmation. Positive LLM_CONTEXT must cite sector-RS or a fundamental
  catalyst — never technical/momentum grounds alone. All derived numbers
  (composite range, threshold rationale, worked examples, sample JSON) updated.
- `predict create`: LIVE BULL raw confidence in [0.60, 0.70] tagged
  `components.low_edge_band = true` — **tag, don't block** (a recal-confidence
  floor is inactionable because isotonic collapses everything to ~0.25; and
  blocking would starve the stage-4 learned blend of training rows).
- `paper_trading_run`: `filter_low_edge_band` skips tagged predictions before
  committing paper capital; fail-open on absent/unparseable components.

## How

TDD: 7 tagging tests + 1 filter test written first. Tag lives in
`cmd_predict_create` after the stage-1 gate refresh; filter is a pure helper
called at the top of `run_range` with an info log of the skipped count.

## Code locations

- `mcp-prediction-store/models.py` — shared `LOW_EDGE_BAND` constant
- `stock_cli.py` — tag block in `cmd_predict_create` (CLI path)
- `scheduler/daily_briefing.py` — mirrored tag in `log_predictions` (API path)
- `scheduler/paper_trading_run.py` — `filter_low_edge_band`, SELECT + `run_range` wiring
- `.claude/skills/expect/SKILL.md` — point table, rationale note, Step 5b guidance,
  worked examples
- `tests/test_low_edge_band.py`, `scheduler/tests/test_paper_trading_run.py`

## Verification

- `uv run pytest -m "not network"` — 895 passed (8 new)
- Stale-reference sweep of SKILL.md for old 8.0-ceiling references — only the
  BUY threshold (intentionally 8.0) remains

## Review loop

- **code-reviewer subagent**: clean for merge; 0 critical/warnings, 2
  non-blocking comment suggestions (dismissed — existing comments already cite
  the rationale and the paper log already states the skip reason).
- **Codex (gpt-5.6-sol, high)**: 2 P2s, both fixed — overview line still said
  composite max +14 (→ +13); worked example claimed a counterfactual BUY that is
  WATCH 7.5 under new weights (re-worded as pre-2026-07 behavior). Its "-11
  composite floor" sub-claim was initially dismissed but is CORRECT (the old
  doc double-counted the earnings penalty): true drag is -3.0, floor -4.0 with
  earnings, composite floor -11.0 — fixed in the PR-review round below.
- **PR #62 bots (round 1)**: Codex bot P2 — low_edge_band tag missed the API-mode
  `log_predictions` path → constant moved to `models.LOW_EDGE_BAND`, tag mirrored
  there, regression test added. Gemini Code Assist — `filter_low_edge_band`
  TypeError when components is already a dict (fixed defensively), the ALGO-floor
  arithmetic above (fixed), temp-DB cleanup in test fixture (fixed).
- **Antigravity (Gemini 3.1 Pro High)**: no findings; verified all arithmetic
  including both worked examples.

## Retrospective

Changing an LLM-executed prose spec cascades through worked examples and
derived sums — the consistency sweep mattered as much as the table edit.
