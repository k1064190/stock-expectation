# Stock Expectation

Stock prediction system with track record for US and Korean markets.

## Architecture

CLI-first with `bin/stock-cli` + Claude Code skills + lightweight Python scheduler:
- `stock_cli.py` — single CLI entry point with all subcommands
- `bin/stock-cli` — bash wrapper that runs stock-cli via `uv run`
- `mcp-market-data/providers/` — US and KR market data providers (name kept for legacy reasons)
- `mcp-prediction-store/` — Prediction schema, DB CRUD, metrics computation
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

# Directly via uv
uv run stock-cli --help
```

All commands output JSON for easy parsing.

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

SQLite at `data/predictions.db` (auto-created on first run). WAL mode enabled.

## API Keys

- `FMP_API_KEY` — Financial Modeling Prep (optional, free tier 250 calls/day)
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` — for Telegram delivery
- `ANTHROPIC_API_KEY` — only needed with `--mode api`
