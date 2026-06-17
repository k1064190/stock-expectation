---
name: sector-rotation-rs-screener
description: Prescriptive sector-rotation screener that ranks sectors by relative strength, constituent breadth, and lifecycle stage, emitting a machine-readable FAVOR / ROTATING_IN / ROTATING_OUT / AVOID / NEUTRAL verdict per sector for both US and KR markets. Unlike the narrative sector-analyst skill, this produces a deterministic JSON snapshot (data/sector_rs_{market}.json) that the daily-briefing candidate discovery consumes to bias stock ranking toward sectors rotating in and away from sectors rotating out. Use when the user wants an actionable "which sectors to favor/avoid right now" answer, a sector leaderboard, sector relative-strength ranking, 섹터 로테이션, 섹터 상대강도, or which sectors are leading/lagging.
---

# Sector Rotation RS Screener

## Overview

This skill turns each sector's price structure — measured **relative to the
market benchmark** — plus its constituent breadth into a prescriptive rotation
verdict and a 0–100 score. It is the deterministic, machine-readable
counterpart to `sector-analyst` (which writes a narrative cycle-phase report).
Use this one when you want an actionable leaderboard and a JSON artifact that
downstream automation (the daily briefing's candidate discovery) can read.

All scoring lives in `mcp-market-data/sector_rs.py` (pure, never-raise). The
CLI command `bin/stock-cli sector-rs` fetches the data, builds the verdicts, and
optionally persists the snapshot.

## When to Use This Skill

- "Which sectors should I favor / avoid right now?"
- "Rank the sectors by relative strength."
- "Is tech rotating in or out?"
- "섹터 로테이션 점검해줘", "지금 어떤 섹터가 강해?"
- Before logging individual-stock predictions, to gate by the parent sector.

For a qualitative market-cycle narrative (Early/Mid/Late cycle, scenario
probabilities), use `sector-analyst` instead — the two are complementary.

## The Three Axes

1. **Relative strength (RS)**
   - `rs_1m = sector_etf.return_1m − benchmark.return_1m`
   - `rs_3m` = a 63-bar (~3-month) close-return spread vs the benchmark.
2. **Breadth** — fraction of the sector's constituent basket whose latest close
   is above its MA20 **and** whose 1-month return beats the benchmark's.
3. **Stage** (lifecycle) — from RSI14, distance to MA50, and overextension:
   - **EARLY** = RSI14 ∈ [45, 65] AND price within ±8% of MA50 AND
     overextension NONE AND `rs_1m > 0` (constructive, not yet extended).
   - **LATE** = overextension ELEVATED/EXTREME OR price >15% above MA50 OR
     RSI14 > 70 (stretched / blow-off).
   - **MID** = otherwise.

## Verdict Map (prescriptive)

| Verdict        | Condition                                                  | Action |
|----------------|------------------------------------------------------------|--------|
| **FAVOR**      | `rs_1m > 0` AND breadth ≥ 0.5 AND stage ∈ (EARLY, MID)     | Overweight; prefer this sector's leaders. |
| **ROTATING_IN**| `rs_1m > 0` AND `rs_3m ≤ 0` AND stage == EARLY             | Fresh leadership turning up — accumulate early. |
| **ROTATING_OUT**| (`rs_1m < 0` AND `rs_3m > 0`) OR (stage == LATE with weak breadth) | Trim; momentum rolling over. |
| **AVOID**      | `rs_1m < 0` AND breadth < 0.4                              | Underweight; lagging with poor participation. |
| **NEUTRAL**    | otherwise, or when the benchmark is missing                | No edge; defer to bottom-up. |

`score = 0.5*RS + 0.3*breadth + 0.2*stage`, normalized to 0–100. A missing
benchmark floors **every** sector to NEUTRAL (score 50) — the screener never
certifies rotation against an unknown market.

## Workflow

### Step 1 — Generate the snapshot

```bash
# US (S&P 500 sector ETFs vs SPY)
bin/stock-cli sector-rs --market US

# KR (KODEX sector proxies vs 069500 / KODEX 200)
bin/stock-cli sector-rs --market KR

# Persist for discovery to consume (atomic, per-market file)
bin/stock-cli sector-rs --market US --write   # → data/sector_rs_us.json
bin/stock-cli sector-rs --market KR --write   # → data/sector_rs_kr.json
```

The output is JSON: a `sectors` list ranked by `score` descending, each with
`verdict`, `stage`, `rs_1m`, `rs_3m`, `breadth_pct`, `score`, the proxy `etf`,
and the `constituents` basket. `--write` is the only side effect; without it the
command is read-only.

- **US** sector ETFs and leader baskets are static inside `sector_rs.py`
  (`US_SECTOR_ETFS` / `US_SECTOR_CONSTITUENTS`), benchmark `SPY`.
- **KR** sectors come from `data/kr_sector_map.csv`
  (`sector,proxy_etf,proxy_etf_name,constituents`), benchmark `069500`.

### Step 2 — Interpret the leaderboard

Read top-to-bottom: FAVOR/ROTATING_IN sectors at the top are where new long
ideas should concentrate; ROTATING_OUT/AVOID at the bottom are where to trim or
stay out. Cross-check `rs_1m` vs `rs_3m`: a positive `rs_1m` with a negative
`rs_3m` is the classic *early rotation* signature (ROTATING_IN).

### Step 3 — Apply per stock

When evaluating an individual ticker, look up its sector's verdict/stage and let
it gate the call: favor leaders in FAVOR/ROTATING_IN sectors; demote or skip
names in ROTATING_OUT/AVOID sectors.

## How discovery consumes the snapshot

`scheduler/candidate_discovery.py` reads the persisted file via
`_load_sector_verdicts(market)` (never-raise: a missing or stale file → `{}`),
which maps every constituent **and** proxy ETF ticker to its sector verdict.
`apply_sector_boost(candidates, verdicts)` then:

1. stamps each candidate's `sector_verdict` / `sector_stage` in place, and
2. returns a bounded `{ticker: multiplier}` map the ranking sort folds in:

| Sector state            | Multiplier |
|-------------------------|-----------|
| FAVOR + EARLY           | ×1.30 |
| ROTATING_IN (or FAVOR/MID) | ×1.15 |
| NEUTRAL / unknown       | ×1.00 |
| ROTATING_OUT (non-LATE) | ×0.80 |
| AVOID / ROTATING_OUT+LATE | ×0.60 |

When no snapshot exists, every multiplier is exactly 1.0 and candidate fields
are left untouched — discovery ranking is byte-identical to the pre-sector
behaviour (this no-op is covered by `scheduler/tests/test_sector_boost.py`).

## Prerequisites

- `bin/stock-cli` available in the project root.
- Network access for the price batch (yfinance for US, PyKRX for KR).
- No API key required.

## Notes

- Pure scoring lives in `mcp-market-data/sector_rs.py`; the CLI command is
  `cmd_sector_rs` in `stock_cli.py`.
- The snapshot files `data/sector_rs_us.json` / `data/sector_rs_kr.json` are
  gitignored — they are regenerated every run.
- Sector-cohort backtesting reuses `scheduler/asof_backtest.py`; this skill does
  not ship its own backtest harness.
