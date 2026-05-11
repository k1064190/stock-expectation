# Stage 2 — News & disclosure data layer

## Why
`/expect` had no quantitative news or disclosure signal — discovery and headline reading both ran through `WebSearch`, which is unstructured and hard to score. The redesign needs:
- A deterministic news fetcher per market that `/expect` can call via `bin/stock-cli`.
- Sentiment scores (US, when available) for the news_score table in Stage 4.
- KR regulatory disclosures (감자, 유상증자, 관리종목 등) for the hard-cap penalty in Stage 4.

Doctor Cho chose **Finnhub + Alpha Vantage** for US and **Open DART + Naver scrape** for KR.

## What
- `mcp-market-data/providers/base.py` — added `NewsItem` and `Disclosure` dataclasses; added abstract `get_news()` to `MarketDataProvider`.
- `mcp-market-data/providers/us.py` — `get_news()` with provider chain Finnhub → AV sentiment merge → FMP → yfinance. Helpers `_finnhub_news`, `_merge_alpha_vantage_sentiment`, `_fmp_news`, `_yfinance_news`. Reads `FINNHUB_API_KEY`, `ALPHA_VANTAGE_API_KEY`, `FMP_API_KEY` from env.
- `mcp-market-data/providers/kr.py` — `get_news()` via Naver Finance HTML scrape; `get_disclosures()` via Open DART REST. Helpers `_scrape_naver_news`, `_parse_naver_date`, `_lookup_dart_corp_code`, `_download_dart_corp_codes`. Reads `OPEN_DART_API_KEY`. Caches DART corp_code mapping at `data/dart_corp_codes.csv` on first use.
- `stock_cli.py` — 3 new subcommands: `news`, `disclosure`, `horizon-metrics-batch`. All emit JSON with a `generated_at` ISO timestamp (staleness pattern borrowed from staskh — see [docs/external-skills-analysis.md](../external-skills-analysis.md)).
- `pyproject.toml` — added `beautifulsoup4>=4.12` (HTML parser for Naver).
- `mcp-market-data/tests/test_news.py` — 14 tests (all mocked, no network). Covers: dataclass smoke, Finnhub happy path, AV merge by URL, AV fallback to ticker average, yfinance fallback when no keys, Finnhub HTTP error handling, Naver fixture parse, since_days filter, Naver network error, DART no-key behavior, DART corp_code download + cache, unknown ticker, status `013` (no data) handled as empty.

## How
- Reused the existing `httpx`-based provider pattern — no new HTTP client. The `with_retry` helper in `base.py` is available but I deliberately did **not** wrap news calls in it: news endpoints are best-effort, and a 30-second retry chain on transient 429s would slow down `/expect` unacceptably.
- Sentiment merge strategy: match Finnhub items to AV's `feed[].url` first; if no match, apply the average `ticker_sentiment_score` across all AV articles to any item still missing a score. This handles the common case where AV indexes a different set of articles than Finnhub for the same ticker and date range.
- For KR news, I initially missed that `news_news.naver` is loaded inside an iframe on `news.naver` and only renders the listing when both an empty `clusterId` parameter **and** a `Referer` header pointing at the parent page are present. Without them the page returns a placeholder showing "no news for ''". Caught at smoke test, fixed in `_scrape_naver_news`. There's a comment on the call making the constraint clear so it doesn't get re-broken.
- For KR disclosures, the corp_code mapping is downloaded once and cached. Unlisted entries (no `stock_code`) are stripped from the cache to keep it small. DART status `013` (no data within window) is treated as a legitimate empty result, not an error.

## Code locations
- `mcp-market-data/providers/base.py:79-119` — `NewsItem`, `Disclosure` dataclasses
- `mcp-market-data/providers/base.py:162-176` — abstract `get_news`
- `mcp-market-data/providers/us.py:91-138` — `get_news` orchestration
- `mcp-market-data/providers/us.py:457-` — Finnhub/AV/FMP/yfinance helpers
- `mcp-market-data/providers/kr.py:64-167` — `get_news`, `get_disclosures`
- `mcp-market-data/providers/kr.py:` (end) — Naver/DART helpers
- `stock_cli.py` — `cmd_horizon_metrics_batch`, `cmd_news`, `cmd_disclosure` (after `cmd_horizon_metrics`)
- `mcp-market-data/tests/test_news.py` — 14 tests
- `pyproject.toml:11` — `beautifulsoup4` dep

## Verification
End-to-end smoke tests passed:
- `uv run pytest -m "not network"` — 152 passed, 12 deselected
- `uv run stock-cli news AAPL --market US --limit 3 --since-days 30` — returned 3 yfinance items (no keys set)
- `uv run stock-cli news 005930 --market KR --limit 3 --since-days 7` — returned 3 live Naver headlines from 한국경제, 파이낸셜뉴스, 매일경제 (after the iframe Referer fix)
- `uv run stock-cli horizon-metrics-batch AAPL,MSFT --market US --days 400` — both tickers resolved with full metrics

DART disclosures not smoke-tested — requires `OPEN_DART_API_KEY`, which Doctor Cho will set up before Stage 4 e2e validation. Tests cover the parse/cache logic.

## Per-stage review

### code-reviewer-pro (round 1)
Three findings:

1. **CRITICAL — Naver date zfill** (`kr.py:_parse_naver_date`). Reviewer flagged that `f"{y}-{mo}-{d}"` could emit non-zero-padded dates like `2026-1-5`. Strictly false positive — the regex enforces `\d{2}` so the groups are guaranteed 2-character. **Action: applied the suggested `zfill(2)` anyway** as defensive clarity (cost = 0, future-proofs against regex relaxation).

2. **WARNING — AV merge silent degradation** (`us.py:get_news` + `_merge_alpha_vantage_sentiment`). No visibility into how many items got an AV score. **Action: applied** — `_merge_alpha_vantage_sentiment` now returns the count, and `get_news` logs it at DEBUG. Helps spot URL-matching breakage in the field.

3. **CONSIDER — AV time-window cross-check**. Reviewer suggested rejecting AV merges where AV's article timestamp falls outside the news fetch window. **Dismissed.** Adds nontrivial complexity for a low-risk edge case (Finnhub + AV both default to recent windows and we already match by URL; a stale URL match would imply AV cached an old article under the same URL, which doesn't really happen). Adding the check would require a third request to retrieve AV publish times reliably — net negative.

Final tests after fixes: 152 pass.

### gemini-subagent (round 2)
Five findings, two of which the first reviewer missed:

1. **MUST — DART CSV race condition** (`kr.py:_download_dart_corp_codes`). Two concurrent `get_disclosures` calls would both observe the cache as missing and both try to write the same CSV — file corruption or `PermissionError` under load. **Action: applied** — write to `dest.with_suffix(... + .tmp.<pid>)` and atomically `os.replace()` into place. Last writer wins on the rename; neither caller sees a partial CSV.

2. **MUST — AV early-exit returns None instead of 0** (`us.py:_merge_alpha_vantage_sentiment`). When AV returns 200 with an "Information"/"Note" body (rate-limited, no feed), the bare `return` would feed `None` into the caller's `%d`-formatted DEBUG log, raising `TypeError`. Caught by the broad `except`, but masks the rate-limit signal in logs. **Action: applied** — return `0` and added a comment noting the AV behavior.

3. **No action — yfinance schema fallback OK**. Both old flat-dict and new content-wrapped shapes are handled; tests cover both.

4. **SHOULD — UTF-8 stdout** (`stock_cli.py:_print_json`). `ensure_ascii=False` relies on `sys.stdout.encoding`; unset `LANG`/`PYTHONIOENCODING` could trigger `UnicodeEncodeError` on Korean. **Dismissed for Stage 2** — the existing `_print_json` was using this pattern long before this stage; introducing a stdout reconfigure unrelated to the news work would be scope creep. Filed in retrospective as something to revisit project-wide if it ever bites.

5. **CONSIDER — `import pandas as pd` inside batch loop** (`us.py:get_price_history_batch`). Out of scope for Stage 2 — that function was untouched by this work; first reviewer correctly skipped it. Dismissed.

Final tests after both rounds: 152 pass.

## Retrospective
What went well: the unit-test mocking strategy let me iterate on the Naver parser without burning real requests. Catching the iframe/Referer issue at smoke test rather than in /expect's e2e was a good call — the unit tests with the fixture HTML had blinded me to it.

What to carry forward: when scraping a site, always run a no-mocks live smoke test before declaring the parser done. Mocked fixtures only prove the parser handles *the structure I expected*, not the structure the site actually serves.
