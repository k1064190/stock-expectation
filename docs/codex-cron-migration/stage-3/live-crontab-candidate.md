# Live Crontab Candidate

## Why

The installed crontab still contains active Claude briefing commands, obsolete Claude/API fallback blocks, an implicit Claude gold default, and an implicit discount collector default. Installing replacements before the feature branches are deployed would break the gold and discount jobs.

## What

- Backed up the 128-line live crontab before transformation and persisted both rollback and candidate files with mode `600`.
- Rendered a candidate with all three daily briefings set to `codex-cli`.
- Removed commented Claude and Anthropic API fallback blocks.
- Made gold use `--llm-mode codex-cli` and discount deals use `DEALS_COLLECTORS=codex` explicitly.
- Preserved all 12 active schedules, including the unrelated Gemini video pipeline and pure-Python jobs.
- Deferred installation until both repository changes are reviewed and deployed.

## How

The persistent backup is `/home/cwh/.local/state/codex-cron-migration/crontab.pre-codex-20260719` (`sha256: 1e57a283097abffb4bfe04e286bbbd9c93abf93764c044077bc3708e1a888bb3`) and the candidate is `/home/cwh/.local/state/codex-cron-migration/crontab.codex-candidate` (`sha256: 9f842e8ae82d1ab7f684fff19ec9e9b063b34e97974753de7db2e82c546435db`). `crontab -n` accepted the candidate. It contains no Claude/Anthropic/API-mode text, and the non-target active-command hash is identical in backup and candidate (`e61a6295e9ae3e7411bd9fc1d61a824cda276d54250275a03039abd5328fd64f`).

## Code locations

- `/home/cwh/.local/state/codex-cron-migration/crontab.pre-codex-20260719`
- `/home/cwh/.local/state/codex-cron-migration/crontab.codex-candidate`
- `scheduler/crontab.example`
- `scheduler/codex_runner.py`
- `src/discount_please/deals/collectors/codex.py`

## Retrospective

Separating render/validation from installation prevents a mixed-version cron failure. Antigravity's valid persistence finding moved the rollback/candidate files out of `/tmp`; its summary-index finding was dismissed because the Stage 3 summary entry already existed outside the initially scoped diff. Its final re-review was clean. Doctor Cho waived Claude review, and live installation remains gated on reviewed deployment.
