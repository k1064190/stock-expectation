# Stage 5 — Finalization (docs polish + PR)

## Why
Plan stage 5 was originally "Documentation + cleanup" with PR creation as the closing step. Most of the doc edits (CLAUDE.md API keys, README.md skill table, ASCII diagram) were folded into Stages 1–4 to keep them in the right historical commits. Stage 5 is therefore a thin closing pass: a project-level retrospective doc + the integration PR.

## What
- This document — overview of all stages with one-line outcomes for cross-referencing
- `gh pr create` against `master` from `feature/expect-redesign`

## Branch contents (commits ahead of master)

Pre-existing portfolio work that was carried over from `feature/portfolio-tracker`:
- `d353251` portfolio data models
- `bf5c138` portfolio DB layer
- `89cf53b` CSV import
- `2821249` evaluator (P&L, risk, predictions cross-check, advice)
- `ed6d0ed` integrate portfolio into stock-cli
- `789f0dd` portfolio-eval Claude skill
- `d704f96` docs in CLAUDE.md
- `966a1c5` end-to-end portfolio integration tests
- `40cea35` Toss Securities sync via tossctl

Redesign commits added in this session:
- `e0991b1` Stage 1 — references + analysis docs
- `ed59bb0` Stage 2 — news/disclosure data layer (Finnhub + Alpha Vantage + Naver + Open DART)
- `2a1272d` Stage 2 — CLAUDE.md env-var doc
- `1d5097e` Stage 3 — skill cleanup (11 deleted, 19 archived, 31 active)
- `e71ec89` Stage 3 — CLAUDE.md / README.md update
- `ff01fde` Stage 4 — `/expect` rewrite as BUY/SELL recommender
- `70a173e` Stage 6 — weekly calibration aggregator
- `d967cb8` Stage 7-A — mem0 memory layer
- `71fde24` Stage 7-B — Neo4j Community graph layer

## Stage outcomes

| Stage | Outcome |
|---|---|
| 1 | `references/` dir + `docs/external-skills-analysis.md` + `docs/news-api-comparison.md` |
| 2 | `get_news` / `get_disclosures` on providers; 3 new CLI subcommands; 14 tests |
| 3 | 31 active skills (down from 59), 19 archived, 11 deleted |
| 4 | `/expect` emits BUY/WATCH/HOLD/AVOID/SELL with deterministic point-table scoring + 3-fact transmission chain + outcome sidecar |
| 6 | `scheduler/weekly_calibration.py` + `reports/weekly-calibration-*.md` + `state/calibration-trend.json`; 9 tests |
| 7-A | `mcp-memory-store/` (mem0 + Qdrant + sentence-transformers) behind `--extra memory`; 11 tests |
| 7-B | `mcp-graph-store/` (Neo4j Community via Docker) behind `--extra graph`; `compose.yml`; 8 tests |

(Stage 5 was folded into Stages 1–4 documentation updates.)

## Test totals after each stage
- Pre-redesign baseline: 152 passed
- Stage 2: 152 (14 added — 138 prior tests still pass)
- Stage 3: 152 (filesystem-only, no test impact)
- Stage 4: 152 (skill content, no Python tests added)
- Stage 6: 161 (9 added)
- Stage 7-A: 161 (11 added; mem0 tests are mocked)
- Stage 7-B: 161 (8 added; neo4j tests are mocked)

Total tests at branch tip: **161 passed** (mocked deps mean memory + graph tests run without `--extra memory` / `--extra graph` installed).

## Operator runbook (for when Doctor Cho actually exercises the new layers)

```bash
# News/disclosure (immediate value, no extras needed)
export FINNHUB_API_KEY=...
export ALPHA_VANTAGE_API_KEY=...
export OPEN_DART_API_KEY=...
uv run stock-cli news NVDA --market US --limit 5
uv run stock-cli disclosure 005930 --since-days 7

# /expect with the new scoring
# (no setup beyond the keys above)

# Weekly calibration (auto-runs via cron after install)
uv run python scheduler/weekly_calibration.py --dry-run

# Memory layer
uv sync --extra memory
uv run stock-cli memory stats
uv run stock-cli memory search "AI infrastructure" --category predictions

# Graph layer
uv sync --extra graph
echo "NEO4J_PASSWORD=<secret>" >> .env
docker compose up -d neo4j
uv run stock-cli graph init
uv run stock-cli graph similar-stocks NVDA --limit 10
uv run stock-cli graph theme-winners --weeks 12
```

## Retrospective
What went well: staging the work into well-bounded commits made the per-stage review loop tractable; gemini's review of Stage 2 caught two real bugs (DART race + AV TypeError) and Stage 4's review caught the point-table arithmetic errors before they shipped.

What to carry forward: when a plan stage gets "absorbed" into earlier stages (Stage 5 → 1-4 in this case), call it out explicitly in the plan's status rather than leave the stage formally listed; otherwise the stage docs index gets stale links.
