# Handoff — `/expect` redesign branch

> **🟢 MERGED 2026-05-11.** PR [#2](https://github.com/k1064190/stock-expectation/pull/2) was squash-merged into `master`
> as commit `d2ef519`. Stage 4.1 follow-up patches (live-E2E findings) are documented separately in
> [`docs/stage-4.1/e2e-followups.md`](stage-4.1/e2e-followups.md). This document remains as the historical record
> of the redesign work and as the orientation pointer for §11's remaining decision points (Stage 7-A/7-B live
> verification, PyKRX xfail, skill catalog second pass).

**Branch:** `feature/expect-redesign` (merged; local copy may still exist as a safety net)
**PR:** [#2](https://github.com/k1064190/stock-expectation/pull/2) — squashed into master as `d2ef519`
**Pre-squash HEAD:** `93fa077` (Stage 4.1 review-loop response — contract pin + stage doc)
**Status:** Implementation complete and merged. Live `/expect` E2E run on 2026-05-11 validated the pipeline (10/10 §11.D checklist) and surfaced 4 defects that were patched in Stage 4.1. Stage 7-A/7-B end-to-end with the heavy deps still deferred until Doctor Cho runs `uv sync --extra memory` / `docker compose up -d neo4j`.

This is the single document a future session should read first. Every other doc in `docs/` is referenced from here.

---

## 1. TL;DR

The `/expect` skill went from "qualitative LLM-driven multi-horizon forecaster" to **a deterministic Buy/Sell recommender** that:

- Combines an algorithmic point-table score (`ALGO_SCORE`, max +8.0) with a structured news+sentiment score (`NEWS_SCORE`, max +3.0) into a 1-decimal `COMPOSITE` in the range −7.0..+11.0
- Maps the composite via half-open ranges to `BUY` / `WATCH` / `HOLD` / `AVOID` / `SELL`
- Emits a 3-fact transmission chain (TECH / NEWS / RISK) per pick
- Logs multi-horizon predictions (1W/1M/6M/1Y) into `data/predictions.db`
- Writes an outcome telemetry sidecar at `state/last-outcome-expect.json`
- Is fed by a new news/disclosure data layer (Finnhub → Alpha Vantage → FMP → yfinance for US; Naver scrape + Open DART for KR)

Around it: the skill catalog was pruned (59 → 31 active + 19 archived + 11 deleted), a weekly calibration aggregator was added, a mem0-backed memory layer and a Neo4j-backed graph layer were scaffolded behind `--extra` flags, and `.env` is now auto-loaded at every entry point.

**Test counts (post Stage 4.1):** 207 unit + 23 network passing (1 pre-existing PyKRX failure unrelated to this branch; was 200/22 pre-Stage-4.1).

---

## 2. Branch + PR

> **Post-merge note:** All 27 branch commits — the 22 listed below plus this HANDOFF commit (`bf27583`) plus 5 Stage 4.1 follow-ups (`0a9696a` chore: track indicators.py, `0e5507e` feat: volume metric (A1), `85518c0` feat: AV diagnostic (B4), `7266e4a` docs: schema discipline (A3), `93fa077` docs+test: review-loop response) — were squashed into master as `d2ef519` on 2026-05-11 03:45 UTC. The list below is the original pre-Stage-4.1 snapshot kept for historical reference.

### Commits ahead of master (22 total)

**9 pre-existing portfolio commits** (carried over from `feature/portfolio-tracker` when this branch was cut):
```
d353251 portfolio data models
bf5c138 DB layer (CRUD + moving-avg position cost)
89cf53b CSV import (validation + duplicate detection)
2821249 evaluator (P&L, risk, predictions cross-check, advice)
ed6d0ed integrate portfolio into stock-cli
789f0dd portfolio-eval Claude skill
d704f96 portfolio docs in CLAUDE.md
966a1c5 end-to-end portfolio integration tests
40cea35 Toss Securities sync via tossctl
```

**13 redesign commits** added this session:
```
e0991b1 docs(stage-1): add external skill references and synthesis
ed59bb0 feat(stage-2): add news + disclosure data layer with sentiment merge
2a1272d docs(stage-2): document new news/disclosure env vars in CLAUDE.md
1d5097e chore(stage-3): cleanup skills -- 11 deleted, 19 archived, 31 active
e71ec89 docs(stage-3): update CLAUDE.md + README.md + stage doc for new skill set
ff01fde feat(stage-4): rewrite /expect as deterministic BUY/SELL recommender
70a173e feat(stage-6): weekly calibration aggregator
d967cb8 feat(stage-7a): mem0 memory layer behind --extra memory
71fde24 feat(stage-7b): Neo4j Community graph layer behind --extra graph
602e52d docs(stage-5): branch summary + operator runbook
d09d30e feat: auto-load .env via python-dotenv at all entry points
cc40fd5 test: comprehensive CLI + dotenv + live-API coverage with codex review
6d6d56c test+fix: address gpt-5.5 round-2 review (5 MUST + 4 SHOULD)
```

PR #2 has the pre-PR reviewer hook approved. The hook is configured to run on every `gh pr create` (see `~/.claude/settings.json`), so CI gating already happened at PR creation.

### Local working tree

`git status --short` shows ~10 modified `.claude/skills/*/SKILL.md` files that were already modified before this branch was cut — they're cosmetic edits in pre-existing skills (earnings-calendar, finviz-screener, etc.) and are NOT part of this redesign. They're left dirty intentionally; merge them in a separate PR if you want them.

---

## 3. Stages — outcome table

| Stage | Status | Stage doc | One-line outcome |
|---|---|---|---|
| 1 | ✅ | [stage-1/references-and-analysis.md](stage-1/references-and-analysis.md) | `references/` dir with 8 cloned repos (gitignored), `external-skills-analysis.md` synthesis, `news-api-comparison.md` |
| 2 | ✅ | [stage-2/news-and-disclosure-data-layer.md](stage-2/news-and-disclosure-data-layer.md) | News/disclosure CLI + provider chain (Finnhub→AV→FMP→yfinance for US; Naver+DART for KR); 14 unit tests, 8 live |
| 3 | ✅ | [stage-3/skill-cleanup.md](stage-3/skill-cleanup.md) | 31 active skills (down from 59), 19 archived under `_archived/`, 11 deleted |
| 4 | ✅ | [stage-4/expect-rewrite.md](stage-4/expect-rewrite.md) | `/expect` rewrite with point-table scoring + transmission chain + sidecar |
| 5 | ✅ | [stage-5/finalization.md](stage-5/finalization.md) | Branch summary + operator runbook |
| 6 | ✅ | [stage-6/weekly-calibration.md](stage-6/weekly-calibration.md) | `scheduler/weekly_calibration.py` + cron entry; 9 unit tests |
| 7-A | ✅ (mocked) | [stage-7/a-memory-layer.md](stage-7/a-memory-layer.md) | mem0 + Qdrant + sentence-transformers behind `--extra memory`; 11 mocked tests |
| 7-B | ✅ (mocked) | [stage-7/b-graph-layer.md](stage-7/b-graph-layer.md) | Neo4j Community via `compose.yml` + `mcp-graph-store/` behind `--extra graph`; 8 mocked tests |
| 4.1 | ✅ | [stage-4.1/e2e-followups.md](stage-4.1/e2e-followups.md) | Live-E2E follow-ups (post-PR): volume metric (A1), AV sentiment URL-match diagnostic (B4), SKILL.md sidecar schema discipline (A3), untracked `indicators.py` chore; 7 new tests + 1 contract pin expansion |

**Borrowed external patterns** (full analysis in [external-skills-analysis.md](external-skills-analysis.md)):
- Explicit point-table scoring → staskh `scanner-bullish`
- Transmission chain → roman-rr `trading-signals`
- Outcome telemetry sidecar → ScientiaCapital `trading-signals`
- Mandatory staleness fields (`generated_at`) → staskh
- Quality gate / consistency check → affaan-m `investor-materials`
- Two-stage cheap→expensive fetch → staskh `whale-hunting`
- Bias checklist (recency / confirmation / anchoring / overconfidence) → JoelLewis `finance-psychology`

**Deliberately not borrowed:**
- JoelLewis 84-skill taxonomy (over-engineered for retail)
- ScientiaCapital Markov 7-state regime ensemble (we have separate skills for that)
- roman-rr's mandatory-GitHub-star handshake (dark pattern)
- Vendor-locked signal APIs (inverts /expect's value prop)

---

## 4. Architecture changes

### New module layout

```
mcp-market-data/
├── providers/
│   ├── base.py            ← extended: NewsItem, Disclosure dataclasses + abstract get_news()
│   ├── us.py              ← extended: get_news() with 4-provider chain
│   └── kr.py              ← extended: get_news() (Naver) + get_disclosures() (Open DART)
├── indicators.py          ← unchanged
└── tests/
    ├── test_news.py       ← NEW: 14 mocked tests for the data layer
    └── test_news_live.py  ← NEW: 8 network-marked live API smokes

mcp-memory-store/           ← NEW (Stage 7-A) — mem0 wrapper, behind --extra memory
├── schemas.py             ← CategoryName, MemoryRecord, SearchHit
├── client.py              ← MemoryStore (lazy mem0 import)
├── ingestion.py           ← ingest_predictions, ingest_transmission_chains
└── tests/test_client.py   ← 11 mocked tests

mcp-graph-store/            ← NEW (Stage 7-B) — neo4j wrapper, behind --extra graph
├── cypher.py              ← INIT_STATEMENTS + CANNED_QUERIES
│                              (renamed from schemas.py to avoid sys.path collision
│                               with mcp-memory-store/schemas.py)
├── driver.py              ← GraphDriver (lazy neo4j import)
├── ingestion.py           ← ingest_predictions/news/disclosures (idempotent MERGE)
└── tests/test_driver.py   ← 8 mocked tests

scheduler/
├── daily_briefing.py      ← unchanged behavior; added load_dotenv at top
├── outcome_tracker.py     ← unchanged behavior; added load_dotenv at top
├── weekly_calibration.py  ← NEW (Stage 6); reuses metrics.py helpers
└── tests/
    └── test_weekly_calibration.py  ← NEW: 9 tests

stock_cli.py               ← extended: 7 new subcommand groups
                             news, disclosure, horizon-metrics-batch, memory.*, graph.*
                             + load_dotenv autoload + _positive_int validator

.claude/skills/
├── expect/SKILL.md        ← REWRITTEN (Stage 4); the central recommender
├── _archived/             ← NEW dir; 19 specialized skills moved here, gitignored
│                              from Claude Code's discovery scan via the leading underscore
└── (29 other active skills)

compose.yml                ← NEW (Stage 7-B): neo4j:5-community service
.env.example               ← NEW: full key list with signup links
references/                ← NEW (Stage 1); cloned external repos (gitignored)
docs/                      ← NEW; per-stage docs + this HANDOFF
```

### CLI surface (post-Stage-2/4/6/7)

```
stock-cli
├── price                            ← existing
├── price-batch                      ← existing
├── fundamentals                     ← existing
├── fundamentals-batch               ← existing
├── search                           ← existing
├── health                           ← existing
├── horizon-metrics                  ← existing
├── horizon-metrics-batch            ← NEW (Stage 4): /expect's bulk metrics fetch
├── news                             ← NEW (Stage 2): Finnhub→AV→FMP→yfinance fallback for US, Naver for KR
├── disclosure                       ← NEW (Stage 2): Open DART (KR only)
├── memory {search,add,stats,purge}  ← NEW (Stage 7-A): mem0 wrapper, --extra memory
├── graph {init,query,similar-stocks,theme-winners}  ← NEW (Stage 7-B): Neo4j wrapper, --extra graph
├── predict {create,list,detail,cancel}  ← existing
├── track-record                     ← existing
├── calibration                      ← existing
└── portfolio {...}                  ← existing
```

---

## 5. API keys + `.env`

The repo root `.env` is gitignored and auto-loaded at every entry point via `python-dotenv` (added in commit `d09d30e`). Existing shell exports take precedence over `.env` (override=False).

### Keys required by the redesign

```bash
# Optional but recommended for /expect's full functionality
FINNHUB_API_KEY=          # 60 req/min free tier, https://finnhub.io
ALPHA_VANTAGE_API_KEY=    # 25 req/day free tier; without this, news_score sentiment is 0
                          # https://www.alphavantage.co/support/#api-key
OPEN_DART_API_KEY=        # Free, https://opendart.fss.or.kr/uss/umt/EgovMberInsertView.do

# Existing
FMP_API_KEY=              # Already in .env; backup news source if Finnhub absent
TELEGRAM_BOT_TOKEN=       # Optional, scheduler delivery
TELEGRAM_CHAT_ID=
ANTHROPIC_API_KEY=        # Only for scheduler --mode api

# Stage 7-B (only when graph layer is exercised)
NEO4J_PASSWORD=           # Pick any string; compose.yml fails at startup if unset
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j

# Stage 7-A (no key required; uses local sentence-transformers)
```

### `.env.example` is committed (commit `d09d30e`)

A new contributor runs `cp .env.example .env`, fills in the keys they have, and goes. Common typo: `ALPHA_VATAGE_API_KEY` instead of `ALPHA_VANTAGE_API_KEY` — the missing `N` will silently zero out the sentiment component of `/expect`. Caught Doctor Cho on this once already.

---

## 6. Testing

### Running

```bash
uv run pytest -m "not network"   # 200 tests, ~5s, no API calls
uv run pytest                     # 223 total, hits real APIs (1 pre-existing PyKRX failure)
uv run pytest -m network          # network only, ~25s
```

### Test inventory by file

| Path | Count | Network? | Covers |
|---|---|---|---|
| `tests/test_cli_integration.py` | 29 (3 net) | mixed | Subprocess CLI invocations: help wiring, error paths, JSON-validation edges, network smokes |
| `tests/test_dotenv_autoload.py` | 10 | no | Sentinel-based real load_dotenv proof at all 4 entry points + override=False + project-root vs CWD + dotenv-missing tolerance |
| `tests/test_batch_commands.py` | 5 (4 net) | mixed | Pre-existing batch tests; 1 network failure (`test_kr_fundamentals_fixed`) is a PyKRX data-source issue unrelated to this branch |
| `tests/test_migration.py` | 6 | no | Pre-existing |
| `mcp-market-data/tests/test_news.py` | 14 | no | Mocked Finnhub/AV/FMP/yfinance/Naver/DART |
| `mcp-market-data/tests/test_news_live.py` | 8 | yes | Live API smokes: contract invariants pinned (newest-first ordering, since_days filter, limit enforcement, hard fail on Naver-zero, DART per-item contract + cache call-count) |
| `mcp-market-data/tests/test_indicators.py` | 11 | no | Pre-existing |
| `mcp-market-data/tests/test_providers.py` | 22 (8 net) | mixed | Pre-existing |
| `mcp-prediction-store/tests/test_metrics.py` | 12 | no | Pre-existing |
| `mcp-prediction-store/tests/test_models.py` | 17 | no | Pre-existing |
| `mcp-memory-store/tests/test_client.py` | 11 | no | Mocked mem0; covers result-shape normalisation across 3 mem0 versions, validate_category, ensure_ready failure modes, stats per-category, purge id-tolerant |
| `mcp-graph-store/tests/test_driver.py` | 8 | no | Mocked neo4j; covers IF NOT EXISTS idempotency, eager verify_connectivity, run_many error tolerance, dict-conversion of records |
| `scheduler/tests/test_weekly_calibration.py` | 9 | no | parse_since, overconfident-bucket flagging, worst-signal filtering, markdown rendering, trend-file capping, idempotent same-day rewrite |
| `scheduler/tests/test_daily_briefing.py` | 6 | no | Pre-existing |
| `scheduler/tests/test_outcome_tracker.py` | 17 | no | Pre-existing |
| `portfolio/tests/*.py` | 56 | no | Pre-existing portfolio work |

### Running individual lanes

```bash
# All new redesign tests, fast lane
uv run pytest tests/ mcp-market-data/tests/test_news.py mcp-memory-store/ mcp-graph-store/ scheduler/tests/test_weekly_calibration.py -m "not network" -v

# Live API smokes (set the keys first)
uv run pytest mcp-market-data/tests/test_news_live.py -m network -v

# CLI integration network paths
uv run pytest tests/test_cli_integration.py -m network -v
```

---

## 7. Code review history (what each round caught)

This branch went through three review passes. Documenting them so a future session knows what's already been considered.

### Round 1 — code-reviewer-pro (Stage 2)
Three findings on `mcp-market-data/providers/{us,kr}.py`:
1. **Critical** Naver date `zfill` (false positive — regex enforced 2 digits, but added defensively anyway)
2. **Warning** AV merge silent degradation → return count + DEBUG log
3. **Consider** AV time-window cross-check → dismissed (over-engineering)

### Round 2 — gemini-subagent (Stage 2)
Two real bugs the first reviewer missed:
1. **Must-fix** DART corp_code download race → atomic `os.replace()` rename
2. **Must-fix** AV `_merge_alpha_vantage_sentiment` returned `None` instead of `0` on empty feed → caller's `%d` log crashed → fixed
3. yfinance schema fallback already correct (no action)
4. UTF-8 stdout dismissed (out of Stage 2 scope; preexisting pattern)
5. Pandas import in loop dismissed (out of Stage 2 scope)

### Round 3 — gemini-subagent gpt-pro (Stage 4)
Six findings on the rewritten `/expect/SKILL.md`:
1. **Must-fix** ALGO_SCORE / NEWS_SCORE / COMPOSITE max-score arithmetic stated wrong (10/5/15 → real 8/3/11) → recalibrated thresholds
2. **Must-fix** Trend and Return_1M buckets overlapped → rewrote with `if/elif`
3. **Must-fix** Decimal-gap thresholds (3..5, 6..8 left 5.5 and 8.5 undefined) → half-open contiguous ranges
4. **Should** ALL-mode 10×15-line output too verbose → "Output verbosity by mode" section scales detail
5. **Consider** LLM math reliability for quality gate → dismissed (point table small enough; revisit if drift shows)
6. **Consider** Recursion guard for skill composition → added explicit note

### Round 4 — codex gpt-5.4 xhigh (round-1 test review)
Seven findings, all addressed in commit `cc40fd5`:
- **Must** dotenv tests not testing real entry points (just grep/snippet)
- **Must** projects-root vs CWD test didn't create conflicting CWD .env
- **Must** AV merge test allowed empty `scored` (silent pass)
- **Must** No FMP-only fallback test
- **Should** CLI yfinance/Naver tests accepted empty items
- **Should** Naver layout test silently skipped on zero
- **Should** No invariants pinned (ordering, limit, since_days)

### Round 5 — codex gpt-5.5 xhigh (round-2 test review, after upgrading codex to 0.130)
Five additional findings 5.4 missed, all addressed in commit `6d6d56c`:
- **Must** dotenv ImportError test still re-implemented the guard → now uses `sys.modules['dotenv']=None` against real entry points
- **Must** No `bin/stock-cli` wrapper smoke (only `uv run stock-cli`) → added
- **Must** `horizon-metrics-batch` contract test allowed per-ticker errors → require both succeed
- **Must** FMP fallback ignored `since_days` (real source bug, not just a test gap) → over-fetch + post-filter in `_fmp_news`
- **Must** DART cache test only checked file size, not call count → spy on `_download_dart_corp_codes`
- **Should** `[]` and `null` JSON didn't fail (only malformed) → parametrized
- **Should** `--limit 0`/`-1` silently sliced wrong → `_positive_int` argparse type
- **Should** Naver-zero on Samsung silently skipped → fail loudly
- **Should** DART live test only checked `first.url` → full per-item contract pinned
- **Consider** `temp_env_file` race under pytest-xdist → deferred (we don't run xdist)

### Real bug caught while writing tests

`schemas.py` name collision between `mcp-graph-store/` and `mcp-memory-store/` on sys.path. Both directories were inserted via `sys.path.insert(0, …)` in `stock_cli.py`, so `from schemas import CATEGORIES` resolved to whichever was inserted last (graph) — breaking `stock-cli memory stats` with a misleading "module unreadable" error. **Fixed:** renamed `mcp-graph-store/schemas.py` to `cypher.py` (descriptive of its content — `INIT_STATEMENTS` and `CANNED_QUERIES` are Cypher-related).

### Round-1 test bug caught

The original Stage-4 SKILL.md had stated max scores wrong (10/5/15 vs actual 8/3/11). Caught only because the gemini reviewer did the arithmetic — the LLM inside `/expect` would have happily emitted "BUY composite 11.5/15" and Doctor Cho would have spotted it eventually.

### Recurring lessons
- Don't trust a max-score number you wrote; sum the table.
- A flat-import pattern (`from schemas import …`) collides if two modules share the same name and both directories are on sys.path.
- "Test that exists" ≠ "test that catches regressions" — every silent-skip path is a way for bugs to ship.

---

## 8. Known issues + deferred items

### Pre-existing failures (not introduced by this branch)

| Test | Failure | Action |
|---|---|---|
| `tests/test_batch_commands.py::test_kr_fundamentals_fixed` | PyKRX bulk fundamentals returns null for Samsung (005930) | Investigate as separate work item; PyKRX upstream issue |

### Deferred CONSIDER items from reviews

| Item | Source | Reason for deferral |
|---|---|---|
| AV time-window cross-check on merge | gemini round 2 | Over-engineering for low-risk edge case |
| LLM math in quality-gate Step 11 → push to Python | gemini round 3 | Point table small enough that LLM math is reliable; revisit if Stage 6's calibration shows drift |
| `temp_env_file` race safety under pytest-xdist | codex 5.5 | We don't run xdist; add a lock if/when introduced |
| stdout `ensure_ascii=False` UnicodeEncodeError on Windows / unset PYTHONIOENCODING | gemini round 2 | Pre-existing pattern in `_print_json`; out of redesign scope |
| `import pandas as pd` inside `get_price_history_batch` loop | gemini round 2 | Pre-existing, out of Stage 2 scope |
| Stage 6.1: auto-prompt-rewriting based on calibration drift | Stage 6 retrospective | Wait ≥12 weeks of trend data before mechanizing |
| Stage 7-B.1: theme tagging via Stage 7-A semantic similarity | Stage 7-B doc | Depends on Stage 7-A live; defer until mem0 has real data |
| Concurrent first-use race on DART corp_code download | gemini round 5 | Atomic-rename code already handles it; live concurrency test deferred |

### Implementation gaps still present in code

- **`_fmp_news` over-fetch ratio is hardcoded to 4×.** If FMP returns fewer than `limit*4` items globally, the post-`since_days` filter could drop too many. Currently safe because FMP's free tier returns plenty of items, but if the contract gets stricter, swap to a paginated loop.
- **`get_news` does not retry on transient HTTP failures.** Each provider call is one-shot; the retry helper `with_retry` exists in `base.py` but isn't wired in (deliberately — news endpoints are best-effort and a 30s retry chain on 429 would slow `/expect` unacceptably).
- **mem0 fact-extraction LLM is not configured.** Without an LLM in the mem0 config, mem0 will still store raw content but won't extract structured facts. Stage 7-A leaves this pluggable; pick before first heavy ingest (`docs/stage-7/a-memory-layer.md` discusses Anthropic Haiku 4.5 as the cheap option).
- **Naver Finance scraper is fragile.** It depends on (1) the `Referer` header, (2) the empty `clusterId=` parameter, and (3) the `table.type5` selector. The live test for Samsung now hard-fails if any of these regress; that's the canary.
- **Graph theme tagging is manual.** The `(Stock)-[:LINKED_TO]->(Theme)` edge has no automatic ingestion path yet; tags must be inserted via raw `graph query`. Stage 7-B.1 work would automate this from mem0 semantic similarity.

---

## 9. The `/expect` skill — current behavior

This is the central piece. SKILL.md is at `.claude/skills/expect/SKILL.md` (commit `ff01fde` rewrote it; commit `6d6d56c` added the `_positive_int` arg validation that downstream tools depend on).

### Invocation

```
/expect KR              5 trending KR stocks
/expect US              5 trending US stocks
/expect 삼성전자         single-stock deep dive (resolve KR name → ticker)
/expect NVDA            single-stock deep dive (US ticker)
/expect NVDA,AMD,AVGO   multi-ticker batch
/expect ALL  or  /expect    full scan (US 5 + KR 5 = 10 stocks)
```

### What runs (12-step workflow)

1. **Pre-flight:** `bin/stock-cli track-record --days 30` + `calibration` — surface overconfident buckets
2. **Discovery** (market-scan and ALL modes): WebSearch for trending stocks; resolve KR names via `bin/stock-cli search`
3. **Bulk technical metrics:** `bin/stock-cli horizon-metrics-batch <tickers> --market <M> --days 400`
4. **News + disclosure** per ticker: `stock-cli news` (US: Finnhub→AV→FMP→yfinance; KR: Naver) + `stock-cli disclosure` (KR only, Open DART)
5. **`ALGO_SCORE`** (max +8.0): trend (3) + momentum (1.5) + return_1m (1.5) + volume (1) + cycle (1) + earnings_event_penalty (-1)
6. **`NEWS_SCORE`** (max +3.0): sentiment (2) + headline_volume (1), with hard caps at -2.0 for negative-keyword scan or KR material disclosure flag
7. **`COMPOSITE`** = ALGO + NEWS, mapped via `>=8.0 BUY`, `[6.0, 8.0) WATCH`, `[3.0, 6.0) HOLD`, `[0, 3.0) AVOID`, `<0 SELL`
8. **Transmission chain** (3 facts, TECH/NEWS/RISK) per pick — must quote specific numbers
9. **Multi-horizon predictions** (1W/1M/6M/1Y) — preserves existing 4-horizon logic with shared `analysis_group_id`
10. **Outcome telemetry sidecar** at `state/last-outcome-expect.json` — every component score broken out
11. **Quality gate** (sign agreement, target separation, fresh-data, transmission-chain hygiene)
12. **Bias checklist** (recency / confirmation / anchoring / overconfidence)

### Output verbosity scales by mode

- Single ticker / batch ≤ 5 → full per-stock detail block
- KR or US (5 stocks) → full block
- ALL (10 stocks) → 3-line abbreviated detail per stock; full block reserved for picks that triggered RULE C1 or a hard cap

### Composition with other skills (gates, not absorption)

- `market-top-detector` / `ftd-detector`: invoked when `COMPOSITE >= 8.0`; defensive reading downgrades BUY → WATCH
- `macro-regime-detector`: cite current regime in bias check
- `theme-detector`: late-stage decay caps label at WATCH
- `prediction-review`: surface poor-calibration tickers
- `position-sizer`: link out for share-count math; do not output share counts directly

**Recursion guard:** none of these gate skills currently call `/expect`. If any are extended to do so in future, add a `--no-gate-recursion` flag to suppress the macro-context call.

---

## 10. Operator runbook (bring-up sequence)

```bash
# 0. One-time clone setup
cd ~/projects/stock-expectation
git fetch origin
git checkout feature/expect-redesign

# 1. Base install (light deps only)
uv sync --extra dev --extra skills

# 2. Configure .env
cp .env.example .env
$EDITOR .env  # fill in keys you have
# Notable typo to avoid: ALPHA_VANTAGE_API_KEY (NOT ALPHA_VATAGE — the N is essential)

# 3. Verify dotenv autoload
uv run python -c "import os, stock_cli; print('FMP:', bool(os.environ.get('FMP_API_KEY')))"
# expect: FMP: True (or whatever key you have set)

# 4. Run unit + live test sweeps
uv run pytest -m "not network"   # 200 should pass
uv run pytest -m network          # 22 pass + 1 pre-existing PyKRX failure

# 5. Smoke-test the new CLI surface
./bin/stock-cli news NVDA --market US --limit 3 --since-days 7
./bin/stock-cli news 005930 --market KR --limit 3 --since-days 7
./bin/stock-cli disclosure 005930 --since-days 7  # needs OPEN_DART_API_KEY
./bin/stock-cli horizon-metrics-batch AAPL,MSFT --market US

# 6. Try /expect against the new CLI
# Inside a Claude Code session:
#   /expect NVDA          → single-ticker deep dive
#   /expect ALL           → full scan
# Confirm:
#   - state/last-outcome-expect.json was written
#   - data/predictions.db has new rows with shared analysis_group_id
#   - transmission chains have specific numbers, not adjectives

# 7. Weekly calibration (cron-ready)
uv run python scheduler/weekly_calibration.py --dry-run
# Confirm: emits real report from live predictions.db
# Install cron: crontab scheduler/crontab.example

# 8. (Optional) Stage 7-A — memory layer
uv sync --extra memory
uv run stock-cli memory stats
# Pick + configure mem0's LLM for fact extraction (see docs/stage-7/a-memory-layer.md)
uv run python -c "
import sys; sys.path.insert(0, 'mcp-memory-store')
from client import MemoryStore
from schemas import MemoryRecord
store = MemoryStore()
mid = store.add(MemoryRecord(category='predictions', content='NVDA BULL @0.72 1W', metadata={'ticker':'NVDA'}))
print('memory_id:', mid)
hits = store.search('AI infrastructure', category='predictions', limit=3)
for h in hits: print(h.score, h.memory)
"

# 9. (Optional) Stage 7-B — graph layer
uv sync --extra graph
echo "NEO4J_PASSWORD=changeme123" >> .env
docker compose up -d neo4j
# Wait ~30s for the container to boot
uv run stock-cli graph init
uv run stock-cli graph query "MATCH (n) RETURN count(n) AS total"
# Then run scheduler/index_to_graph.py (not yet written — Stage 7-B.1 work)
```

---

## 11. Next session — decision points

These are the questions a future session should put to Doctor Cho (or that Doctor Cho should think about) before doing more work.

### A. Live verification of Stages 7-A / 7-B
Both stages are mocked-tested and CLI-wired but have not been exercised against real mem0 + real Neo4j. The first run might surface: mem0 version-shape drift (we handle 3 known shapes), Neo4j Cypher syntax differences vs documented form, sentence-transformers download size on first import. **Action:** schedule ~30 min when Doctor Cho can `uv sync --extra memory` and `docker compose up -d neo4j` and exercise the flows. Smoke test commands are in §10.

### B. Stage 7-B.1: theme tagging from mem0 similarity
The `(Stock)-[:LINKED_TO]->(Theme)` edge in Neo4j has no automatic ingestion path yet. Once Stage 7-A has data, `index_to_graph.py` (not yet written) should:
1. For each Stock, fetch related news/predictions via mem0 semantic search
2. Cluster by theme (mem0 `themes` category)
3. Insert LINKED_TO edges with similarity strength

This was deferred because it needs Stage 7-A live data to debug against. **Action when ready:** new feature branch off `feature/expect-redesign`'s eventual merge.

### C. Stage 6.1: auto-tuning the point table from calibration drift
The weekly calibration aggregator currently emits markdown reports + JSON trend; humans read them and decide. If a confidence bucket shows persistent drift for ≥4 weeks (Stage 6 doc retrospective), the trigger is pulled to mechanize trim. **Don't pull early:** at least 12 weeks of trend data should accumulate first.

### D. /expect end-to-end run against real data
The `/expect` skill is markdown-only — its correctness can only be validated by running it. **First ALL run should test:**
1. WebSearch surfaces 10 candidates (5+5)
2. `horizon-metrics-batch` resolves all 10 without error
3. News fetch returns ≥1 item per US ticker (Finnhub or yfinance fallback)
4. KR news fetch returns ≥1 item per KR ticker (Naver)
5. KR disclosures don't error if any tickers have material flags
6. Composite math hand-checks: pick one stock and verify ALGO + NEWS sum matches `composite` field
7. Transmission chain has specific numbers (not "good momentum" / "positive sentiment")
8. Predictions logged with shared `analysis_group_id` per stock
9. Sidecar JSON written
10. Output verbosity scales by mode

### E. PyKRX test failure cleanup
`tests/test_batch_commands.py::test_kr_fundamentals_fixed` has been failing for some time — PyKRX's bulk endpoint returns null for Samsung. Either fix the upstream issue (probably patch our `_pykrx_fundamentals` to fall through to the per-ticker endpoint immediately for known-broken tickers) or mark the test xfail with a Linear/issue ref.

### F. Skill catalog stewardship
With 31 active skills, there's still some overlap (e.g. `stock-research` vs `/expect` for single-ticker mode; `daily-briefing` vs scheduler `daily_briefing.py`). Watch for which ones get triggered in actual sessions. A second cleanup pass after 1–2 months of usage should be possible without losing functionality.

---

## 12. Critical files reference

For quick navigation when working on the various subsystems:

| Subsystem | File | Purpose |
|---|---|---|
| `/expect` skill | `.claude/skills/expect/SKILL.md` | Single-file rewrite; the central recommender |
| News data layer (US) | `mcp-market-data/providers/us.py` | `get_news` + provider chain |
| News data layer (KR) | `mcp-market-data/providers/kr.py` | `get_news` (Naver) + `get_disclosures` (DART) |
| News dataclasses | `mcp-market-data/providers/base.py` | `NewsItem`, `Disclosure`, abstract `get_news` |
| CLI | `stock_cli.py` | All subcommands; `_positive_int` validator; load_dotenv |
| Wrapper | `bin/stock-cli` | Bash wrapper; covered by Stage 5+ tests |
| Predictions DB | `data/predictions.db` | SQLite; auto-created |
| DART corp_code cache | `data/dart_corp_codes.csv` | Auto-downloaded on first DART call |
| Outcome sidecar | `state/last-outcome-expect.json` | Written by `/expect` per run; consumed by Stage 6 weekly aggregator |
| Calibration trend | `state/calibration-trend.json` | Rolling 12-week history written by `weekly_calibration.py` |
| Memory store dir | `data/memory/qdrant/` | Qdrant embedded data; gitignored |
| Graph store dir | `data/neo4j/` | Neo4j docker volume; gitignored |
| Reports | `reports/weekly-calibration-YYYY-MM-DD.md` | Stage 6 output; one per week |
| Env file | `.env` | gitignored; auto-loaded; example at `.env.example` |
| Compose | `compose.yml` | Neo4j Community Edition service |
| Pre-PR review hook | `~/.claude/settings.json` | PreToolUse on `gh pr create*` runs sonnet code reviewer |

### When something breaks

| Symptom | Probable cause | First check |
|---|---|---|
| `stock-cli memory stats` returns "module unreadable" | A new `schemas.py` somewhere on sys.path | Check for collision between mcp-* modules |
| KR news returns 0 items | Naver `Referer`/`clusterId` workaround broken | `mcp-market-data/providers/kr.py:_scrape_naver_news` + run `test_naver_scrape_returns_real_kr_news_for_samsung` |
| AV sentiment is None on every item | AV quota exhausted (25/day free), or key typo (`ALPHA_VATAGE`) | `os.environ.get('ALPHA_VANTAGE_API_KEY')` + check key spelling |
| FMP path returns items outside `since_days` | `_fmp_news` over-fetch ratio (4×) too low for the API tier | Increase ratio in `mcp-market-data/providers/us.py:_fmp_news` |
| Composite math doesn't match component sums | `/expect` LLM mis-applied the point table | Compare `state/last-outcome-expect.json` `algo_components`/`news_components` to displayed score |
| `gh pr create` blocked by reviewer hook | Real finding from sonnet reviewer | Read the block reason; if false positive, `SKIP_PR_REVIEW=1 gh pr create …` (per global CLAUDE.md) |

---

## Appendix — How this branch was built (commit timeline)

A future you can reconstruct what each commit did from the messages, but here's the conceptual order:

1. **Plan** (`/home/cwh/.claude/plans/branch-twinkly-cook.md`) — written and approved before any code
2. **Stage 1**: References + analysis (read-only, no code)
3. **Stage 2**: Code + tests + reviews (rounds 1+2 of code review)
4. **Stage 3**: Skill cleanup (filesystem moves)
5. **Stage 4**: SKILL.md rewrite + review (round 3)
6. **Stage 6**: Weekly calibration (smaller scope, single-reviewer skip)
7. **Stage 7-A**: mem0 layer
8. **Stage 7-B**: Neo4j layer
9. **Stage 5**: Branch summary + operator runbook (folded into earlier stages then closed out)
10. **dotenv autoload**: Mid-session addition after Doctor Cho's API-key question
11. **Test coverage round 1** (codex gpt-5.4 review): 27 new tests
12. **Test coverage round 2** (codex gpt-5.5 review, after upgrading codex): 10 more tests + a real implementation fix (FMP `since_days`)

The plan file at `/home/cwh/.claude/plans/branch-twinkly-cook.md` is the authoritative spec — it captures the original 5-stage plan plus the Stage 6 / 7-A / 7-B additions Doctor Cho asked for mid-session.
