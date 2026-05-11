# News & Disclosure API Comparison

For the Stage 2 data-layer integration. All data current as of 2026-05.

## US / global

| API | Free tier | Auth | Sentiment | Endpoint | Notes |
|---|---|---|---|---|---|
| **Finnhub** | 60 calls/min | API key (free signup at finnhub.io) | No | `GET /api/v1/company-news?symbol=X&from=YYYY-MM-DD&to=YYYY-MM-DD` | Headline volume + URL. Rate limit is generous; we'll hit this for 5–10 tickers per `/expect ALL` run with room to spare. |
| **Alpha Vantage** | 25 req/day, 5/min | API key (free signup at alphavantage.co) | **Yes — ticker-level avg sentiment + per-article score** | `GET /query?function=NEWS_SENTIMENT&tickers=XX` | Score range -1.0 .. +1.0 (article level), -0.35 .. +0.35 (ticker average). 25/day means we use it sparingly — only for finalist tickers, not the full discovery set. |
| **FMP** | 250/day (existing key in repo) | `FMP_API_KEY` already present | No | `GET /api/v3/stock_news?tickers=X&limit=N` | Already authenticated. Backup if Finnhub limits hit. Headlines only. |
| **yfinance** | Unlimited (unofficial) | None | No | `yf.Ticker(t).get_news()` | Free, no key, but feed quality is shallow and unstable. Used as last-resort fallback. |
| **Polygon Ticker News** | Paid only above 5/min | API key | No (separate sentiment endpoint paid) | `GET /v2/reference/news?ticker=X` | Excluded for cost. |
| **Benzinga** | Paid | API key | Yes | `GET /api/v2/news` | Excluded for cost. |
| **Marketaux / StockNewsAPI** | Limited free tiers | API key | Yes (vendor-graded) | varies | Excluded — Finnhub + AV cover the same need with better-known terms. |

### US strategy (Stage 2)

Provider chain in `mcp-market-data/providers/us.py:get_news()`:

1. If `FINNHUB_API_KEY` set → Finnhub `/company-news` for last 7 days, return up to `limit` items.
2. If `ALPHA_VANTAGE_API_KEY` also set → call AV `NEWS_SENTIMENT` for the same ticker; merge `overall_sentiment_score` into Finnhub items by URL (or attach as a separate `ticker_sentiment` field if no URL match).
3. If only `FMP_API_KEY` set → FMP `/stock_news`, return as-is.
4. If no keys → `yfinance.Ticker(t).get_news()`, no sentiment field.

Each `NewsItem` returned with `{headline, source, date, url, sentiment_score: Optional[float], sentiment_label: Optional[str]}`.

## Korea

| API | Free tier | Auth | Coverage | Endpoint | Notes |
|---|---|---|---|---|---|
| **Open DART** | Unlimited (rate limit ~100/min) | `OPEN_DART_API_KEY` (free signup at opendart.fss.or.kr) | All 공시 (disclosures) — quarterly/annual reports, dividends, equity changes, M&A, key contracts | `GET https://opendart.fss.or.kr/api/list.json?corp_code=<8-digit>&bgn_de=YYYYMMDD&end_de=YYYYMMDD` | Requires one-time `corp_code` mapping CSV download (`/api/corpCode.xml`). We'll cache to `data/dart_corp_codes.csv`. |
| **Naver Finance (scrape)** | Unlimited | None | General news headlines per ticker | `GET https://finance.naver.com/item/news_news.nhn?code=<6-digit>` | HTML scrape. Pure HTTP + BeautifulSoup. Fragile to layout changes — wrap in try/except, log warnings. No NLP — LLM summarizes. |
| **dartpoint MCP** | Free tier | dartpoint.ai key | Wraps DART | MCP | Excluded — adds a vendor dependency for what is a single REST call. |
| **BrightData Naver SERP** | Paid | API key | News + price + company info | proprietary | Excluded for cost. |
| **OpenDartReader (PyPI)** | — | DART key | Python wrapper for DART | — | We'll consider importing this rather than hand-rolling the DART client; reduces our maintenance burden. |

### KR strategy (Stage 2)

Provider chain in `mcp-market-data/providers/kr.py`:

- `get_news(ticker)` → scrape Naver `news_news.nhn`, parse top-N items (title, source, date, link). No sentiment.
- `get_disclosures(corp_code, since=7d)` → Open DART `/api/list.json`. Filter to material reports (이상 비중공시 제외). Returns `{rcept_no, report_nm, flr_nm, rcept_dt, summary}` where `summary` is the report name + filer.

For ticker → corp_code lookup, ship `data/dart_corp_codes.csv` (one-time download via `bin/stock-cli setup dart` — to be added in Stage 2).

## Score-table impact

How these surface in `/expect` (Stage 4 spec):

```
NEWS_SCORE (max 5):
  Sentiment (Alpha Vantage, US only, if available):
    avg score > +0.15  → +2
    avg score 0..+0.15 → +1
    avg score -0.15..0 → -1
    avg score < -0.15  → -2
  Headline volume (Finnhub or Naver):
    ≥ 3 headlines past 7d → +1
  Negative keyword scan (bankrupt, fraud, lawsuit, downgrade): -2 hard cap
  KR disclosure flag (감자, 유상증자, 관리종목): -2 hard cap
```

For US tickers without `ALPHA_VANTAGE_API_KEY`, sentiment-derived points default to 0; only headline-volume and keyword scans contribute.
