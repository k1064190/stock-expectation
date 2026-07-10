# ISA ETF Long-Term Investing — Design Spec

Date: 2026-07-10 · Approved by Doctor Cho (conversation, 2026-07-10)

## Goal

Give the ISA (개인종합자산관리계좌) long-term ETF practice the same grade of
analysis the stock system gives short-term picks: a KR-listed ETF data layer,
deterministic long-horizon decision logic, a monthly contribution (적립)
advisor, and a logged track record — reusing the existing CLI/skill/scheduler
architecture.

User's starting state: ISA account opened, no holdings yet, monthly
contributions planned (amount passed per run, never hardcoded).

## Scope

**In:** KR-listed ETFs only (ISA constraint): domestic index, KR-listed
overseas index, bond, gold, REIT ETFs. Leveraged/inverse excluded from the
universe by default. Tax-type tagging (국내주식형 vs 기타형) as displayed
information.

**Out:** overseas-listed ETFs, tax computation (한도/손익통산 are displayed as
info only), automatic order execution, the predictions.db pipeline (BULL/BEAR
labels, R1–R3 gates, 1Y gate) — long-term decisions never flow through it.

## Architecture

Same pattern as the stock system: CLI provides data, the skill judges, code
enforces caps, the scheduler runs the cadence, the DB records decisions.

| Piece | Location | Role |
|---|---|---|
| ETF data layer | `mcp-market-data/etf_kr.py` | Universe + metadata: 총보수, AUM, 괴리율, 추적오차, 분배금, asset class, tax type, hedge flag. pykrx ETF API primary, KRX 정보데이터시스템 fallback. Fail-open with visible notes. Universe classification cached at `data/etf_universe_kr.csv`. |
| CLI | `stock_cli.py` | `etf list / info / compare`, `isa init / status / allocate / rebalance / log`. JSON output like all commands. |
| Portfolio | existing `portfolio/` | No schema change to existing tables — the ISA book is a normal KR portfolio named "ISA". Two new tables in `portfolio.db`: `isa_targets` (approved target allocation, versioned), `isa_decisions` (monthly decision log). |
| Allocation engine | `portfolio/isa_allocator.py` | Deterministic core (see below). |
| Skill | `.claude/skills/isa-briefing/` | Monthly Korean briefing; quarterly runs add a rebalance-check section. Reuses `macro-news`. |
| Scheduler | `scheduler/isa_briefing.py` + crontab entry | Monthly on contribution day; quarterly check. Telegram delivery reused. |

## Decision framework

1. **ETF scoring** (stage 27): cost (총보수 + 추적오차), liquidity (AUM,
   거래대금, 괴리율), index fit. Among ETFs tracking the same index, the best
   ticker is chosen automatically; `etf compare` exposes the comparison.
2. **Initial target allocation** (stage 28): risk/horizon questions → proposal
   anchored on verified model portfolios (60/40, all-weather variants) mapped
   to KR-listed ETFs → Doctor Cho approves/edits → stored in `isa_targets`.
   Every later change is also propose → approve.
3. **Monthly contribution** (stage 28): `isa allocate --amount N`. Deterministic
   core: drift-correcting DCA — contributions fill underweight asset classes
   first. The LLM may *propose* valuation/momentum tilts; code clamps any tilt
   to **±10 percentage points** per asset class (gate philosophy carried over
   from the stock system). Every run logs inputs, proposal, clamped result to
   `isa_decisions`.
4. **Rebalancing** (stage 28): band-based, **±5 percentage points** per asset
   class, checked quarterly. Sell-minimizing by design (ISA 비과세 한도 /
   의무기간); tax-type info displayed alongside.
5. **Track record** (stage 29): monthly NAV snapshot vs the target-allocation
   benchmark plus S&P 500/KOSPI reference; tilt decisions get annual
   after-the-fact evaluation.

## Error handling

All data sources fail open with visible notes (never silent zeros — lesson
from the R3 gate incident). The skill trusts only CLI JSON. Missing metadata
for a ticker downgrades its score with an explicit "data unavailable" flag
rather than dropping it silently.

## Testing

`uv run pytest -m "not network"` green at every stage. Deterministic allocator
tests (positions + targets + amount → exact split; tilt clamp; band trigger),
mocked metadata-fetch tests, in-process CLI tests, briefing block render tests.

## Implementation stages

Stage numbering: 25 is taken by the in-flight gold-weekly-trend branch.

- **Stage 26** — ETF data layer + `etf list/info` CLI
- **Stage 27** — scoring + `etf compare` (same-index best-ticker)
- **Stage 28** — `isa init/status/allocate/rebalance` + targets/decisions tables + allocator
- **Stage 29** — `/isa-briefing` skill + scheduler + track record

Each stage: own branch/PR, stage doc under `docs/stage-N/`, `docs/summary.md`
index entry, stage-docs visual, review loop (internal + second opinion; Codex
bot when quota allows), full non-network test pass.
