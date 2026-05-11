# Stage 4.1 — E2E follow-up patches

## Why

Stage 4 shipped the redesigned `/expect` skill (commit `ff01fde`) without ever running it against real APIs end-to-end. On 2026-05-11 a live `/expect ALL` run (5 US + 5 KR) surfaced four defects that the test suite had not caught:

1. **Untracked production code.** `mcp-market-data/indicators.py` (the `compute_horizon_metrics` source) and `mcp-market-data/tests/test_indicators.py` were never committed to git. The HANDOFF test inventory listed them as "11 pre-existing tests" but `git log --all` returned 0 commits for either path. A fresh checkout would not have been able to run `horizon-metrics-batch`.
2. **Volume bucket had no data source.** The `/expect` Step 5 point table awards +1.0 when `5d avg vol > 50d avg × 1.3`, but `compute_horizon_metrics` did not surface any volume fields. All 10 tickers in the E2E run got 0 for the Volume component regardless of actual flow.
3. **Alpha Vantage sentiment merge is silently broken.** Every Finnhub item for a US ticker carried an identical `sentiment_score` value, indicating that `_merge_alpha_vantage_sentiment` always falls back to the per-ticker average. Root cause: Finnhub returns its own redirect URLs (`https://finnhub.io/api/news?id=...`) which never match AV's publisher URLs. Per-item URL matching is dead code in production.
4. **Sidecar schema discipline failure.** The Step 10 sidecar example specifies `algo_score` / `news_score` keys, but the freeform JSON construction in the E2E run used the shorter `algo` / `news` keys instead. Stage 6's future weekly calibration aggregator would silently break on this.

## What

Four commits on top of `bf27583` (the HANDOFF commit):

| Commit | Subject | Files |
|---|---|---|
| `0a9696a` | chore: track indicators.py + test_indicators.py | `mcp-market-data/indicators.py` (+240), `mcp-market-data/tests/test_indicators.py` (+129) |
| `0e5507e` | feat(stage-4.1): expose volume metric to /expect (A1) | `indicators.py` (+45), `test_indicators.py` (+73), `.claude/skills/expect/SKILL.md` (+4/-2) |
| `85518c0` | feat(stage-4.1): AV sentiment URL-match diagnostic (B4) | `mcp-market-data/providers/us.py` (+25/-11), `mcp-market-data/tests/test_news.py` (+129) |
| `7266e4a` | docs(stage-4.1): SKILL.md sidecar schema discipline (A3) | `.claude/skills/expect/SKILL.md` (+29) |

Plus one post-review patch (folded into `7266e4a`'s commit or added separately — see Retrospective):

- `tests/test_cli_integration.py` — added `vol_5d_avg`, `vol_50d_avg`, `vol_ratio` to the `expected_fields` set in `test_horizon_metrics_batch_returns_expected_fields` so a future refactor that drops them fails the contract test loudly (gemini SHOULD finding).

**Test counts:** 207 unit + 23 network passing (was 200/22 before this stage; +7 tests = 5 volume + 2 AV-diagnostic; network test count includes the expanded contract pin).

## How

**TDD loop** per CLAUDE.md:
1. Write failing tests first (`test_indicators.py` volume tests, `test_news.py` caplog tests).
2. Minimal implementation in `indicators.py` and `providers/us.py`.
3. Confirm green; check for regressions across the full suite.
4. Update SKILL.md to reflect the contract change.

**Volume metric (A1).** Extended `HorizonMetrics` dataclass with three new fields (`vol_5d_avg`, `vol_50d_avg`, `vol_ratio`); added pure-function helpers `_volume(bar)` and `compute_avg_volume(volumes, period)` symmetric with the existing `_close` and `compute_sma`. `vol_ratio` returns `None` (not 0) when either average is `None` or when the 50-day average is 0 — keeps the "no data" vs "zero data" distinction so downstream consumers don't silently confuse them.

**AV diagnostic (B4).** `_merge_alpha_vantage_sentiment` now tracks `url_matched` and `fallback_applied` independently and emits one `DEBUG` line per call with the breakdown. The caller-side duplicate `DEBUG` log was removed (the function's log subsumes it). Docstring updated with the Finnhub-URL reality and a pointer to "follow-the-redirect" as future work.

**Schema discipline (A3).** Added a Field-Names-Are-Contract block in SKILL.md Step 10 with top-level and per-pick schema tables, calling out the specific `algo` vs `algo_score` failure mode the operator hit. Pure docs change.

**Untracked file remediation (chore).** Brought `indicators.py` + `test_indicators.py` under version control with no behavior change. 11 existing tests pass against the committed version.

## Code locations

- `mcp-market-data/indicators.py:HorizonMetrics` — extended dataclass (lines 28-83)
- `mcp-market-data/indicators.py:compute_horizon_metrics` — populates `vol_*` (lines 220-260)
- `mcp-market-data/indicators.py:compute_avg_volume` — new helper (lines 175-192)
- `mcp-market-data/providers/us.py:_merge_alpha_vantage_sentiment` — URL-match diagnostic (lines 540-600)
- `mcp-market-data/providers/us.py` line ~265 — caller-side duplicate `DEBUG` log removed
- `.claude/skills/expect/SKILL.md` Step 5 Volume row (line ~120) — references `vol_ratio` directly
- `.claude/skills/expect/SKILL.md` Step 10 — Field-Names-Are-Contract tables (lines ~261-285)
- `mcp-market-data/tests/test_indicators.py` — 5 new volume tests (lines 132-208)
- `mcp-market-data/tests/test_news.py` — 2 new caplog tests (lines 188-280)
- `tests/test_cli_integration.py:test_horizon_metrics_batch_returns_expected_fields` — vol_* added to expected_fields set
- `state/last-outcome-expect.json` — written by the 2026-05-11 E2E run; now uses correct `algo_score`/`news_score` keys after the in-session patch

## Per-Stage Review Loop outcomes

**Round 6 — code-reviewer-pro (Claude subagent)**
- **0 blocking issues.** All 4 commits assessed as ready-to-merge.
- 1 CONSIDER: should `_merge_alpha_vantage_sentiment` *raise* when `url_matched == 0 AND len(feed) > 0` instead of silently averaging?
  - **Dismissed (deferred):** The new DEBUG log already gives operators full visibility. Raising would force every US `/expect` call to fail until Finnhub-redirect-following is implemented, which is much larger work. Filed as deferred item below.

**Round 7 — gemini-pro (via gemini-subagent skill)**
- **MUST**: keep `vol_ratio = None` on zero/insufficient 50d — already done. Confirmed.
- **SHOULD**: `tests/test_cli_integration.py::test_horizon_metrics_batch_returns_expected_fields` only does subset validation. New fields would pass but a future drop wouldn't fail.
  - **Fixed:** added `vol_5d_avg`, `vol_50d_avg`, `vol_ratio` to `expected_fields` set. Network test re-run confirms green.
- **CONSIDER**: `algo_components` keys like `return_1m` collide with the metric name; LLM might write the raw return instead of the score. Rename to `return_1m_score` etc.
  - **Dismissed (deferred):** Renaming `algo_components` keys is another sidecar contract churn. Filed as deferred item below — worth doing in a single batched contract revision rather than now.
- **CONSIDER**: type hints on `_close(bar)` / `_volume(bar)` for `bar: dict | OHLCV`.
  - **Dismissed (deferred):** Small clarity win, not worth a separate commit. Filed below for a future cleanup pass.
- Confirmed safe: `_volume(bar)` access pattern (OHLCV.volume is mandatory), caplog test isolation (function scope), `providers.us` logger name stability.

## Deferred items (filed during the review loop)

- **AV merge raise-on-zero-URL-match (code-reviewer-pro)**: implement Finnhub redirect-following so per-item URL matching actually works, then optionally raise when match fails. Not worth doing until someone is investing in the news data quality side.
- **`algo_components` key naming (gemini)**: rename score keys to `*_score` suffix (`return_1m_score`, `momentum_score`, etc.) to disambiguate from the metric names. Touches the sidecar contract — batch with any other future contract revision so consumers only update once.
- **`_close`/`_volume` type hints (gemini)**: add `bar: dict | OHLCV` typing. Tiny cleanup; combine with the next type-hint sweep across `indicators.py`.

## Retrospective

**What went well**
- The 12-step `/expect` workflow ran top-to-bottom on real APIs without crashing — the only issues were *quality* defects, not *liveness* defects. The Stage 4 redesign is structurally sound.
- TDD loop caught the volume metric edge cases (zero-50d → None vs ZeroDivisionError) before they shipped.
- Reviewer parallelism worked well: `code-reviewer-pro` and `gemini-pro` reported overlapping but distinct findings, and gemini specifically caught the `expected_fields` subset-test gap that the first reviewer missed.

**What to carry forward**
- **Always run new skills against live APIs at least once before declaring them complete.** Stage 4's SKILL.md was internally consistent and code-reviewed three times but had three latent contract defects (algo vs algo_score, missing volume data, AV silent fallback) that only appeared at runtime.
- **The "Field Names Are Contract" pattern in A3 should generalise** — any spec table that downstream code parses by key name should be annotated so the LLM doesn't shorthand the keys away.
- **`git status` review during every commit pass.** The `indicators.py` untracked-but-imported state had been latent since at least Stage 2; the per-stage doc-writing loop didn't catch it because the file existed locally and tests passed. A quick `git ls-files | xargs -I {} test -f {} || echo missing: {}` plus the inverse check belongs in the per-stage checklist.
