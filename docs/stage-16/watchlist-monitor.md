# Stage 16 — watchlist-monitor skill

## Why

Predictions and positions already exist, but there was no way to be told when a
ticker actually reached a level of interest — an entry zone, a stop, a target,
or a re-entry trigger. The outcome tracker resolves predictions nightly but is
silent about "price is at your level now". This stage adds a delayed/EOD-ish
trigger alerter so Doctor Cho gets a Korean Telegram nudge when the latest close
touches a watched level, without any new schema in predictions.db.

## What

- A saved-ticker watchlist in a **sibling** DB `data/watchlist.db` (CRUD), plus
  a unified-watchlist builder that merges saved rows + OPEN predictions +
  portfolio positions, deduped by `(ticker, market)` with precedence
  `saved > prediction > position`.
- A pure-Python monitor that fetches the latest price per ticker (never-raise),
  evaluates ENTRY / STOP / TARGET / RE-ENTRY triggers (BULL default, BEAR
  mirrored), and fires Korean Telegram alerts on rising-edge transitions with a
  6h cooldown backstop and re-arm semantics. Market-hours gated (KST), alert-only
  (never mutates predictions).
- CLI `watch add/remove/list/check`, `send_watch_alert` (ENTRY 📥 / REENTRY 🔁),
  an EOD-cadence crontab entry, the SKILL.md, and `.gitignore` entries.

## How

- **Delayed reframing made explicit.** `get_current_price()` returns the last
  close (it calls `get_price_history(days=5)` and takes the last bar); KR PyKRX
  is EOD. This is documented prominently in the SKILL.md, module docstrings, the
  crontab comment, and the alert body ("현재가(지연/종가 기준)"). No pretense of
  intraday.
- **Mirrored existing patterns.** Connection pattern (WAL + busy_timeout +
  `CREATE TABLE IF NOT EXISTS`, optional `db_path`) copied from `portfolio/db.py`.
  Never-raise loop, timezone-aware gate, and Telegram alerting mirror
  `outcome_tracker.py`. CLI subparser group mirrors `portfolio`. Tests mirror
  `test_outcome_tracker.py` style (sys.path inserts, table-driven boundaries).
- **Rising-edge dedup.** `state/watchlist_alerts.json` keyed
  `{source}:{ticker}:{market}:{trigger}` stores `{last_state, last_alert_iso}`.
  Fire only on a transition into the satisfied state; re-arm on transition out;
  6h cooldown as a flip-flop guard. Atomic write via temp + `os.replace`; stale
  keys pruned each run.
- **Single-point entry band.** Predictions/positions carry one entry price, so
  ENTRY uses a ±1% band; saved rows use the explicit `[entry_low, entry_high]`.
- **Delivery.** One focused Korean message per trigger; a single batched digest
  when more than 3 fire. Sends are deferred to after the evaluation loop so the
  individual-vs-digest choice is made once from the final fired count (avoids the
  double-send trap of deciding mid-loop).
- **TDD.** Store tests then monitor tests written before/with implementation;
  36 new tests, all green.

## Code locations

- `scheduler/watchlist_store.py` — `get_connection`, `add_watch`/`remove_watch`/
  `list_watches`, `load_unified_watchlist`, `WatchTarget`, `POSITION_DEFAULT_STOP_PCT`.
- `scheduler/watchlist_monitor.py` — `is_market_open`, `evaluate_triggers`,
  `should_fire`, `run_monitor`, `_deliver`, `load_state`/`save_state`/`_prune_state`.
- `scheduler/telegram_sender.py:163-...` — `_ALERT_EMOJI` (ENTRY/REENTRY added),
  `send_watch_alert`, `_watch_level_for`.
- `stock_cli.py` — `cmd_watch_add/remove/list/check` + the `watch` subparser
  group; added `PROJECT_ROOT` to `sys.path` so `import scheduler.*` resolves.
- `scheduler/crontab.example` — KR + US (split DST-safe) watchlist-monitor lines.
- `scheduler/tests/test_watchlist_store.py`, `scheduler/tests/test_watchlist_monitor.py`.
- `.claude/skills/watchlist-monitor/SKILL.md`, `.gitignore`.

## Retrospective

- The single-pass collect-then-deliver design removed an ugly mid-loop digest
  decision; carrying the `WatchTarget` in the trigger dict and stripping it
  before return keeps the summary JSON-serializable while still enriching the
  per-trigger message.
- `scheduler` is a source package, not installed — the CLI needed `PROJECT_ROOT`
  on `sys.path` (the same insert the monitor already does). Worth remembering for
  any future CLI command that reaches into `scheduler/`.
- Carry forward: the market-hours gate is intentionally coarse (a generous
  22:30–05:00 KST US window across DST). If false off-hours fetches ever matter,
  tighten with a real ET-based calculation, but for an EOD alerter the loose
  window is the right call.
