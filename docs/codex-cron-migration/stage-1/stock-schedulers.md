# Stock Scheduler Migration

## Why

Cron-triggered stock analysis still had executable Claude CLI and Anthropic API paths. The migration requires one explicit, testable Codex execution path so scheduled jobs cannot silently fall back to Claude.

## What

- Added a shared non-interactive Codex runner defaulting to `gpt-5.6-sol` with high reasoning effort while preserving the `CODEX_MODEL` override.
- Migrated daily briefing, deep dives, ISA briefing, gold trend, and news-bucket audit execution to that runner.
- Changed the capstone-readiness cron notification so its human handoff points to Codex.
- Removed scheduler Claude/API mode selection and the Anthropic optional dependency.
- Updated the crontab template and operator documentation to describe Codex-only execution.
- Added regression tests that reject executable Claude patterns and validate the exact Codex command contract.

## How

The tests were written first and failed against the prior Claude paths. The implementation then centralized subprocess behavior in `scheduler/codex_runner.py`, kept existing prompt and fail-open behavior where applicable, and removed only the obsolete execution branches. Verification covered 115 focused tests, CLI help/import checks, live read-only and workspace-write `gpt-5.6-sol` smoke calls, and the full offline suite of 958 tests; all passed.

## Code locations

- `scheduler/codex_runner.py`
- `scheduler/daily_briefing.py`
- `scheduler/deep_dive.py`
- `scheduler/isa_briefing.py`
- `scheduler/gold_trend.py`
- `scheduler/news_bucket_audit.py`
- `scheduler/capstone_readiness.py`
- `scheduler/crontab.example`
- `scheduler/tests/test_codex_runner.py`
- `scheduler/tests/test_cron_codex_migration.py`
- `pyproject.toml`
- `uv.lock`
- `README.md`
- `cron_setting.md`

## Retrospective

Centralizing the CLI contract made omissions mechanically detectable. Self-review found and fixed the stale capstone handoff. Antigravity's model-override finding was fixed; its `--full-auto` findings were dismissed after the current CLI completed an actual workspace-write shell call without prompting and did not list that flag. Antigravity's re-review was clean. Codex PR reviews found stale `--mode api` recovery guidance and inherited approval policy; both were fixed with regression tests, and an actual `-a never` smoke passed. Gemini's empty-model finding was fixed; its read-only deep-dive suggestion was dismissed because the job requires networked `stock-cli` calls, and replacement decoding was dismissed because it would hide malformed structured output. Doctor Cho explicitly waived the Claude review for this migration on 2026-07-19 because the local Claude CLI was not authenticated.
