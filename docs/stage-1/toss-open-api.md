# Stage 1 — Toss official Open API portfolio sync

## Why
`portfolio sync` depended on the unofficial `tossctl` CLI (browser/QR login,
~weekly session expiry, a separate session-expiry monitor + Telegram alert).
Toss Securities now offers an official Open API (OAuth2, REST/JSON), so we can
sync holdings with server-side credentials and automatic token refresh — no
browser, no session babysitting.

## What
- New `portfolio/toss_api.py`: OAuth2 client-credentials client for the Toss
  Open API. Issues/caches a token, lists accounts, reads holdings, and
  normalizes each holding into the dict shape `reconcile()` already consumes.
- `portfolio sync` now prefers the official API and falls back to `tossctl`
  when credentials are absent. New `--source {auto,toss-api,tossctl}` flag;
  output JSON reports the `source` actually used.
- Removed the now-unnecessary session-expiry monitor
  (`scheduler/toss_auth_check.py` + test, the 07:30 cron line, and doc
  references) — OAuth token refresh replaces it.
- Credentials via `.env`: `TOSS_CLIENT_ID`, `TOSS_CLIENT_SECRET`
  (optional `TOSS_OPENAPI_BASE_URL`).

## How
The reconciliation logic, DB schema, and ticker/market normalization are
data-source agnostic and were left untouched. The new code only sits at the
boundary: `toss_api.fetch_toss_positions_api()` produces the same normalized
positions `tossctl` used to, so `toss_sync.reconcile()` is unchanged. A thin
dispatcher `toss_sync.fetch_positions(source)` selects the source.

The exact endpoints, auth method, and field names were confirmed against the
canonical spec at `https://openapi.tossinvest.com/openapi-docs/latest/openapi.json`
(initial research guesses — Basic-header auth, `/v1/...` paths — were wrong and
corrected): token via form-body `client_id`/`client_secret`, `GET /api/v1/accounts`
(`result` = list of Account with `accountSeq`), `GET /api/v1/holdings` with header
`X-Tossinvest-Account: {accountSeq}` (`result.items` = list of HoldingsItem with
`symbol`, `name`, `marketCountry`, `currency`, `quantity`, `lastPrice`,
`averagePurchasePrice`).

Verified live: token issued, BROKERAGE account, 18 holdings (6 KR + 12 US) synced
correctly via `portfolio sync --source toss-api --dry-run`.

## Code locations
- `portfolio/toss_api.py` — Open API client + adapter
- `portfolio/toss_sync.py` — `fetch_positions()` dispatcher (reconcile unchanged)
- `stock_cli.py` — `cmd_portfolio_sync` + `--source` flag (`portfolio sync`)
- `portfolio/tests/test_toss_api.py` — client/adapter/dispatcher unit tests
- Removed: `scheduler/toss_auth_check.py`, `scheduler/tests/test_toss_auth_check.py`
- Docs: `CLAUDE.md` (API Keys), `.claude/skills/toss-sync/SKILL.md`,
  `scheduler/crontab.example`, `cron_setting.md`, `README.md`

## Retrospective
The boundary-only design kept the change surgical — reconcile, DB, and tests
for the existing logic were untouched. Pulling the real `openapi.json` early
(instead of trusting research guesses) was the key unblock: auth method,
path prefix, and the `accountSeq` header were all different from the initial
assumptions. The credential mix-up (ID/secret swapped) surfaced as a clean
`invalid_client`, so verifying against the live spec quickly isolated it.
