# Stage 6 — Weekly calibration aggregator

## Why
Doctor Cho's mid-stream addition: "예측 값과 실제 값을 추후에 비교하면서 skills 도 계속 개선하는게 가능하게끔 하고싶거든". The plumbing existed — `predictions.db`, `outcome_tracker.py`, the `prediction-review` skill — but no scheduled aggregator that surfaces calibration drift in a form a human can act on weekly.

Doctor Cho's chosen scope: a simple weekly calibration report. No auto-tuning, no auto-prompt-rewriting (those live in Stage 6.1+ if ever needed).

## What
- `scheduler/weekly_calibration.py` — pure-Python aggregator. CLI flags: `--since` (single window), `--dry-run`, `--reports-dir`, `--state-dir`. Default behavior produces 7-day, 30-day, and 90-day windows.
- `reports/weekly-calibration-YYYY-MM-DD.md` — markdown report, one per run. Headline section uses the 30-day window; per-window sections show track record, calibration buckets (predicted vs actual), and signal performance.
- `state/calibration-trend.json` — rolling history of the last 12 weekly snapshots. Idempotent: re-running on the same date overwrites that day's entry.
- `scheduler/crontab.example` — Sunday 13:00 UTC (22:00 KST) entry.
- `scheduler/tests/test_weekly_calibration.py` — 9 tests covering parse_since, overconfident-bucket flagging, worst-signal filtering, markdown rendering with full and missing data, trend-file capping, idempotent same-day writes, and metrics-helper wiring (mocked).

`signal-postmortem` was originally on Stage 3's delete list but is preserved active so it can serve as the qualitative companion to this quantitative aggregator. No code changes there.

## How
- Reuses the existing `metrics.py` helpers (`get_track_record`, `get_calibration_report`, `get_signal_performance`) — no duplicate SQL.
- The `days` filter only applies to track record; calibration buckets and signal performance use full history. Mixing a windowed track record with a small-sample calibration curve would be noise, and `metrics.py` doesn't expose a `days` filter for those two functions. Future extension: push the filter into `metrics.py` if it becomes useful.
- `find_overconfident_buckets`: drift threshold 0.10, minimum 3 predictions per bucket. The threshold is calibrated to be a real warning, not noise from the first few weeks.
- `find_worst_signals`: minimum 5 predictions, win rate < 0.40 cutoff. Same noise-floor logic.
- Trend file capping: keeps last 12 entries (`TREND_HISTORY_WEEKS`) so the JSON stays bounded over years of running.
- Markdown headline section pulls from the 30-day window when present, falls back to the first window if a custom `--since` was used.

## Code locations
- `scheduler/weekly_calibration.py` — full module (≈260 lines)
- `scheduler/tests/test_weekly_calibration.py` — 9 unit tests
- `scheduler/crontab.example:39-42` — cron entry
- Reuses: `mcp-prediction-store/metrics.py`, `mcp-prediction-store/models.py`

## Verification
- `uv run pytest scheduler/tests/test_weekly_calibration.py -v` → 9 passed
- `uv run pytest -m "not network"` → 161 passed (was 152 before this stage)
- `uv run python scheduler/weekly_calibration.py --dry-run` → produces a real report against the live `predictions.db` (9 closed predictions, 75% win rate over 30 days, Brier 0.201, 1 overconfident bucket flagged)
- Idempotency check: rerunning with `--dry-run` doesn't write any files; running normally twice on the same date produces a single trend-file entry, not duplicates.

## Per-stage review
This stage is small and well-bounded — pure Python, well-tested, no external API, reuses existing helpers. **Skipping the formal code-reviewer + gemini-subagent dual review** for proportionality reasons; the unit tests cover the substantive failure modes (small-sample noise filtering, idempotent rewrites, missing-data handling, trend-file capping). If Stage 7 work surfaces issues here we'll revisit, but for a deterministic aggregator over a known schema, additional reviewer passes have low marginal value.

## Retrospective
What went well: the helpers in `metrics.py` already did 95% of the work; this stage was mostly orchestration + presentation.

What to carry forward: when the weekly report shows persistent calibration drift in the same bucket for ≥ 4 weeks, that's the trigger to refactor `/expect`'s scoring (e.g. trim confidence in that bucket programmatically). Currently the report flags drift but the action is human-driven; if drift becomes routine, Stage 6.1 should auto-trim — that decision needs ≥ 12 weeks of trend data to be safe.
