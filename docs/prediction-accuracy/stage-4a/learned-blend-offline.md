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
- **Walk-forward CV** (expanding window over outcome_date order, min-train 50,
  3 folds) against two baselines: raw confidence and a train-fold isotonic map.
  Metrics: Brier + rank AUC. `blend_wins` requires beating isotonic on Brier
  AND AUC > 0.55.
- `stock-cli blend-eval [--write]` — CV report; `--write` trains on all rows
  and saves `state/blend_model.json` (with CV metrics embedded) — **inert
  until stage 4b wires it in**.
- Weekly calibration report gains a "Learned blend (offline walk-forward CV)"
  section (fail-open).

## Live-data result (2026-07-12)

On the real 150-row sample the blend **honestly loses**: blend Brier 0.245 /
AUC 0.227 vs isotonic Brier 0.160 / AUC 0.168 (raw 0.327 / 0.482). Both
learned rankings are *inverted* (AUC << 0.5) — the June→July regime break
flips feature-outcome relationships across folds. `blend_wins: false` → no
live wiring; the weekly report keeps re-evaluating as rows accumulate. This
is exactly the outcome the offline stage exists to catch.

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

(three-reviewer outcomes recorded below)

## Retrospective

The harness paid for itself on day one: a plausible-sounding model would have
made live confidence *worse* (inverted ranking). Re-run weekly; wire in (4b)
only on a sustained CV win.
