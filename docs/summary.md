# Stage Documentation Index

One entry per stage document under `docs/stage-N/`. When looking for prior work,
read this index first, then open only the linked stage files that match.
(No `stage-10` or `stage-18` directories exist.)

## Stage 0 — Benchmarking external trading repos for improvements
- [external-repo-benchmark](stage-0/external-repo-benchmark.md) — Cloned and analyzed 3 external trading repos, synthesized 13 improvement candidates (S1–S13); only Tier 1 approved for Stage 1.

## Stage 1 — Signal rigor, candidate discovery, Toss API
- [calibration-report-trustworthiness](stage-1/calibration-report-trustworthiness.md) — Added per-window filtering and raw-vs-recalibrated dual tables so the weekly calibration report stops recommending double-corrected confidence trims.
- [candidate-discovery](stage-1/candidate-discovery.md) — New `candidate_discovery.py` dynamically discovers KR candidates from cap∪trading-value universe with momentum/volume filters, replacing 6 hardcoded blue chips.
- [references-and-analysis](stage-1/references-and-analysis.md) — Cloned 8 external skill repos and wrote synthesis docs (external-skills-analysis, news-api-comparison) as the evidence base for the redesign.
- [tier1-signal-gate-recal](stage-1/tier1-signal-gate-recal.md) — Added signal binomial p-value/verdict (S1), risk/edge + BEAR prompt gates (S2), and isotonic confidence recalibration map (S3).
- [toss-open-api](stage-1/toss-open-api.md) — New `toss_api.py` OAuth2 client uses the official Toss Open API for portfolio sync, removing the tossctl session-expiry monitor.

## Stage 2 — News data layer plus prediction-store fixes
- [bear-sign-gate-dedup](stage-2/bear-sign-gate-dedup.md) — Fixed BEAR return sign, added store-level LIVE-BEAR/6-key dedup gates, and backfilled the DB (920→732 rows, 38 signs flipped).
- [news-and-disclosure-data-layer](stage-2/news-and-disclosure-data-layer.md) — Added `get_news`/`get_disclosures` to providers (Finnhub+Alpha Vantage / Naver+Open DART) and three CLI subcommands with 14 tests.
- [theme-clusterer](stage-2/theme-clusterer.md) — New `theme_clusterer.py` clusters candidate news via cross-ticker n-grams to surface named KR themes into the briefing prompt.

## Stage 3 — Static universe fallback, skills, staged debate
- [kr-universe-fallback](stage-3/kr-universe-fallback.md) — Added 166-ticker static KR universe CSV fallback for the broken PyKRX bulk endpoint plus seven Codex/Copilot review fixes.
- [skill-cleanup](stage-3/skill-cleanup.md) — Deleted 11 dead skills and archived 19 under `_archived/`, leaving 31 active `/expect`-centric skills.
- [tier2-oos-staged-debate](stage-3/tier2-oos-staged-debate.md) — Added OOS signal-decay detection (S5) and staged Quant→Director→Bull/Bear/Judge adversarial debate roles (S4/S6) in prompts.

## Stage 4 — /expect recommender and statistical validation
- [expect-rewrite](stage-4/expect-rewrite.md) — Rewrote `/expect` with deterministic algo/news point tables, a single BUY/WATCH/HOLD/AVOID/SELL label, 3-fact transmission chain, and outcome sidecar.
- [tier3-bootstrap-permutation-validation](stage-4/tier3-bootstrap-permutation-validation.md) — Added bootstrap win-rate/Brier CI (S7), confidence permutation test (S8), and prediction JSON schema validation (S9).

## Stage 4.1 — Live end-to-end /expect defect patches
- [e2e-followups](stage-4.1/e2e-followups.md) — Tracked untracked `indicators.py`, exposed the volume metric to `/expect`, added AV sentiment URL diagnostics, and enforced sidecar key contracts.

## Stage 5 — Finalization and US discovery mirror
- [finalization](stage-5/finalization.md) — Wrote the project-wide retrospective (stage outcomes, test totals, operator runbook) and opened the integration PR against `master`.
- [us-mirror](stage-5/us-mirror.md) — Ported dynamic candidate discovery and theme clustering to US: `us_universe.csv` (135 tickers), `ANCHORS_US`, `discover_us_candidates`, dollar-cap formatting.

## Stage 6 — Theme-cluster noise filter and weekly calibration
- [themes-noise-reduction](stage-6/themes-noise-reduction.md) — Added a `min_distinct_articles` gate to `cluster_news` so single-article echoes and sliding-window trigrams stop forming fake theme clusters.
- [weekly-calibration](stage-6/weekly-calibration.md) — New `weekly_calibration.py` aggregator emitting 7/30/90-day markdown calibration reports plus a rolling trend JSON, cron-scheduled Sundays.

## Stage 7 — Vector memory and Neo4j graph storage layers
- [a-memory-layer](stage-7/a-memory-layer.md) — Added `mcp-memory-store` mem0 semantic-recall layer (Qdrant + local embeddings) with a `memory` CLI subcommand behind an optional extra.
- [b-graph-layer](stage-7/b-graph-layer.md) — Added `mcp-graph-store` Neo4j Community graph layer (Docker compose + idempotent ingestion) with a `graph` CLI subcommand behind an optional extra.

## Stage 8 — Anti-momentum-bias LLM context score
- [llm-context-score](stage-8/llm-context-score.md) — Added a third LLM_CONTEXT_SCORE component (−5.0 to +3.0) to `/expect` and daily-briefing that vetoes momentum-only BUYs when macro context disagrees.

## Stage 9 — codex-cli briefing mode dodging claude -p throttling
- [codex-cli-mode](stage-9/codex-cli-mode.md) — Added `--mode codex-cli` routing briefing prompts through `codex exec` instead of throttled `claude -p`, and made it the cron default.

## Stage 11 — Prediction-accuracy initiative: recalibration, gates, features
- [backfill-harness](stage-11/backfill-harness.md) — Added a read-only `backfill_eval.py` A/B harness validating recalibration, dead-signal, and regime changes on past closed predictions before touching live flow.
- [component-persistence](stage-11/component-persistence.md) — Added a nullable `components` JSON column and `component-contribution` metric so each pillar's per-prediction contribution is stored for a future learned blend.
- [leakage-audit](stage-11/leakage-audit.md) — Audited the 42pt LIVE-vs-INTERACTIVE win-rate gap, found regime plus selection (not leakage), and added a `by_source` track-record breakdown.
- [llm-context-rigor](stage-11/llm-context-rigor.md) — Added a `validate_llm_context` rigor gate and `lint-llm-context` CLI that dampen LLM_CONTEXT scores lacking cited evidence, a genuine bear case, or valid range.
- [news-features](stage-11/news-features.md) — Added `news_features.py` producing deduped, recency-weighted sentiment and catalyst event tags, replacing raw headline-volume news scoring.
- [overextension-gate](stage-11/overextension-gate.md) — Added a per-stock `overextension_level` and RULE R2 suppressing BULL calls on parabolic names (EXTREME won 29% vs NONE 53.5%).
- [recalibration-and-dead-signals](stage-11/recalibration-and-dead-signals.md) — Added a `--recalibrate` flag applying the source-scoped isotonic map (raw confidence stored separately) and pruned 0%-win dead signals from both skills.
- [regime-gate](stage-11/regime-gate.md) — Added a `stock-cli regime` deterministic RISK_ON/NEUTRAL/RISK_OFF verdict and RULE R1 hard gate suppressing BULL calls in risk-off windows.

## Stage 12 — Pre-surge discovery, blended funnel, as-of gate
- [asof-backtest](stage-12/asof-backtest.md) — Added a point-in-time A/B backtest harness that (EXPIRED-aware) showed pre-surge does not beat momentum, blocking its auto-promotion to cron.
- [blended-funnel-gate](stage-12/blended-funnel-gate.md) — Added a store-level LIVE-BULL overextension rejection plus a blended pre-surge-and-capped-momentum candidate funnel wired into cron/API prompts with R1/R2 enforcement.
- [pre-surge-discovery](stage-12/pre-surge-discovery.md) — Added `pre_surge_discovery.py` scoring four not-yet-extended setups (base_pivot/pullback/rs_leader/pre_earnings) and a `screen-presurge` CLI as the orthogonal candidate stream.

## Stage 13 — Per-holding exit discipline skill
- [position-exit-manager](stage-13/position-exit-manager.md) — Adds ATR chandelier stop, R:R take-profit, and thesis-invalidation rules emitting EXIT/TRIM/ADD/WATCH/HOLD per holding.

## Stage 14 — Deterministic sector-rotation RS screener
- [sector-rotation-rs](stage-14/sector-rotation-rs.md) — Scores each sector by RS/breadth/lifecycle into FAVOR/ROTATING/AVOID verdicts, feeding candidate discovery as a bounded ranking boost.

## Stage 15 — R3 catalyst event-risk gate
- [catalyst-event-gate](stage-15/catalyst-event-gate.md) — Merges FMP earnings + macro calendars into a forward timeline; caps/trims picks near earnings or FOMC/CPI/NFP events.

## Stage 16 — EOD price-level watchlist alerter
- [watchlist-monitor](stage-16/watchlist-monitor.md) — Fires Korean Telegram alerts when a ticker's close touches entry/stop/target/re-entry levels, merging saved rows, predictions, and positions.

## Stage 17 — Wire sector boost + event gate into briefing
- [briefing-integration](stage-17/briefing-integration.md) — Threads the sector-RS boost and R3 event gate into the cron and API daily-briefing paths so the LLM receives both.

## Stage 19 — Paper-trading books + advisory loop
- [paper-trading-system](stage-19/paper-trading-system.md) — Builds simulated US $100k / KR ₩100M books trading LIVE BULL predictions long-only, with daily NAV and weekly advisory review.

## Stage 20 — KR news via official Naver Search API
- [kr-news-naver-api](stage-20/kr-news-naver-api.md) — Replaces the fragile finance.naver.com scrape with the official Naver Search API for KR per-ticker news, keeping the scrape as fallback.

## Stage 21 — Global macro / geopolitical news feed
- [macro-news](stage-21/macro-news.md) — Adds a market-agnostic macro-news fetcher (wire RSS primary, GDELT fallback, no key) injected into the daily-briefing prompts.

## Stage 22 — Restore the dead R3 event gate
- [r3-event-gate-restore](stage-22/r3-event-gate-restore.md) — Migrates R3 to FMP /stable endpoints, adds a keyless yfinance earnings fallback, and exposes per-source availability fields.

## Stage 23 — Hard-reject LIVE 1Y predictions
- [gate-1y-live](stage-23/gate-1y-live.md) — Blocks LIVE 1Y predictions at the store (0/12 hits, -23.8% avg), making the Cycle horizon narrative-only across prompts.

## Stage 24 — Deterministic macro risk-off switch
- [macro-risk-off-switch](stage-24/macro-risk-off-switch.md) — Adds a keyword tripwire over macro headlines emitting NORMAL/ELEVATED/RISK_OFF that caps new BULL and trims confidence in briefings.

## Stage 26 — KR ETF data layer
- [etf-data-layer](stage-26/etf-data-layer.md) — Adds the KR ETF universe + metadata layer (Naver source, cp949; pykrx broken) with tax/asset classification, CSV stale-fallback cache, and `stock-cli etf list/info`.
