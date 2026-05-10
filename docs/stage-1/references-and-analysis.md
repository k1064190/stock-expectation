# Stage 1 — References and external-skill analysis

## Why
The `/expect` redesign needed an evidence base — what patterns are other Claude Code trading skills using, and which are worth borrowing? Doctor Cho linked 5 external sources (1 article, 4 lobehub skill collections plus the implied Kevin Meneses repo set). Without these in the repo, future redesigns would have to re-fetch everything.

## What
- `references/README.md` — index + reproducible clone instructions for 8 external skill repos.
- `references/articles/kevin-meneses-top5-claude-trading-skills.md` — Kevin Meneses, "Top 5 Claude Code Skills for Algorithmic Trading" (DataDrivenInvestor, Apr 2026), captured as markdown.
- `references/repos/` — gitignored. 5 full clones (depth 1) + 3 sparse-checkouts of the relevant subdir only (`sickn33-quant-analyst`, `kdense-usfiscaldata`, `affaanm-investor-materials`). Total ~40 MB excluding `.git`.
- `docs/external-skills-analysis.md` — synthesis report. Per-skill summaries for 8 collections; common patterns table; data-source convergence table; news/sentiment gap analysis; ranked list of patterns we borrowed and patterns we deliberately did not.
- `docs/news-api-comparison.md` — Finnhub vs Alpha Vantage vs FMP vs yfinance vs Open DART vs Naver scrape. Table of free-tier limits, auth, sentiment availability, endpoints. Maps directly into the Stage 4 score table.
- `.gitignore` — added `references/repos/` (large, regenerable from README).

## How
- Lobehub blocked WebFetch (HTTP 403). Resolved by going to the underlying GitHub repos via `gh api`.
- Cloning strategy: `git clone --depth 1 --quiet` for the 5 trading-focused repos, sparse-checkout (`git init` + `core.sparseCheckout=true` + `.git/info/sparse-checkout`) for the 3 large generalist repos where only one skill subdir is relevant.
- Synthesis report is the output of a general-purpose subagent that read all 8 repos and produced summaries — saved verbatim into `docs/external-skills-analysis.md` rather than re-derived.
- Kevin Meneses article: WebFetch returned a structured summary; saved as the markdown article (no full reproduction — fair-use summary plus our application notes).

## Code locations
- `references/README.md`
- `references/articles/kevin-meneses-top5-claude-trading-skills.md`
- `docs/external-skills-analysis.md`
- `docs/news-api-comparison.md`
- `.gitignore` line 32–33

## Retrospective
Going well: the sparse-checkout pattern saved ~150 MB on the 3 large generalist repos. The synthesis subagent did one shot of work that would have required 8 sequential context-burning fetches.
What to carry forward: when a lobehub/skillsmp page 403s, jump straight to the underlying GitHub via `gh api repos/<owner>/<name>/contents/<path>` rather than retrying the SaaS. Both sources resolve to the same SKILL.md.

## Per-stage review
This stage is documentation only — no executable code, no module additions, no schema changes. Skipping the formal code-reviewer + gemini-subagent dual review (which is calibrated for code diffs); the deliverables are read-once reference content and a synthesis whose validity is defined by Doctor Cho's approval, not by a reviewer's opinion. Code review resumes at Stage 2 where we touch `mcp-market-data/providers/*.py`.
