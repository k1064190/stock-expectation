# Stock Expectation

Stock prediction system with track record for US and Korean markets.

> **Status:** The `/expect` redesign was merged into `master` via [PR #2](https://github.com/k1064190/stock-expectation/pull/2)
> on 2026-05-11 (squash commit `d2ef519`). For full background on the staged work — stage outcomes, test inventory,
> code-review history, known issues, operator runbook, and pending decision points — read
> [`docs/HANDOFF.md`](docs/HANDOFF.md). The live-E2E follow-up patches are documented in
> [`docs/stage-4.1/e2e-followups.md`](docs/stage-4.1/e2e-followups.md). Remaining decision points
> (Stage 7-A/7-B live verification, PyKRX xfail, skill catalog second pass) are in HANDOFF.md §11.

## Architecture

CLI-first with `bin/stock-cli` + Claude Code skills + lightweight Python scheduler:
- `stock_cli.py` — single CLI entry point with all subcommands
- `bin/stock-cli` — bash wrapper that runs stock-cli via `uv run`
- `mcp-market-data/providers/` — US and KR market data providers (name kept for legacy reasons)
- `mcp-prediction-store/` — Prediction schema, DB CRUD, metrics computation
- `portfolio/` — Manual portfolio tracking, trade recording, evaluation
- `.claude/skills/` — Claude Code skills that invoke `bin/stock-cli` via Bash
- `scheduler/` — Automated daily briefings and outcome tracking

## Python Environment — uv

This project uses `uv` for dependency management.

```bash
# First-time setup (installs Python 3.11 and all deps)
uv sync

# Include dev dependencies (pytest)
uv sync --extra dev

# Include Anthropic API deps (only for --mode api)
uv sync --extra api

# Include skill script dependencies (jsonschema, pyyaml, scipy)
uv sync --extra skills

# Run any command in the project venv
uv run <command>
```

## Using the CLI

```bash
# Via wrapper (recommended)
./bin/stock-cli --help
./bin/stock-cli price NVDA --market US --days 30
./bin/stock-cli price 005930 --market KR --days 10
./bin/stock-cli track-record --days 30

# Batch commands (multiple tickers at once)
./bin/stock-cli price-batch AAPL,MSFT,NVDA --market US --days 30
./bin/stock-cli price-batch 005930,000660,035420 --market KR --days 30
./bin/stock-cli fundamentals-batch AAPL,MSFT,NVDA --market US
./bin/stock-cli fundamentals-batch 005930,000660 --market KR

# Directly via uv
uv run stock-cli --help
```

All commands output JSON for easy parsing.

### Batch commands

`price-batch` and `fundamentals-batch` accept comma-separated ticker lists.
Designed for skills that need data for multiple stocks (screeners, breadth
analysis, sector rotation). Uses yfinance `download()` for efficient bulk
fetching instead of sequential per-ticker calls.

### Portfolio tracking

```bash
# Create portfolios (one-time setup)
./bin/stock-cli portfolio create --market KR --name "Toss KR"
./bin/stock-cli portfolio create --market US --name "Toss US"

# Record trades
./bin/stock-cli portfolio buy 005930 --qty 10 --price 55000 --market KR
./bin/stock-cli portfolio sell 005930 --qty 5 --price 60000 --market KR --date 2026-04-01

# Import from CSV
./bin/stock-cli portfolio import trades.csv --market KR --dry-run

# View positions and evaluate
./bin/stock-cli portfolio positions --market KR
./bin/stock-cli portfolio report --market KR
./bin/stock-cli portfolio risk --market KR
./bin/stock-cli portfolio vs-predictions --market KR
./bin/stock-cli portfolio advice --market KR
```

Portfolio data stored in `data/portfolio.db` (SQLite, WAL mode). Separate from predictions.db.

## Running Tests

```bash
uv run pytest -m "not network"   # fast unit tests only
uv run pytest                     # includes network tests (hits real APIs)
```

## Scheduler — Two Modes

### Claude Code mode (default, no API cost)

Uses `claude -p` CLI. Claude Code uses `bin/stock-cli` via Bash to fetch data
and log predictions. Reads skills from `.claude/skills/` automatically.

```bash
uv run python scheduler/daily_briefing.py --market US
uv run python scheduler/daily_briefing.py --market KR
uv run python scheduler/daily_briefing.py --market ALL
```

### Anthropic API mode (fallback)

Calls API directly. Pre-fetches data and injects into prompt. Predictions
returned as JSON, parsed and logged by the script. Requires `ANTHROPIC_API_KEY`
and `uv sync --extra api`.

```bash
uv run python scheduler/daily_briefing.py --market US --mode api
```

### Outcome tracker (no LLM needed)

Pure Python: fetches prices, scores predictions as HIT/MISS/EXPIRED.

```bash
uv run python scheduler/outcome_tracker.py
```

### Cron setup

```bash
crontab scheduler/crontab.example
```

## Database

- `data/predictions.db` — Predictions (auto-created on first run). WAL mode.
- `data/portfolio.db` — Portfolio trades (auto-created on first use). WAL mode.

## API Keys

- `FMP_API_KEY` — Financial Modeling Prep (optional, free tier 250 calls/day)
- `FINNHUB_API_KEY` — US news headlines (optional, free 60 req/min) — used by `stock-cli news --market US`
- `ALPHA_VANTAGE_API_KEY` — US news sentiment scores (optional, free 25/day) — merged into Finnhub items by URL match
- `OPEN_DART_API_KEY` — KR regulatory disclosures (optional, free) — used by `stock-cli disclosure`. First call downloads the corp_code mapping to `data/dart_corp_codes.csv`.
- `NAVER_CLIENT_ID` + `NAVER_CLIENT_SECRET` — 네이버 검색 API (뉴스) for KR per-ticker news (optional, free ~25,000 calls/day). Issued at Naver Developers (developers.naver.com). When set, `stock-cli news --market KR` queries the official Search API by the ticker's Korean name; otherwise it falls back to the legacy finance.naver.com HTML scrape. No sentiment (computed downstream).
- **(no key needed)** Global macro/geopolitical news — `stock-cli macro-news` fetches market-moving world news (wars, oil, central banks, tariffs) from wire-service RSS (BBC/CNBC/Yonhap) with a GDELT fallback; no API key. GDELT is rate-limited per-IP (1 req/5s) so RSS is the primary; wired into the daily briefing's macro-context block.
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` — for Telegram delivery
- `TOSS_CLIENT_ID` + `TOSS_CLIENT_SECRET` — 토스증권 공식 Open API (OAuth2) for `portfolio sync`. Issued in the Toss Securities app (더보기 → Open API). When set, `portfolio sync` uses the official API; otherwise it falls back to the legacy `tossctl` CLI. Optional `TOSS_OPENAPI_BASE_URL` overrides the default base URL.
- `ANTHROPIC_API_KEY` — only needed with `--mode api`

## Skills

After the Stage 3 cleanup, the active set is **31 skills** under `.claude/skills/`. Specialized or rarely-used skills (edge-pipeline, kanchi-dividend, US 13F, etc.) live under `.claude/skills/_archived/` and are not loaded by Claude Code — see `.claude/skills/_archived/README.md` to revive one. Eleven skills (downtrend-duration, strategy-pivot-designer, scenario-analyzer, dual-axis-skill-reviewer, etc.) were deleted outright.

### Active groups

- **Core flow** (4): `expect` (multi-horizon BUY/SELL recommender — see Stage 4 redesign), `daily-briefing`, `prediction-review`, `stock-research`
- **Portfolio** (5): `portfolio-eval`, `portfolio-manager`, `position-sizer`, `toss-sync`, `trader-memory-core`
- **Regime + breadth** (6): `macro-regime-detector`, `market-breadth-analyzer`, `uptrend-analyzer`, `market-top-detector`, `ftd-detector`, `sector-analyst`
- **Screeners** (5): `vcp-screener`, `canslim-screener`, `finviz-screener`, `base-breakout-screener`, `earnings-trade-analyzer`
- **Calendars + analysis** (5): `earnings-calendar`, `economic-calendar-fetcher`, `theme-detector`, `technical-analyst`, `stock-analysis`
- **Korea-specific** (1): `korean-market-analysis`
- **Meta + ops** (5): `backtest-expert`, `data-quality-checker`, `signal-postmortem` (revived for Stage 6), `retrospect`, `init`

### Key multi-skill workflows (post-cleanup)

- **Stock-by-stock prediction** (most common): `/expect` orchestrates `bin/stock-cli` (price/news/disclosure) + scoring + prediction logging, calling regime/breadth skills only as gates.
- **Daily macro snapshot**: `daily-briefing` → `market-top-detector` + `ftd-detector` + `theme-detector` → predictions logged.
- **Thesis-driven trading**: screener → `trader-memory-core` (lifecycle) → `position-sizer`.
- **Weekly calibration loop** (Stage 6): `signal-postmortem` + `prediction-review` → weekly calibration report.

### Running imported skill scripts directly

Scripts that call FMP/FINVIZ APIs can still be run standalone (US only):
```bash
uv run python .claude/skills/{skill}/scripts/{script}.py --api-key $FMP_API_KEY
```
