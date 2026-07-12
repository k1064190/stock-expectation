# Stage 6 — Monthly LLM audit of news keyword buckets

## Why

The catalyst/risk matchers are hand-maintained keyword lists with a documented
live false positive (2026-06 "blockade": a ballot-counting-site blockade
tripped the war_conflict macro bucket and wrongly trimmed every pick). The
stage-24 retrospective said "the keyword lists will need occasional curation"
— this job makes that curation systematic instead of reactive.

## What

`scheduler/news_bucket_audit.py` — monthly report-only job: samples recent
live headlines (recently-predicted tickers via each market's provider + the
macro wire), annotates them with the CURRENT matcher verdicts
(`EVENT_KEYWORDS`, hard catalysts, `MACRO_RISK_BUCKETS`), and has one
`claude -p` call judge false positives, likely misses, per-family precision,
and suggested keyword edits. Output: `reports/news-bucket-audit-YYYY-MM-DD.md`.
**Never edits the tables** — the human applies accepted suggestions via PR.
Monthly crontab entry (1st, 20:00 KST).

## How

TDD (5 tests). Reuses `deep_dive._call_claude`; sampling is fail-open per
source; headlines are fenced as untrusted data in the auditor prompt.

## Code locations

- `scheduler/news_bucket_audit.py` — full module
- `scheduler/tests/test_news_bucket_audit.py`
- `scheduler/crontab.example` — monthly entry

## Verification

- `uv run pytest -m "not network"` — 948 passed (5 new)

## Review loop

- **Antigravity (Gemini 3.1 Pro High)**: 3 low — `</sample>` tag-breakout via
  hostile headline (fixed: defang + newline flatten + regression test),
  provider construction not fail-open in main() (fixed), unbounded macro
  sample (fixed: slice 20). SQL parameterization, fail-open loops, and matcher
  reuse confirmed good.
- **code-reviewer subagent**: no critical; 1 warning — late `_call_claude`
  import (fixed: moved to module level); 1 suggestion — main() integration
  test (dismissed: thin orchestration, consistent with the other scheduler
  entry points which are also unit-tested only).
- **Codex**: quota-exhausted — deferred to the PR bot round.

## Retrospective

The audit reuses the exact production matchers, so its false-positive verdicts
are directly actionable against the real tables rather than a re-implementation.
