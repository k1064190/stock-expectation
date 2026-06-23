# Stage 20 — KR news via the official Naver Search API

## Why

KR per-ticker news was fetched by scraping finance.naver.com HTML
(`KoreanMarketProvider.get_news`), which "degrade[d] silently to an empty list"
on any layout change or block — the recurring "latest news" failure for Korean
stocks. A deep-research pass (verified, 22/25 claims) recommended the official
**Naver Search API for News** as the best free, stable replacement; it also
removes the DB-rights/UCPA legal risk of crawling Naver.

## What

- `get_news` now prefers the Naver Search API
  (`https://openapi.naver.com/v1/search/news.json`) when `NAVER_CLIENT_ID` +
  `NAVER_CLIENT_SECRET` are set: it resolves the ticker's Korean name (pykrx
  `get_market_ticker_name`) and queries by keyword, mapping results to the
  unchanged `NewsItem` shape (headline, source=publisher domain, ISO date, url).
- Robust fallback: missing keys, no resolvable name, any API error, or an
  empty filtered result (possible over-filtering) → the legacy HTML scrape (no
  regression). Scrape is now the safety net, not the primary path.
- Helpers: `_company_name`, `_fetch_naver_search_news`, `_clean_html` (strip
  `<b>` + `html.unescape`), `_domain`, `_parse_rfc822` (tz-aware).
- `sentiment_score=None` (Naver supplies none; computed downstream by /expect).
- Docs: `NAVER_CLIENT_ID`/`NAVER_CLIENT_SECRET` added to CLAUDE.md API Keys and README `.env`.

## How

Keyword search is noisy, so the fetch over-fetches (`display=min(100, max(limit*3,30))`,
`sort=date`) then keeps only recent items whose company name appears in the title
or summary, parsing RFC-822 `pubDate` to a tz-aware datetime for a
host-timezone-independent `since_days` cutoff. Built TDD (8 KR-news tests:
API parse/relevance/since_days + 3 fallback paths + cleaning).

## Code locations

- `mcp-market-data/providers/kr.py` — `get_news` rewrite + `_company_name`,
  `_fetch_naver_search_news`, `_clean_html`, `_domain`, `_parse_rfc822`;
  `NAVER_SEARCH_NEWS_URL` constant.
- `mcp-market-data/tests/test_news.py` — Naver Search API tests + fallback tests.
- `CLAUDE.md` (API Keys), `README.md` (.env).

## Review loop

- **code-reviewer-pro**: 0 critical; flagged a missing test for the
  `_company_name → None` fallback (added) + a docstring clarity nit on
  unparseable dates (added). Confirmed fallback routing, credential safety
  (secret never logged), and no ReDoS in `_clean_html`.
- **Codex (gpt-5.5, high)**: [MAJOR] — `since_days` compared a tz-stripped
  pubDate against local `datetime.now()`, skewing the window on non-KST hosts.
  Fixed: `_parse_rfc822` keeps the datetime tz-aware and the cutoff uses UTC.
- **Gemini**: unavailable (free-tier CLI deprecated).

## Retrospective

- The research → integrate handoff worked: the verified recommendation mapped
  cleanly onto the existing `NewsItem` interface, so the change was contained to
  one provider with the scrape preserved as a fallback.
- Keyword search means relevance filtering and a ticker→name lookup are now part
  of the path; if precision proves weak (e.g. 삼성 vs 삼성전자 disambiguation),
  tighten the relevance check or add DeepSearch (paid, per-symbol) later.
