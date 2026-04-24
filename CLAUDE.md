# Stock Expectation

Stock prediction system with track record for US and Korean markets.

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
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` — for Telegram delivery
- `ANTHROPIC_API_KEY` — only needed with `--mode api`

## Imported Trading Skills

51 trading skills imported from claude-trading-skills, organized into three tiers:

- **Pure logic skills** (29): Analysis frameworks, methodology guides, risk management
  tools. Work for any market without data API calls. Includes edge-* pipeline (6 skills),
  position-sizer, technical-analyst, backtest-expert, scenario-analyzer, etc.
- **Adapted skills** (13): Originally US-only, adapted to use `bin/stock-cli` (price-batch,
  fundamentals-batch) for dual US/KR support. Original FMP scripts preserved in scripts/
  for reference. Includes vcp-screener, sector-analyst, macro-regime-detector, etc.
- **US-only skills** (9): Depend on US-specific data sources (SEC 13F, FMP earnings,
  Alpaca, FINVIZ, IRS). Marked with "US only" banner. No KR equivalent.

### Key multi-skill workflows

- **Edge Research Pipeline**: edge-candidate-agent → edge-hint-extractor →
  edge-concept-synthesizer → edge-strategy-designer → edge-strategy-reviewer →
  edge-pipeline-orchestrator
- **Earnings Momentum**: earnings-trade-analyzer → pead-screener → technical-analyst
- **Dividend Portfolio**: value-dividend-screener → dividend-growth-pullback-screener →
  kanchi-dividend-sop
- **Thesis-Driven Trading**: screener → trader-memory-core → position-sizer

### Running imported skill scripts directly

Scripts that call FMP/FINVIZ APIs can still be run standalone (US only):
```bash
uv run python .claude/skills/{skill}/scripts/{script}.py --api-key $FMP_API_KEY
```
