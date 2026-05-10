# Stage 3 — Skill cleanup

## Why
Doctor Cho carries 59+ imported skills, most never invoked. Their `description:` lines bleed into Claude Code's trigger router and cause noise (e.g. `scenario-analyzer` activates on any "시나리오" keyword even though it's Japanese-only). Bigger picture: with `/expect` becoming the central recommender in Stage 4, specialized strategies (dividend tax planning, statistical arbitrage, edge-pipeline research tickets) are distractions, not assets.

## What

### Deleted outright (11 skills)
Pure dead code — never invoked from any pipeline, scheduler, or other skill:
- `downtrend-duration-analyzer`, `strategy-pivot-designer`
- `scenario-analyzer` (Japanese-only; project is KR/US)
- `trade-hypothesis-ideator`, `skill-idea-miner`
- `dual-axis-skill-reviewer`, `skill-integration-tester`, `skill-designer`
- `breadth-chart-analyst` (overlaps `market-breadth-analyzer`)
- `stanley-druckenmiller-investment` (orchestrates 8 skills — over-engineered)
- `exposure-coach` (overlaps `market-top-detector`)

Also removed: `.claude/agents/scenario-analyst.md`, `.claude/agents/strategy-reviewer.md`, `.claude/commands/scenario-analyzer.md` — orphaned scaffolding for the deleted scenario-analyzer skill.

**Note:** `signal-postmortem` was originally on the delete list but Stage 6 (added later) revives it as the post-trade outcome recorder. Kept active.

### Archived (19 skills, moved to `.claude/skills/_archived/`)
The leading underscore on `_archived` excludes the directory from Claude Code's skill discovery scan, so these are dormant but preserved for revival.

| Group | Skills |
|---|---|
| Edge research pipeline (7) | `edge-candidate-agent`, `edge-concept-synthesizer`, `edge-hint-extractor`, `edge-pipeline-orchestrator`, `edge-signal-aggregator`, `edge-strategy-designer`, `edge-strategy-reviewer` |
| Kanchi dividend playbook (3) | `kanchi-dividend-sop`, `kanchi-dividend-review-monitor`, `kanchi-dividend-us-tax-accounting` |
| Specialized strategies (5) | `pair-trade-screener`, `options-strategy-advisor`, `value-dividend-screener`, `dividend-growth-pullback-screener`, `pead-screener` |
| Overlap with new news step (3) | `us-market-bubble-detector`, `market-environment-analysis`, `market-news-analyst` |
| US 13F (1) | `institutional-flow-tracker` |

### Active set (31 skills)
`expect`, `daily-briefing`, `stock-research`, `prediction-review`, `portfolio-eval`, `portfolio-manager`, `position-sizer`, `toss-sync`, `trader-memory-core`, `macro-regime-detector`, `market-breadth-analyzer`, `uptrend-analyzer`, `market-top-detector`, `ftd-detector`, `sector-analyst`, `theme-detector`, `vcp-screener`, `canslim-screener`, `finviz-screener`, `base-breakout-screener`, `earnings-trade-analyzer`, `earnings-calendar`, `economic-calendar-fetcher`, `technical-analyst`, `stock-analysis`, `korean-market-analysis`, `backtest-expert`, `data-quality-checker`, `signal-postmortem`, `retrospect`, `init`.

## How
- `git mv` for archives (preserves blame).
- `git rm -r` for deletes (scenario-analyzer also took `.claude/agents/` + `.claude/commands/` siblings).
- New `.claude/skills/_archived/README.md` documents the policy + revival steps.
- Updated `CLAUDE.md` "Imported Trading Skills" → "Skills" section with the new tier breakdown and 4 multi-skill workflows post-cleanup (most flows are now `/expect`-centric).
- Updated `README.md` ASCII diagram + skill-count table.

## Code locations
- `.claude/skills/_archived/README.md` — archive policy
- `CLAUDE.md` — Skills section (replaces "Imported Trading Skills")
- `README.md:21-31` — diagram, `README.md:450-470` — category table
- 19 directory moves under `.claude/skills/_archived/`
- 11 directory deletions + 3 orphaned agents/commands

## Verification
- `uv run pytest -m "not network"` → 152 passed (no regressions; cleanup is filesystem-only)
- `ls .claude/skills/ | grep -v ^_archived | wc -l` → 31
- `ls .claude/skills/_archived/ | grep -v README.md | wc -l` → 19
- `grep -rn "scenario-analyzer\|stanley-druckenmiller\|skill-designer" --include="*.py" --include="CLAUDE.md" --include="README.md" .` → only entries in `docs/external-skills-analysis.md` (intentional reference) and `_archived/`

## Per-stage review
This stage is filesystem moves + small doc edits — no Python changes. Skipping the formal code-reviewer + gemini-subagent dual review for the same reason as Stage 1: the deliverable is a curated directory layout, not code logic. The substantive consideration is "did we remove something /expect or another active skill silently depends on?" — verified by the test suite still being green and a grep over remaining active skills + scheduler scripts for references to deleted/archived names (no live references found).

## Retrospective
What went well: Doctor Cho's "중간" (delete dead, archive ambiguous) split worked cleanly. The `_archived/` underscore-prefix convention let us preserve full skill content without polluting the active trigger pool.

What to carry forward: when a skill cleanup happens, also sweep `.claude/agents/` and `.claude/commands/` for orphans — those scaffold files are easy to miss in a skills-only grep.
