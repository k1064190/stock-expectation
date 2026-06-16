# Stage 11 · Backfill A/B evaluation harness (Stage 0 of the accuracy initiative)

## Why
Prediction win rate collapsed 73% (2026-05-11) → 24% (2026-06-14). The approved
plan (`~/.claude/plans/glittery-gliding-crystal.md`) prescribes six accuracy
changes (A–F). Every change must be judged on **past data before** it touches
the live `/expect` flow — otherwise we risk repeating the LIVE-vs-INTERACTIVE
optimism (F4). This harness is that gate.

## What
`scheduler/backfill_eval.py` — read-only A/B tool over closed (HIT/MISS)
predictions, plus `tests/test_backfill_eval.py` (9 tests, all passing).

Sections it reports:
- **[A] Isotonic recalibration** — leakage-safe: fits the recal map on an
  earlier train slice, scores raw-vs-recalibrated Brier on a held-out recent
  test slice.
- **[B] Dead-signal contribution** — splits closed rows by whether they used a
  dead signal (`valuation`/`cycle`/`mean_reversion`).
- **[D] Regime-window suppression** — outcome of BULL predictions created inside
  a risk-off window (what a hard regime gate would have suppressed).

### First run results (real DB, n=420 closed)
| Change | Metric | Before | After |
|--------|--------|--------|-------|
| A (recal, OOS) ALL | Brier | 0.340 | **0.204** |
| A (recal, OOS) LIVE | Brier | 0.331 | **0.160** |
| B with-dead-signal | win rate | — | **0.0%** (n=15, Brier 0.424) |
| D BULL 6/1–6/7 | win rate | — | **4.3%** (n=140) |

These validate A and D strongly and confirm B as noise (low volume).

## How
- Reuses production calibration math (`build_recalibration_map`,
  `apply_recalibration`, `get_calibration_report` in
  `mcp-prediction-store/metrics.py`) rather than reimplementing it; the train
  slice is loaded into an in-memory DB so bucketing is identical to live.
- Time-ordered split mirrors `get_signal_decay` (`outcome_date, created_at, id`).

## Code locations
- `scheduler/backfill_eval.py` — harness (`_connect_readonly`, `load_closed`,
  `_metrics`, `eval_recalibration`, `eval_dead_signals`, `eval_regime_window`,
  `build_report`).
- `tests/test_backfill_eval.py` — 15 unit tests.

## Review loop (code-reviewer + codex + gemini)
- **Fixed — DB not read-only (codex, High):** the harness used
  `models.get_connection`, which migrates/indexes/WAL/dedup-self-heals (writes).
  Replaced with `_connect_readonly` (`file:...?mode=ro` URI). Honors the
  read-only contract.
- **Fixed — avg-return inflated ×100 (codex, Low but corrected a wrong claim):**
  `outcome_return` is stored as percentage points (range −91.65..+87.25); the
  `:+.2%` format spec multiplied by 100 (showed −865%). Now `:+.2f%`. **The
  earlier "corrupted data" note was wrong — the data is fine; it was a display
  bug.**
- **Fixed — invalid `oos_fraction` / tiny-n (codex + gemini):** added
  `0 < oos_fraction < 1` validation and an "insufficient data" guard in the
  report.
- **Fixed — per-horizon recalibration (gemini + code-reviewer):** exposed an
  optional `timeframe` on `eval_recalibration`/`build_report` + `--timeframe`
  CLI flag, so change A can compare global vs per-horizon maps (1W global Brier
  0.331→0.169 vs all-horizon 0.331→0.160).
- **Fixed — test gaps:** added invalid-fraction, empty-signal-list, custom
  dead-set, and `build_report` smoke tests (9 → 15).
- **Dismissed — in-memory schema "Critical" (code-reviewer):** all three
  reviewers confirmed `get_calibration_report` only reads
  confidence/status/source/timeframe; the 4-column temp table is exactly right.
- **Dismissed — strict point-in-time split (gemini/codex):** split is by
  `outcome_date` so test outcomes never enter the fitted map; the residual PIT
  effect is second-order on a stationary overconfidence bias and mirrors live
  recalibration. Documented in `eval_recalibration` rather than restructured.

## Known pre-existing issue (flagged, NOT fixed — out of scope)
- `scheduler/weekly_calibration.py:208,236` formats `avg_return` with `:+.2%`,
  the same ×100 inflation bug — the production weekly report overstates average
  return. Left for a separate, scoped fix.

## Retrospective
- Reusing the production recal builder via an in-memory DB kept the harness
  honest (no duplicated calibration logic that could drift from live).
- The three-reviewer loop earned its keep: codex caught the read-only contract
  violation and the formatting bug that had led me to wrongly blame the data.
  Lesson to carry forward: verify the *display path* before blaming the data.
