# Stage 21 — Global macro / geopolitical news (GDELT + RSS)

## Why

The per-ticker news feeds (Finnhub for US, Naver Search for KR) miss broad,
market-moving world news — wars (e.g. the Strait of Hormuz crisis), oil/energy
shocks, central-bank surprises, tariffs. Macro context previously came only from
the LLM's training knowledge (stale after cutoff) + the scheduled economic
calendar. A verified deep-research pass identified GDELT (free, no key) as the
best global source, with wire-service RSS as a reliable no-key alternative.

## What

- **`mcp-market-data/macro_news.py`** — market-agnostic macro-news fetcher:
  - `fetch_macro_news` — GDELT DOC 2.0 artlist (User-Agent, file cache w/ TTL +
    serve-stale-on-error, retry honoring GDELT's 1-req/5s limit).
  - `fetch_rss_macro_news` — no-key wire RSS (BBC business/world, CNBC economy,
    Yonhap English): dedup, strict tz-aware `since_days` cutoff, sort by publish
    time, per-feed error skip.
  - `get_macro_news` — orchestrator returning `(items, source)`.
- **`stock-cli macro-news`** — `--timespan / --limit / --query`, JSON out.
- **`scheduler/daily_briefing.py`** — `_macro_block()` injected into the US + KR
  prompts (degrades to `""` on any error, never blocking the briefing).
- Cache dir `data/cache/` gitignored. No API key required.

## How

**RSS is the primary, GDELT the fallback** — reversed from the initial plan after
live evidence: GDELT 429s frequently from this host's shared IP (1-req/5s, hit by
other traffic) and its broad multilingual query returns noisy off-topic results,
whereas the RSS wires are reliable, English, and editorially curated. GDELT (with
an English-only, single-OR-group query) is used only when RSS yields nothing.
Built TDD (13 macro tests + 2 daily-briefing wiring tests); full suite 662 passing.

## Code locations

- `mcp-market-data/macro_news.py`
- `mcp-market-data/tests/test_macro_news.py`
- `stock_cli.py` — `cmd_macro_news` + `macro-news` subparser
- `scheduler/daily_briefing.py` — `_macro_block()` + `{macro_block}` injections
- `.gitignore` (`data/cache/`), `CLAUDE.md`, `README.md`

## Review loop

- **code-reviewer-pro**: 0 critical / 0 warnings — "ship as-is"; its 2 suggestions
  (RSS sort stability, missing-date fallback) overlapped Codex's RSS minor and were
  applied.
- **Codex (gpt-5.5, high)**: [MAJOR] GDELT query wrapped the OR-group in an extra
  outer paren — GDELT forbids nested OR groups → removed the outer wrap. [MINOR]
  cache only handled malformed JSON, not wrong-shape valid JSON → `_read_cache`
  rejects non-dict, `_cache_is_fresh`/`_items_from_cache` catch type errors and
  treat a bad cache as absent. [MINOR] RSS missing/bad pubDate bypassed the cutoff
  and date-string sort ignored time → require a parseable date and sort by the
  tz-aware datetime.
- **Gemini**: unavailable (deprecated free-tier CLI).

## Retrospective

- Live testing changed the design: the research's "GDELT is best free" held in the
  abstract, but the integration evidence (persistent 429 + multilingual noise from
  a shared IP) made editorial RSS the better primary. Lesson: validate a chosen
  source against the actual runtime environment, not just its spec.
- GDELT's graceful degradation (cache + serve-stale + empty-not-crash) means a
  throttle or outage never blocks a briefing — the value of building the resilience
  in from the start.
