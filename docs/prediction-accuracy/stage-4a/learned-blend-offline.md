# Stage 4a — Learned blend: offline model + validation harness

## Why

Raw confidence carries zero discrimination (permutation test) and the isotonic
recalibrator collapses everything to the ~0.25 base rate — confidence has
stopped ranking picks. The standing capstone (docs/stage-11) prescribed a
learned blend over the persisted `components` pillars, gated on ≥100
components-tagged closed rows. That threshold is now crossed (150 usable rows).

## What

- `mcp-prediction-store/blend.py` — hand-rolled L2 logistic regression (no new
  deps, matching the hand-rolled isotonic PAV): fixed 11-feature vector (algo /
  news / llm_context / return_1m scores, overextension & regime one-hots,
  market, timeframe), column standardization, deterministic full-batch GD.
- **Leak-free walk-forward CV**: folds by `created_at`; each fold trains only
  on rows whose `outcome_date` precedes the fold's first test `created_at`
  (outcomes actually known at prediction time — overlapping horizons cannot
  leak). Baselines: raw confidence and a train-fold isotonic map. Metrics:
  Brier + rank AUC (pooled and fold-mean). `blend_wins` requires beating
  isotonic on Brier AND AUC > 0.55.
- `stock-cli blend-eval [--write]` — CV report; `--write` trains on all rows
  and saves `state/blend_model.json` (with CV metrics embedded) — **inert
  until stage 4b wires it in**.
- Weekly calibration report gains a "Learned blend (offline walk-forward CV)"
  section (fail-open).

## Live-data result (2026-07-12, leak-free CV)

On the real 150-row sample the blend **honestly loses**: blend Brier 0.233 /
AUC 0.406 vs isotonic Brier 0.123 (raw 0.348). Only one fold currently has
enough *already-known* outcomes to train on (the components-tagged history is
young and horizons overlap), and on it the blend under-ranks while isotonic's
flat base-rate output wins Brier. `blend_wins: false` → no live wiring; the
weekly report keeps re-evaluating as rows accumulate. This is exactly the
outcome the offline stage exists to catch.

## Code locations

- `mcp-prediction-store/blend.py` — full module
- `stock_cli.py` — `cmd_blend_eval` + `blend-eval` parser
- `scheduler/weekly_calibration.py` — `_render_blend_eval`, main() wiring
- `tests/test_blend.py` — 12 tests (coefficient recovery, L2 shrinkage, AUC
  known values, split time-ordering/no-leakage, end-to-end learnable-pattern
  fixture, persistence round-trip)

## Verification

- `uv run pytest -m "not network"` — 923 passed (12 new)
- Real-DB read-only eval (numbers above); empty-DB CLI smoke returns the
  zero-state shape

## Review loop

- **Antigravity (Gemini 3.1 Pro High)**: 1 CRITICAL — walk-forward folds split
  by `outcome_date` leaked outcomes not yet known at the test predictions'
  `created_at`. Fixed: folds anchor on `created_at`, per-fold training pool =
  rows with `outcome_date < first test created_at`, folds below min-train are
  skipped (+ regression test). 1 MEDIUM — L2 penalty divided by n decays with
  data growth; fixed (constant per-step penalty, `L2_DEFAULT` retuned 1.0 →
  0.05). 1 LOW — pooled AUC distorted by cross-fold regime shifts; fold-mean
  AUC now reported alongside.
- **code-reviewer subagent**: confirmed both fixes, no critical/warnings
  remaining ("ready to merge"); 1 docstring-verbosity suggestion (dismissed).
- **Codex**: CLI + GitHub bot both quota-exhausted at review time — Codex pass
  deferred to the PR bot round when quota resets.

## Retrospective

The harness paid for itself on day one: a plausible-sounding model would have
made live confidence *worse* (inverted ranking). Re-run weekly; wire in (4b)
only on a sustained CV win.
