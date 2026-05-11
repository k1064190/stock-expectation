# Archived skills

Skills moved here are **not loaded** by Claude Code (the `_` prefix excludes the directory from the skill discovery scan). They're preserved verbatim — full SKILL.md, scripts, references — so they can be revived without rewriting.

## Why archive instead of delete?
- Specialized domain (dividend tax planning, options spreads, statistical arb) — useful in the right context, distracting in `/expect`-centric flows.
- Pipeline scaffolding that pre-supposes a much larger research operation than Doctor Cho currently runs.
- Overlap with `/expect`'s new built-in news step.

## How to revive a skill
1. `git mv .claude/skills/_archived/<skill> .claude/skills/<skill>`
2. Verify `.claude/skills/<skill>/SKILL.md` frontmatter trigger keywords don't conflict with active skills (`grep -r '^description:' .claude/skills/`).
3. Commit and reload Claude Code.

## What's in here

### Edge research pipeline (7 skills)
A multi-stage research-ticket workflow for converting market observations into reproducible strategy YAML. Powerful but never run end-to-end in Doctor Cho's day-to-day flow.
- `edge-candidate-agent`, `edge-concept-synthesizer`, `edge-hint-extractor`, `edge-pipeline-orchestrator`, `edge-signal-aggregator`, `edge-strategy-designer`, `edge-strategy-reviewer`

### Kanchi dividend playbook (3 skills)
Japanese-style dividend investing methodology adapted for US tax law. Keep for the case where the active strategy shifts toward income.
- `kanchi-dividend-sop`, `kanchi-dividend-review-monitor`, `kanchi-dividend-us-tax-accounting`

### Specialized strategy screeners (5 skills)
Each screens for a specific setup. Resurface when the setup matters.
- `pair-trade-screener` (statistical arb)
- `options-strategy-advisor` (Black-Scholes + Greeks)
- `value-dividend-screener` (P/E + P/B + yield + 3y growth)
- `dividend-growth-pullback-screener` (RSI-oversold dividend growers)
- `pead-screener` (overlaps `earnings-trade-analyzer`; archive the narrower one)

### News/regime overlap (3 skills)
Each does a flavor of news + macro analysis that `/expect`'s new news step + `macro-regime-detector` together cover.
- `us-market-bubble-detector`, `market-environment-analysis`, `market-news-analyst`

### US 13F (1 skill)
Requires SEC EDGAR scraping pipeline we don't have.
- `institutional-flow-tracker`
