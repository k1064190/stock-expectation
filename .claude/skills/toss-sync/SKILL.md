---
name: toss-sync
description: Sync portfolio from Toss Securities. Use when user mentions "토스", "Toss", "토스에서", "토스 동기화", "sync from toss", "toss 조회", or wants to update their portfolio from their brokerage account.
---

# Toss Securities Portfolio Sync

Sync holdings from Toss Securities into the local portfolio tracker via `tossctl`.

## Prerequisites

- `tossctl` must be installed: `curl -fsSL https://raw.githubusercontent.com/JungHoonGhae/tossinvest-cli/main/install.sh | sudo sh`
- User must have logged in: `tossctl auth login` (requires Chrome + QR code scan from Toss app)

Check status with:
```bash
tossctl doctor
```

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
- Fetch current positions from Toss via `tossctl portfolio positions`
- Auto-create KR and US portfolios if they don't exist
- Compare Toss holdings with local portfolio.db
- Record synthetic BUY/SELL transactions for any differences
- Subsequent syncs are idempotent (no changes if already in sync)

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

## Session Management

Toss sessions expire after ~1 hour of inactivity. If sync fails with an auth error:
1. Tell the user to re-login: `tossctl auth login`
2. Retry the sync after login completes

## Common User Triggers

When the user says any of these, run the sync:
- "토스에서 가져와줘"
- "토스 동기화해줘"
- "토스 조회해줘"
- "Toss에서 내 포트폴리오 가져와"
- "sync from toss"
- "update from brokerage"

Always run `--dry-run` first and show the user what will change before applying.
