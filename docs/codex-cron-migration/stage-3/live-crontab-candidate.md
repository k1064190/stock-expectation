# Live Crontab Deployment

## Why

The live crontab needed to move to Codex only after both repository implementations were reviewed, merged, and deployed, while retaining every unrelated schedule and a verified rollback point.

## What

- Backed up the 128-line live crontab before transformation and persisted both rollback and candidate files with mode `600`.
- Rendered a candidate with all three daily briefings set to `codex-cli`.
- Removed commented Claude and Anthropic API fallback blocks.
- Made gold use `--llm-mode codex-cli` and discount deals use `DEALS_COLLECTORS=codex` explicitly.
- Preserved all 12 active schedules, including the unrelated Gemini video pipeline and pure-Python jobs.
- Merged and deployed stock commit `bb4c6186a7d9bcef045aade1810e5776e80605c3` and discount commit `17671c3b0b0132183b5d135f46a7c6c9f2864b41`.
- Installed the candidate on 2026-07-20 KST after confirming the live crontab still matched the backup byte for byte.
- Verified the installed crontab exactly matches the candidate, retains 12 active jobs, and contains no Claude, Anthropic, or `--mode api` references.

## How

The persistent backup is `/home/cwh/.local/state/codex-cron-migration/crontab.pre-codex-20260719` (`sha256: 1e57a283097abffb4bfe04e286bbbd9c93abf93764c044077bc3708e1a888bb3`) and the installed candidate is `/home/cwh/.local/state/codex-cron-migration/crontab.codex-candidate` (`sha256: 9f842e8ae82d1ab7f684fff19ec9e9b063b34e97974753de7db2e82c546435db`). `crontab -n` accepted the candidate immediately before installation. A fresh `crontab -l` after installation produced the candidate's exact SHA-256 and showed three Codex daily briefings, one Codex gold job, and one explicit Codex discount job. Rollback remains `crontab /home/cwh/.local/state/codex-cron-migration/crontab.pre-codex-20260719`.

The deployed stock checkout passed `960` offline tests with `27` network tests deselected; the deployed discount checkout passed `263` tests with one credential-dependent test skipped. Both local base branches matched their remotes after deployment, and pre-existing user changes remained intact.

## Code locations

- `/home/cwh/.local/state/codex-cron-migration/crontab.pre-codex-20260719`
- `/home/cwh/.local/state/codex-cron-migration/crontab.codex-candidate`
- `scheduler/crontab.example`
- `scheduler/codex_runner.py`
- `src/discount_please/deals/collectors/codex.py`

## Retrospective

Separating render/validation from installation prevented a mixed-version cron failure. Antigravity's valid persistence finding moved the rollback/candidate files out of `/tmp`; its summary-index finding was dismissed because the Stage 3 summary entry already existed outside the initially scoped diff. Its final candidate re-review was clean. Doctor Cho waived Claude review. The reviewed repository deployments and exact post-install hash check closed the live-installation gate successfully. Post-deployment documentation self-review found and fixed a stale Candidate card title; Antigravity's final exact-diff review was clean.
