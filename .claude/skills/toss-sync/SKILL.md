---
name: toss-sync
description: Sync portfolio from Toss Securities. Use when user mentions "토스", "Toss", "토스에서", "토스 동기화", "sync from toss", "toss 조회", or wants to update their portfolio from their brokerage account.
---

# Toss Securities Portfolio Sync

Sync holdings from Toss Securities into the local portfolio tracker. The
official Toss Open API (OAuth2) is the default source; the legacy `tossctl`
CLI is used as a fallback when API credentials are not configured.

## Prerequisites

### Official Open API (preferred)

Set both credentials in `.env` (issued in the Toss Securities app:
더보기 → Open API → 신청):
```
TOSS_CLIENT_ID=...
TOSS_CLIENT_SECRET=...
# TOSS_OPENAPI_BASE_URL=https://openapi.tossinvest.com  # optional override
```
No browser, QR scan, or session re-login is needed — the OAuth token is
issued and refreshed automatically.

### tossctl fallback (only if no API credentials)

- Install: `curl -fsSL https://raw.githubusercontent.com/JungHoonGhae/tossinvest-cli/main/install.sh | sudo sh`
- Log in: `tossctl auth login` (requires Chrome + QR code scan from Toss app)
- Check status: `tossctl doctor`

## Sync Workflow

### Step 1: Sync from Toss

Preview changes first:
```bash
./bin/stock-cli portfolio sync --dry-run
```

Apply sync:
```bash
./bin/stock-cli portfolio sync
```

This will:
- Fetch current positions from Toss (Open API by default, tossctl fallback)
- Auto-create KR and US portfolios if they don't exist
- Compare Toss holdings with local portfolio.db
- Record synthetic BUY/SELL transactions for any differences
- Subsequent syncs are idempotent (no changes if already in sync)
- The output JSON includes `"source": "toss-api"` or `"tossctl"` so you can
  confirm which path ran

Force a specific source if needed:
```bash
./bin/stock-cli portfolio sync --source toss-api --dry-run
./bin/stock-cli portfolio sync --source tossctl --dry-run
```

### Step 2: Evaluate (optional)

After sync, evaluate the portfolio:
```bash
./bin/stock-cli portfolio report --market KR
./bin/stock-cli portfolio report --market US
./bin/stock-cli portfolio risk --market US
```

### Sync one market only

```bash
./bin/stock-cli portfolio sync --market KR
./bin/stock-cli portfolio sync --market US
```

## Auth troubleshooting

- **Open API:** the OAuth token is refreshed automatically, so there is no
  session to expire. An auth error means `TOSS_CLIENT_ID`/`TOSS_CLIENT_SECRET`
  are missing or invalid — verify them in `.env`.
- **tossctl fallback:** sessions expire after inactivity. Re-login with
  `tossctl auth login`, then retry the sync.

## Common User Triggers

When the user says any of these, run the sync:
- "토스에서 가져와줘"
- "토스 동기화해줘"
- "토스 조회해줘"
- "Toss에서 내 포트폴리오 가져와"
- "sync from toss"
- "update from brokerage"

Always run `--dry-run` first and show the user what will change before applying.
