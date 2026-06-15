# Stage 11 · Deterministic recalibration (A) + dead-signal pruning (B)

## Why
- **F1/F7 (overconfidence + unused recal map):** the 0.60–0.70 confidence band
  ran +0.28 over actual, and although `/expect` *told* the operator to apply the
  recalibration map by hand, the backfill proved logged confidences were still a
  near-constant ~0.6. A manual LLM step is not reliable; recalibration must be
  deterministic.
- **F2/F5 (dead signals):** `valuation`, `cycle`, `mean_reversion` hit 0% and
  only pollute per-signal calibration.

## What
- **A — `--recalibrate` flag on `predict create`** (`stock_cli.py`). Maps the raw
  `--confidence` through the source-scoped isotonic curve before storing; echoes
  `raw_confidence` / `recalibration_applied` in the JSON. Safe no-op below
  `MIN_CLOSED_FOR_RECAL = 30` closed predictions of that source.
- **B — dead signals removed** from the allowable `--signals` list in both
  `/expect` and `daily-briefing` skills; operators told to route those reads
  through `LLM_CONTEXT_SCORE` instead.
- **Pre-existing fix (authorized):** `weekly_calibration.py:208,236` formatted
  `avg_return` with `:+.2%` (×100 inflation). Now `:+.2f%`.
- Tests: `tests/test_recalibration_cli.py` (3) — min-sample gate, overconfident
  pull-down, source-scoping.

## How
- Recalibration reuses `build_recalibration_map`/`apply_recalibration`
  (`mcp-prediction-store/metrics.py`). A **global** (all-horizon) map is used —
  the backfill found it as accurate as per-horizon maps (Brier 0.160 vs 0.169)
  and more robust at current sample sizes.
- Source-scoped: a LIVE prediction calibrates only against LIVE history, never
  the over-fit INTERACTIVE rows (the F4 gap).
- Logging threshold still gates on **raw** confidence; only the stored value is
  recalibrated, so "save each horizon ≥ 0.60" semantics are unchanged.

## Verification (backfill A/B + live map inspection)
- Backfill OOS Brier: LIVE **0.331 → 0.160**, ALL **0.340 → 0.204** (leakage-safe
  train/test split).
- **Caveat surfaced by inspecting the live map:** the production LIVE curve has a
  single anchor (0.612 → 0.248) because raw confidence is near-constant, so
  recalibration collapses every confidence to the ~0.25 base rate. This is the
  source of the Brier win (base-rate beats a non-informative 0.6 — the
  permutation test already showed raw confidence carries no signal), but it
  means logged confidence stops *ranking* picks until the model emits an
  informative confidence spread. Restoring discrimination is downstream work
  (change E / meta-labeling), not recalibration's job.

## Review loop (code-reviewer + codex + gemini)
- **CRITICAL (all 3, fixed) — recalibration corrupted its own training data.**
  Storing the recalibrated value back into `confidence` poisoned future maps:
  `build_recalibration_map`/`get_calibration_report` read `confidence` as the
  *raw* predictor, so once recalibrated rows (≈0.25) closed they would re-enter
  the curve as if raw, collapsing its domain (recursive feedback) and silently
  double-applying on replays. **Fix:** added a nullable `raw_confidence` column
  (`models.py` schema + additive `_ensure_raw_confidence_column` migration +
  dataclass/insert/reader); `predict create` now always persists the raw
  confidence there, and calibration trains on `COALESCE(raw_confidence,
  confidence)` so it can never see recalibrated values. New test
  `test_map_trains_on_raw_not_recalibrated` proves a stored-0.30 / raw-0.65 row
  contributes 0.65 to the map (no spurious low anchor). Migration verified on a
  copy of production (1169 rows preserved, column added, legacy map unchanged).
- **Fixed — test gaps (codex/code-reviewer):** added empty-map-after-gate no-op,
  the no-corruption test, and an end-to-end `cmd_predict_create --recalibrate`
  test asserting raw is kept and stored confidence is calibrated.
- **Acknowledged — replay double-apply:** if a caller manually re-submits a
  *stored* (already-calibrated) confidence with `--recalibrate`, it would map
  again. Out of the normal `/expect` path (which always passes the model's raw
  confidence); documented, not gated.

## Code locations
- `stock_cli.py` — `_recalibrated_confidence`, `MIN_CLOSED_FOR_RECAL`,
  `cmd_predict_create`, `--recalibrate` arg, `raw_confidence` persistence.
- `mcp-prediction-store/models.py` — `raw_confidence` column, schema,
  `_ensure_raw_confidence_column`, dataclass field, insert, `_row_to_prediction`.
- `mcp-prediction-store/metrics.py` — `get_calibration_report` trains on
  `COALESCE(raw_confidence, confidence)`.
- `scheduler/weekly_calibration.py:208,236` — avg_return format fix.
- `.claude/skills/expect/SKILL.md` — recalibrate step + dead-signal list.
- `.claude/skills/daily-briefing/SKILL.md` — recalibrate step + dead-signal rule.
- `tests/test_recalibration_cli.py` — 7 unit tests.

## Retrospective
- Moving recalibration from a hand-applied LLM instruction to a CLI flag is the
  real fix for F7 — the map existed for weeks but was never reliably applied.
- Inspecting the *actual* live map (not just the backfill delta) caught that
  recalibration currently degenerates to base-rate. Better to log that honestly
  now than to discover later that confidence stopped discriminating.
