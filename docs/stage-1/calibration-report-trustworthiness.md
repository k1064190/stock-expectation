# Stage 1 — Make the weekly calibration report trustworthy

## Why

The weekly calibration report (`reports/weekly-calibration-*.md`) drives operator
decisions, but two structural flaws made its advice misleading:

1. **No windowing.** `compute_window` windowed only the headline track record;
   the calibration buckets and signal-performance tables were computed over the
   **full history** and repeated identically under every window header. The
   "7-day window" section showed all-time data (e.g. `technical n=445`), hiding
   that recent, post-momentum-overhaul signals had recovered (7-day `technical`
   ~52% vs all-history ~38%).
2. **Blind to recalibration.** The buckets were always computed on **raw**
   confidence (`COALESCE(raw_confidence, confidence)`), so the report could never
   show whether the live isotonic recalibrator (`recalibrate_confidence`) was
   closing the overconfidence gap. Its headline warning ("output confidence
   should be trimmed") recommended a manual trim that would **double-correct**
   the recalibrator — which already maps raw 0.61 → ~0.31 on recalibrated rows.

Doctor Cho chose "make the report trustworthy" over applying the report's literal
(and counter-productive) recommendations.

## What

- `get_calibration_report` and `get_signal_performance` gained an optional
  `days` window filter (on `outcome_date`, matching `get_track_record`).
- `get_calibration_report` gained `use_raw` (default `True` = raw, preserving the
  recalibration-training path; `False` = as-logged/post-recalibration confidence).
- The report now renders **two calibration tables per window** (raw vs as-logged)
  when they diverge, collapsing to one table + a note while they still coincide.
- The headline overconfidence warning is now based on the **as-logged** curve
  (what actually ships) and reworded to stop recommending double-corrections; the
  raw flags are retained in `overconfident_buckets_raw` (also in the trend file
  for plotting the gap over time).

Result on live data: the 7-day window now shows raw drift +0.09 → as-logged +0.02
(recalibrator closing the gap on recent data), and signal tables differ per
window, surfacing the recent recovery the old report hid.

## How

Test-first (TDD): 7 new failing tests (4 in `test_metrics.py`, 3 in
`test_weekly_calibration.py`) drove minimal additions. The windowing reuses the
exact `outcome_date >= datetime('now', ?)` predicate `get_track_record` already
uses, so the windowed sections align with the headline. `use_raw` only swaps the
selected confidence column; the bucketing math is untouched. Rendering was
refactored to extract `_render_bucket_table` (removing the duplication the second
table would have introduced).

## Code locations

- `mcp-prediction-store/metrics.py`
  - `get_calibration_report` — added `days`, `use_raw` (≈467-540)
  - `get_signal_performance` — added `days` (≈540-585)
- `scheduler/weekly_calibration.py`
  - `compute_window` — windowed helpers + dual raw/as-logged buckets + warning basis
  - `_render_bucket_table` (new helper) + two-table render logic in `render_markdown`
  - headline warning wording; `overconfident_buckets_raw` in the trend entry
- Tests: `mcp-prediction-store/tests/test_metrics.py`,
  `scheduler/tests/test_weekly_calibration.py`

## Review loop

- **code-reviewer-pro**: 0 critical / 0 warnings, 1 suggestion — the two-table
  render path would emit an empty as-logged table when `recal_buckets` is empty.
  Fixed: the render now requires a non-empty `recal_buckets` to show two tables;
  an empty list (recalibration pulled all as-logged confidence below the 0.50
  bucket floor — likely, since observed avg as-logged ≈ 0.31) renders one table +
  an explanatory note. New test `test_render_markdown_notes_when_recal_falls_below_bucket_floor`.
- **Codex (gpt-5.5, high)**: [MAJOR] — the `outcome_date >= datetime('now', ?)`
  window compared ISO strings lexicographically (the `T` separator sorts after a
  space), wrongly including same-cutoff-date rows. Fixed by wrapping with
  `datetime(outcome_date)` in the new queries; applied to the shared
  `_closed_filter` helper and `get_track_record` too so the whole module is
  consistent (the headline + robustness sections already shared the latent bug).
  New boundary test `test_calibration_report_window_excludes_same_cutoff_date_earlier_time`.
  Re-review verdict: **RESOLVED, no new material issues**.
- **Gemini (gemini-subagent)**: could not run — the installed Gemini CLI returns
  `IneligibleTierError` (free-tier client deprecated by Google), an environment
  limitation, not a skipped check.

## Retrospective

- Verifying the report's premise before acting paid off: the data showed the
  "trim confidence" advice would have double-corrected an existing recalibrator.
  Carry forward: treat advisory reports as hypotheses, confirm against the DB.
- The `days` window deliberately reuses `outcome_date` semantics so all sections
  agree; `get_signal_decay` stays full-history by design (it is an OOS split).
- Follow-on to watch: as the 722 open recalibrated LIVE predictions close, the
  30-day as-logged curve should diverge from raw and converge toward 0 drift —
  the new dual-table view is exactly how to confirm that without code changes.
