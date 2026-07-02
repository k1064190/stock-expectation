# Stage 22 — Restore the R3 catalyst-event gate (FMP migration + yfinance fallback + visible fail-open)

## Why

The R3 event gate had been silently dead: FMP's legacy `/api/v3/earning_calendar`
and `/api/v3/economic_calendar` return **403 Forbidden** for this account's key
(legacy endpoints blocked after FMP's plan migration). Because the gate is
fail-open by design, every failure collapsed into `gate_unavailable: true` with
zero caps/trims, and every LIVE prediction just logged "R3 unavailable=0" —
earnings-imminent WATCH caps and FOMC/CPI macro trims were **never** applied,
with nothing in the output distinguishing "feed dead" from "no events".

## What

- **Endpoint migration.** Probed with the real key: `/stable/earnings-calendar`
  → 200 (works; same `symbol`/`date` keys, no `time`/`companyName`), `/stable/economic-calendar`
  → 402 (needs a paid plan), both v3 endpoints → 403. Migrated `FMP_BASE_URL`
  to `/stable` in `events.py` and the earnings URL in `pre_surge_discovery.py`.
- **Keyless yfinance earnings fallback.** New `_fetch_earnings_fallback_yf`
  fetches the next earnings date per candidate ticker via `yf.Ticker(t).calendar`
  and feeds the same per-ticker cap/trim logic when the FMP earnings fetch
  fails (or the key is missing). Raises only on total outage so an empty result
  is never confused with "no imminent earnings".
- **Visible partial availability.** `EventGate` gains `earnings_source`
  ("fmp" / "yfinance" / null) and `macro_available` (bool); each degradation
  appends an explanatory note. `gate_unavailable: true` now means "no source
  produced data" — partial outages (e.g. the macro 402) keep the gate live.
  The daily-briefing prompt block renders the notes.
- **Key redaction.** httpx error strings embed the request URL including
  `apikey=…`; new `_redact_key` scrubs it before the message reaches `notes`
  (JSON output / LLM prompts) or logs.
- **Timeline split.** `catalyst timeline` fetches the two calendars
  independently so a macro 402 with `--include-macro` no longer hides a
  working earnings timeline.
- Docs updated: `catalyst-event-gate/SKILL.md` (availability fields table,
  stable endpoints, fallback), `expect/SKILL.md` (Step 4 + R3 fail-open
  bullet), `daily-briefing/SKILL.md` (R3 paragraph).

## How

`evaluate_gate` was restructured from one all-or-nothing try/except into two
independent sides: earnings (FMP → yfinance fallback, US only) and macro (FMP
only, no keyless source exists). Each side records its outcome on the gate;
the neutral-verdict short-circuit fires only when both sides are down. The
fallback fetcher is injectable (`fetch_earnings_fallback`) following the
existing test-stub convention. Live-verified read-only from the main checkout:
`earnings_source: "fmp"` with the real key, macro 402 flagged in notes (key
redacted), and the yfinance path returning CAT's real next earnings date.

## Code locations

- `mcp-market-data/events.py` — `FMP_BASE_URL` (stable), `EventGate.earnings_source`
  / `macro_available`, `_redact_key`, restructured `evaluate_gate` (~L436-530),
  `_fetch_earnings_fallback_yf`, stable-endpoint fetch wrappers.
- `stock_cli.py` — `cmd_catalyst_timeline` split fetches (~L875-900),
  `cmd_catalyst_gate` docstring, `_redact_key` import.
- `scheduler/daily_briefing.py` — `_format_event_gate_for_prompt` renders notes.
- `scheduler/pre_surge_discovery.py` — earnings URL migrated to stable.
- `mcp-market-data/tests/test_events.py` — 9 new tests (403→fallback caps,
  missing-key fallback, macro-down visible, both-down partial, redaction,
  yf-fallback unit tests), 2 fail-open tests updated to inject the fallback.
- `scheduler/tests/test_daily_briefing.py` — partial-availability note render.

## Retrospective

Fail-open without per-source visibility is indistinguishable from "no risk" —
any future gate should ship with source/availability fields from day one. The
live probe also caught an httpx-error API-key leak into gate notes that had
existed since the original implementation.

## Review

Internal review (round 1) found **no critical issues**; one warning applied,
one cosmetic suggestion dismissed:

- **Applied — past-date filter in the yfinance fallback.**
  `_fetch_earnings_fallback_yf` used `min(dates)` unfiltered, so a stale
  already-past earnings date from yfinance could shadow the real future date
  (and emit a row `build_timeline` would drop anyway). Fix: the function now
  takes `asof` (deterministic + testable) and selects the nearest date
  **on/after** `asof`; if every date for a ticker is past, the ticker is
  treated as "no scheduled earnings". Deliberate deviation from the literal
  "skip `<= asof`" suggestion: a **same-day** (== asof) date is kept, because
  td == 0 still carries binary risk (WATCH cap) and must match the FMP-side
  behavior asserted by `test_evaluate_gate_earnings_same_day_caps_watch`.
  Covered by `test_yf_fallback_skips_past_dates` (past-shadowing + past-only +
  same-day-kept) and `test_evaluate_gate_yf_past_only_dates_no_cap`
  (end-to-end: FMP 403 → yfinance past-only → neutral verdict, no bogus cap).
- **Dismissed — `_redact_key` not matching a quoted `apikey="KEY"` form.**
  Cosmetic; httpx error strings never produce that format (the key appears as
  a bare URL query parameter), so the pattern is unreachable in practice.

### Codex bot review (round 1, PR #53 @ cf6e53c) — 4 findings, all resolved

1. **Duplicate of the past-date filter warning above** (with the nuance that
   past dates must be filtered *before* `min()` selects the nearest candidate,
   so a stale lower bound of an estimated window cannot shadow a still-imminent
   upper bound). Already implemented that way: `upcoming = [d for d in dates
   if d >= asof_d]` runs before `min(upcoming)`.
2. **(P2) Misleading "no imminent earnings/macro risk" prompt line when
   `macro_available` is false.** Fixed in
   `daily_briefing._format_event_gate_for_prompt`: with the macro feed down
   the line now reads "no imminent earnings risk among candidates; macro event
   feed unavailable — macro risk unknown" (symmetric wording added for the
   earnings-feed-down + macro-up case, same falsehood class). Tests:
   `test_event_gate_block_macro_unavailable_no_false_no_risk_claim`, updated
   `test_event_gate_block_no_risk_note` /
   `test_event_gate_block_renders_partial_availability_notes`.
3. **(P2) Per-ticker yfinance lookup failures silently treated as "no
   scheduled earnings".** `_fetch_earnings_fallback_yf` now returns
   `(rows, failed_tickers)`; `evaluate_gate` appends a note per failed ticker
   ("yfinance lookup failed for XYZ — earnings risk unknown"), keeping
   fail-open. Tests: `test_evaluate_gate_partial_yf_failure_noted`, updated
   `test_yf_fallback_partial_errors_keep_survivors`.
4. **(P3) `catalyst timeline` marked `gate_unavailable` on earnings failure
   even when `--include-macro` succeeded.** Aligned with `evaluate_gate`
   semantics: unavailable only when *every requested* calendar failed;
   per-source failure notes kept. Tests: new `tests/test_catalyst_cli.py`
   (in-process, fetchers monkeypatched — partial macro fail, partial earnings
   fail, all fail, earnings-only-requested fail).

### Second-opinion review (Gemini/antigravity, PR #53 @ 289ff7a) — 2 blockers + 2 should-fix + 1 nit

1. **REFUTED (blocker claim): `cmd_catalyst_gate` missing the availability
   fields.** The gate CLI does not hand-build its JSON — it prints
   `asdict(gate)` (`stock_cli.py`), and the `EventGate` dataclass includes
   `earnings_source` / `macro_available` / `notes`, so the CLI output always
   carried them (also confirmed live on 2026-07-02). Locked permanently by the
   new `test_gate_json_includes_availability_fields`.
2. **Fixed (blocker): API-key leak in `pre_surge_discovery` error logs.**
   `fetch_earnings_days_map_us` logged raw `requests` exceptions, whose
   messages embed the full URL incl. `apikey=…`. Now redacted via
   `_redact_key` (imported from `events`; that dir is already on the module's
   `sys.path`). The other exception paths in the file (benchmark, batch
   price, per-ticker scoring) don't touch the key. Test:
   `test_earnings_fetch_error_log_redacts_api_key` (caplog).
3. **Fixed (should-fix): tz-aware yfinance datetimes.** A tz-aware UTC
   timestamp for an evening US report is already next-day in UTC; `.date()`
   would shift the event a day late. Tz-aware values are now converted to
   `America/New_York` before taking the date (naive values unchanged). Test:
   `test_yf_fallback_tz_aware_evening_uses_us_eastern_date`.
4. **Fixed (should-fix): `pd.NaT` handling.** NaT passes
   `isinstance(..., datetime)` and breaks the `>=` comparison, misclassifying
   a missing date as a per-ticker failure. NaT/NaN entries are now skipped via
   `pd.isna` before normalization — "no scheduled earnings", not a failure.
   Test: `test_yf_fallback_nat_is_no_event_not_failure`.
5. **Fixed (nit): in-process `catalyst gate` JSON test** added
   (`test_gate_json_includes_availability_fields` in
   `tests/test_catalyst_cli.py`) — the test that would have caught item 1 had
   it been real.

### Codex bot review (round 2, PR #53 @ 289ff7a) — 1 finding, resolved

- **(P2) Unqualified "no imminent earnings/macro risk" line despite failed
  per-ticker lookups.** With the earnings side on the yfinance fallback and
  some lookups failed (but no caps and macro available), the prompt line
  contradicted the failure note above it. The no-risk claim in
  `_format_event_gate_for_prompt` is now compositional: failed-lookup tickers
  are excluded explicitly ("no imminent earnings risk among candidates except
  AMD (lookup failed — earnings risk unknown); no imminent macro risk"). The
  failed tickers are parsed from `notes` via the shared
  `events.YF_LOOKUP_FAILED_PREFIX` constant (no magic-string duplication).
  Test: `test_event_gate_block_failed_lookup_excluded_from_no_risk_claim`.
