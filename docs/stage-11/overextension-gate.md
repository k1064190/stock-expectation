# Stage 11 · E1 — Per-stock overextension gate (RULE R2)

## Why
The D-stage validation proved the broad-index regime gate could not catch the
6/2 cluster: those losers (ORCL, NET, SMCI, DDOG, KR semis) were **parabolic
individual names** that reversed while the index was calm. A per-stock
overextension gate is the missing lever.

## What
- New `overextension_level` (NONE / ELEVATED / EXTREME) on `HorizonMetrics`,
  computed by `classify_overextension(rsi14, price, ma20)` from RSI14 and price
  distance above MA20. Surfaced automatically in `horizon-metrics[-batch]`.
- RULE R2 in `/expect` + `daily-briefing`: EXTREME → suppress new BULL (cap
  WATCH); ELEVATED → raise BUY bar +1.0 and trim confidence (cap 0.60).
- R1+R2 stacking made explicit: WATCH cap from either wins; BUY-bar raises are
  additive (NEUTRAL regime + ELEVATED stock → COMPOSITE ≥ 10.0).
- Tests added to `mcp-market-data/tests/test_indicators.py`; CLI contract test
  pins the new field.

## How
- Thresholds calibrated on 377 closed BULL by reconstructing each prediction's
  entry-time metrics (truncating each ticker's history to its `created_at`):
  EXTREME = RSI14 > 75 or price > 15% above MA20; ELEVATED = RSI14 > 70 or
  > 8% above MA20. Pure, network-free classifier.

## Validation (entry-time reconstruction, 377 closed BULL)
| overextension_level at entry | n | win rate |
|---|---|---|
| NONE | 99 | **53.5%** |
| ELEVATED | 79 | 41.8% |
| EXTREME | 199 | **29.1%** |

Suppressing EXTREME lifts kept BULL win-rate to **48.3% (n=178) vs 38.2%
overall — +10pp**, with a clean monotonic gradient. By RSI alone: RSI>75 won
26.4% (n=125) vs 47.5% at RSI<60. By MA20 distance: >15% won 29.6% (n=196) vs
66% in the 3–8% band.

## Review loop (code-reviewer + codex + gemini)
- Code verified correct by all three (severity ordering, ma20≤0/None guards,
  RSI-None default, no double-counting with the point table, defaulted dataclass
  field is backward-compatible).
- **Fixed (codex Medium) — R1+R2 stacking ambiguity:** documented bar raises as
  additive and WATCH-cap as "either wins".
- **Fixed (both Low) — stale schema list / unpinned field:** added
  `overextension_level` to the `/expect` field list and to the CLI contract
  test's `expected_fields`.
- **Fixed — boundary tests:** added strict-RSI-boundary and MA20-band tests
  (avoided float-fragile exact-percent edges after one such test flaked).
- **Noted (both) — ELEVATED is intentionally mild:** RSI 70-75 still earns +0.5
  momentum and near-high cycle +1.0, partly offsetting R2's +1.0 bar. Justified:
  ELEVATED won 41.8% (near break-even), so a soft gate is appropriate; EXTREME
  (29%) gets the hard suppression.

## Code locations
- `mcp-market-data/indicators.py` — `classify_overextension`, `OVEREXT_*`
  constants, `HorizonMetrics.overextension_level`, wiring in
  `compute_horizon_metrics`.
- `.claude/skills/expect/SKILL.md` (RULE R2 + field list),
  `.claude/skills/daily-briefing/SKILL.md` (RULE R2).
- `mcp-market-data/tests/test_indicators.py`, `tests/test_cli_integration.py`.

## Retrospective
- This is the complement the D validation predicted: R1 (market) + R2 (stock)
  together address both regime-blind clusters and parabolic single names.
- Reconstructing entry-time metrics from stored `created_at` turned a hunch
  ("the losers were extended") into a calibrated, monotonic threshold.
