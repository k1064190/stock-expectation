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

## Review loop

A second-pass review surfaced four P1 correctness defects; all were fixed under
TDD (a failing test added/adjusted for each, then the fix). The EOD/delayed
framing, never-mutate-predictions, and never-raise conventions were preserved
throughout.

**Reviewers.** `code-reviewer-pro` (Claude) reviewed the fix diff; `codex`
(gpt-5.5, high effort) reviewed the implementation diff independently. Codex
confirmed the edge-rollback, dry-run, digest all-or-nothing, and DST math are
correct.

**Codex (gpt-5.5) P1 findings — all fixed:**

1. **State persisted before Telegram delivery** (`watchlist_monitor.py`). The
   rising-edge/cooldown marker (`last_alert_iso`) was committed before the alert
   was actually sent, so a Telegram send FAILURE consumed the edge and
   suppressed the retry. Fix: deliver BEFORE persisting; `_deliver` now returns
   the set of delivered `_state_key`s, and `run_monitor` rolls back each fired
   key not in that set to its pre-fire `_prior` snapshot — leaving it armed for
   the next run. Dry-run keeps edges committed (sends suppressed only). Digest is
   all-or-nothing. Tests: `test_failed_send_does_not_suppress_next_run`,
   `test_successful_send_consumes_edge`.
2. **Shared temp state path under concurrent runs** (`save_state`). All writers
   used a single fixed `.tmp` path, so overlapping runs could clobber it / break
   `os.replace`. Fix: `tempfile.mkstemp` allocates a unique same-dir temp per
   call, then `os.replace`; temp is unlinked on any failure. Test:
   `test_save_state_uses_unique_same_dir_temp`.
3. **OPEN-prediction dedup ordering** (`watchlist_store.py`). OPEN predictions
   were selected without `ORDER BY`, so an older/arbitrary row could mask the
   newest prediction's levels under the `(ticker, market)` first-wins dedup. Fix:
   `ORDER BY created_at DESC, rowid DESC`. (`predictions.id` is a UUID, so
   `rowid` — not `id` — is the insertion-order tiebreaker; this was Codex's LOW
   follow-up, also fixed.) Tests: `test_open_prediction_dedup_keeps_newest`,
   `test_open_prediction_dedup_same_created_at_uses_insertion_order`.
4. **US market-hours/DST gate** (`is_market_open`). The fixed 05:00 KST close
   missed the real ~06:00 KST close during EST. Fix: the US session is computed
   in `America/New_York` (DST-aware) — RTH 09:30–16:00 ET inclusive plus a 30-min
   post-close EOD grace, compared as full datetimes (robust to any future grace
   value). The US cron window was widened to 00–06 KST so the EST close is
   actually covered. Tests: `test_us_open_at_est_close_kst`,
   `test_us_open_est_evening_kst`, `test_us_closed_est_before_open_kst` (the
   pre-existing EDT cases still pass).

**code-reviewer-pro findings — addressed:**

- *Critical: `id()`-based delivered-set matching is fragile.* Accepted. Switched
  to matching by stable `_state_key` strings (`_deliver` returns a `set[str]`).
- *Warning: digest path import/build outside the try.* Accepted. The lazy import
  and message build now sit inside the never-raise `try`.
- *Warning: grace boundary `.time()` could wrap past midnight if grace grows.*
  Accepted. Gate now compares full datetimes, so the grace is robust.

**Dismissed / pushed back:**

- *Codex MEDIUM: Telegram delivery failures aren't counted in
  `summary["errors"]`.* Pushed back. `errors` is, by existing contract and the
  `test_run_none_price_skips_and_counts_error` test, a price-fetch-failure
  counter; conflating delivery failures would muddy that semantic. The new
  edge-rollback is the correct operational remedy (undelivered triggers re-arm
  and retry next run, a strict improvement over the prior silent drop), and
  failures are logged via `logger.warning`. Left out of scope deliberately.

**Verification.** `uv run pytest scheduler/tests/test_watchlist_monitor.py
scheduler/tests/test_watchlist_store.py -q` → 44 passed (36 baseline + 8 new).
Full `scheduler/tests/` (no-network) → 130 passed.
