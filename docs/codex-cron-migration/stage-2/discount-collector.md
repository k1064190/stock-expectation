# Discount Collector Migration

## Why

The live discount cron still defaulted to a Claude CLI collector, so changing only the stock schedulers would leave one installed Claude execution path active.

## What

- Replaced `ClaudeCollector` and the `claude` registry key with `CodexCollector` and `codex`.
- Made `codex` the default `DEALS_COLLECTORS` value and updated the generated README template.
- Enabled native Codex live web search with explicit non-interactive approval, read-only sandboxing, ephemeral sessions, closed stdin, and the existing provider timeout/concurrency bounds.
- Preserved one-call-per-provider coverage, official event-page anchoring, per-provider failure isolation, and grounding-document output.
- Removed Claude-specific JSON-envelope parsing and `WebFetch` prompt wording.
- Added regression tests for the exact Codex command, model override, registry/config defaults, failures, concurrency behavior, and absence of Claude source paths.

## How

Tests first failed because the Codex collector did not exist. The implementation kept the existing collector structure and changed only the CLI boundary and Claude-specific output handling. Worktree tests explicitly used `PYTHONPATH=src` because the shared micromamba environment's editable install points at the original checkout. Verification passed 263 tests with 1 existing live test skipped, compile/static checks, the original read-only Codex web-search smoke that returned one parseable document containing 51 deals, and an isolated follow-up smoke that returned one 20,237-byte document with user config and local shell access disabled.

## Code locations

- `src/discount_please/deals/collectors/codex.py`
- `src/discount_please/deals/collectors/claude.py` (removed)
- `src/discount_please/deals/run.py`
- `src/discount_please/config.py`
- `src/discount_please/deals/extract.py`
- `src/discount_please/prompts/deals_grounding.j2`
- `src/discount_please/templates/index.md.j2`
- `tests/test_deals_codex.py`
- `tests/test_codex_migration.py`
- `tests/test_deals_extract.py`
- `tests/test_deals_run.py`
- `tests/test_config.py`

## Retrospective

The real smoke tests confirmed that native Codex search preserves the structured collector contract. Antigravity's valid configuration-flow finding was fixed; approval, TOML, prompt, legacy-env, and PATH findings were dismissed with CLI smoke/help and live deployment evidence. Its final re-review was clean. Gemini's duplicate-failure-log and timeout-traceback findings were fixed with regression tests. Codex follow-ups found user-config inheritance, local shell exposure, and legacy collector-value outage risk; `--ignore-user-config`, `--disable shell_tool`, and a one-way legacy-value-to-`codex` normalization fixed all three without restoring a Claude execution alias. Doctor Cho waived Claude review for this migration.
