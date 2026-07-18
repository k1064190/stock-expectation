# Codex Cron Migration

## Stage 1 — Stock schedulers use one Codex execution path

- [stock-schedulers](stage-1/stock-schedulers.md) — Replaced scheduled Claude/API execution with a tested shared Codex runner.

## Stage 2 — Discount collection uses native Codex web search

- [discount-collector](stage-2/discount-collector.md) — Replaced the default Claude deals collector with a tested Codex search collector.

## Stage 3 — Live crontab candidate is validated and deployment-gated

- [live-crontab-candidate](stage-3/live-crontab-candidate.md) — Preserved every active schedule while rendering a Codex-only candidate for post-deployment installation.
