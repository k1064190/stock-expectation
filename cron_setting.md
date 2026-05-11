# Cron Setup — Stock Expectation

Installed on **2026-05-11 by Claude Code session**. Documents the active scheduled tasks, the rationale for each, and how to inspect / modify / disable them.

## Active schedule (all times KST)

| Schedule | Job | Mode | Purpose |
|---|---|---|---|
| Mon-Fri 07:00 | `scheduler/daily_briefing.py --market KR` | claude-code | KR market briefing (before 09:00 open) |
| Mon-Fri 21:00 | `scheduler/daily_briefing.py --market US` | claude-code | US market briefing (US pre-market opens 22:00 KST) |
| Daily 00:00 | `scheduler/outcome_tracker.py` | pure-Python | Judge previous day's HIT/MISS/EXPIRED |
| Sunday 22:00 | `scheduler/weekly_calibration.py` | pure-Python | Weekly calibration report + 12-week trend |

System TZ on this host is already `Asia/Seoul`. The crontab also declares `TZ=Asia/Seoul` defensively so a host migration to a different TZ won't silently shift the schedule.

## Why these times / days

- **KR briefing weekdays 07:00:** KOSPI/KOSDAQ regular hours start 09:00 KST. 2-hour buffer for skim + position adjustment.
- **US briefing weekdays 21:00:** US pre-market starts 22:00 KST (= 09:00 ET pre-market). Regular hours 23:30 KST onwards.
- **Outcome tracker daily 00:00 (incl. weekends):** Runs every day so Friday closes are judged Saturday morning rather than waiting to Monday. Weekend runs are cheap (KR/US markets closed → most predictions stay open).
- **Weekly calibration Sunday 22:00:** End-of-week reflection time before next week's trading. Pure Python read from `predictions.db`, writes report + trend JSON. No LLM cost.

## Mode choice — claude-code (not API)

The daily briefings run via `claude -p` (Claude Code CLI in headless mode), **not** the Anthropic API. Reason:

- `ANTHROPIC_API_KEY` is NOT set in `.env` on this host.
- `claude` CLI v2.1.138 is installed at `/home/cwh/.local/bin/claude` and authenticated (the install was verified with a `claude -p "ping"` smoke test on 2026-05-11).
- Cost: $0 (Claude Code subscription covers the headless usage).

To switch to API mode later: set `ANTHROPIC_API_KEY` in `.env`, run `uv sync --extra api`, then edit the crontab via `crontab -e` and replace `--mode claude-code` with `--mode api` on the two daily_briefing lines.

## Logs

All cron output → `~/logs/stock-expectation/`:

- `briefing_kr.log` — KR daily briefing stdout/stderr
- `briefing_us.log` — US daily briefing
- `outcome_tracker.log` — nightly outcome judging
- `weekly_calibration.log` — Sunday weekly aggregator

Each line redirects via `>> $LOG_DIR/<name>.log 2>&1` so both stdout and stderr land in the same file. Logs are **append-only** (no rotation) — if growth becomes a concern, add `logrotate` later.

## Telegram delivery

Enabled. `.env` carries `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`; `telegram_sender.py` automatically attaches alerts for:

- HIT / MISS / EXPIRED outcomes from `outcome_tracker.py` (each prediction = 1 message)
- Daily briefing summaries from `daily_briefing.py`

Each alert header now includes the company name when resolvable (added 2026-05-11). Example:

```
✅ *HIT* | 005380 현대차 (KR)
Entry: 505000.00 → 646000.00 (+27.9%)
Direction: BULL | Confidence: 72%
Thesis: …
```

To disable Telegram per-job, comment the `TELEGRAM_BOT_TOKEN` line in `.env` (the sender silently no-ops when keys are missing). To disable globally, remove both keys.

## Crontab content (literal)

```
# ============================================================
# Stock Expectation — Scheduled Tasks
# Installed by Claude Code session on 2026-05-11.
# All times in KST (Asia/Seoul). See cron_setting.md for details.
# ============================================================

SHELL=/bin/bash
PATH=/home/cwh/.local/bin:/usr/local/bin:/usr/bin:/bin
TZ=Asia/Seoul
PROJECT=/home/cwh/projects/stock-expectation
LOG_DIR=/home/cwh/logs/stock-expectation

# KR market briefing — weekdays 07:00 KST (before KR market opens 09:00)
0 7 * * 1-5 cd $PROJECT && uv run python scheduler/daily_briefing.py --market KR --mode claude-code >> $LOG_DIR/briefing_kr.log 2>&1

# US market briefing — weekdays 21:00 KST (US pre-market opens 22:00 KST)
0 21 * * 1-5 cd $PROJECT && uv run python scheduler/daily_briefing.py --market US --mode claude-code >> $LOG_DIR/briefing_us.log 2>&1

# Outcome tracker — every day 00:00 KST (judges previous day's closes)
0 0 * * * cd $PROJECT && uv run python scheduler/outcome_tracker.py >> $LOG_DIR/outcome_tracker.log 2>&1

# Weekly calibration aggregator — Sunday 22:00 KST
0 22 * * 0 cd $PROJECT && uv run python scheduler/weekly_calibration.py >> $LOG_DIR/weekly_calibration.log 2>&1
```

## How the cron environment differs from your shell

Cron starts each job in a near-empty environment:

- `PATH` is forced via crontab to include `/home/cwh/.local/bin` (so `uv` resolves)
- `TZ=Asia/Seoul` ensures time-related Python code is consistent
- Working dir is set per-job via `cd $PROJECT`
- `python-dotenv` (already imported at every scheduler entry point) loads `.env` from `$PROJECT` so API keys are present without explicit `export` lines

If a job ever fails with "command not found", the most likely cause is a binary outside `/home/cwh/.local/bin`. Add the path to the crontab's `PATH=` line and re-install.

## Inspection commands

```bash
# Show currently-installed crontab
crontab -l

# Check cron daemon is running
systemctl is-active cron

# Tail the most recent log
tail -f ~/logs/stock-expectation/outcome_tracker.log

# Show when each job ran most recently
grep "$(date +%Y-%m-%d)" ~/logs/stock-expectation/*.log | tail -20

# Quickly verify nothing crashed
ls -la ~/logs/stock-expectation/
wc -l ~/logs/stock-expectation/*.log
```

## Modification

```bash
# Edit interactively
crontab -e

# Replace from file
crontab /path/to/new-crontab.txt

# Remove all (DANGER — disables all jobs)
crontab -r

# Re-install the version in this repo (if you ever update scheduler/crontab.example
# with KST-correct times — currently it's UTC-based and would shift schedules)
crontab scheduler/crontab.example   # NOT recommended until that file is fixed
```

## Smoke-test record (this session)

Manual run on 2026-05-11 19:48 KST to verify the cron commands actually work:

- `scheduler/outcome_tracker.py` → exit 0, 139 predictions evaluated (51 HIT / 19 MISS / 7 EXPIRED / 62 still open / 0 errors), 9 Telegram alerts sent successfully (1 US + 8 KR).
- FMP returned 403 on US ticker fetches; yfinance fallback engaged automatically and outcomes resolved correctly.
- Telegram delivery confirmed via the 200 OK responses logged for each alert call.

Other jobs (briefings, weekly calibration) are not smoke-tested but will fire on their first scheduled time. Watch `~/logs/stock-expectation/` after each scheduled time on the first day to confirm.

## Known caveats / future work

1. **PyKRX `test_kr_fundamentals_fixed` failure** (HANDOFF §11.E) is unrelated to cron and does not affect any of these jobs.
2. **No log rotation** — files grow indefinitely. Add `logrotate` if `~/logs/stock-expectation/` exceeds a few hundred MB.
3. **No retry-on-failure** at the cron level — if `outcome_tracker.py` happens to fail mid-run, the next day's 00:00 run will pick up the same `OPEN` predictions and re-evaluate. Idempotent by design.
4. **`scheduler/crontab.example` in the repo is UTC-based** and would mis-schedule on a KST host like this one. The actively-installed crontab (snapshotted above) supersedes it. Cleanup item: align the example to KST or add an explicit `CRON_TZ=UTC` line in the example.
5. **Claude Code subscription is required** for daily briefings. If you revoke `claude` CLI auth (e.g. `claude /logout`), both briefing jobs will fail silently until re-auth or switching to `--mode api`.

## Cleanup / rollback

Disable all four jobs:

```bash
crontab -r          # removes installed crontab entirely
rm -rf ~/logs/stock-expectation/   # optional: remove logs
```

Selective disable: `crontab -e`, comment out the lines you don't want, save.
