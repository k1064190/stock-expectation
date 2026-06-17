# Stage 14 — Sector-Rotation RS Screener

## Why

`/expect` and the daily briefing rank individual tickers by raw momentum/volume
with no awareness of the parent sector's rotation state. A name can post a big
5-day move while its sector is topping out (LATE, breadth thinning) — exactly
the cohort most likely to mean-revert. The existing `sector-analyst` skill is
narrative-only (cycle phase + scenario prose); nothing emits a machine-readable
per-sector verdict that automation can consume. This stage adds a deterministic,
prescriptive sector-rotation screener and wires its output into candidate
discovery as a bounded ranking boost.

## What

- **`mcp-market-data/sector_rs.py`** — pure, never-raise scoring module.
  `SectorVerdict` dataclass + a three-axis score (RS 1m/3m, constituent breadth,
  lifecycle stage) → verdict (FAVOR / ROTATING_IN / ROTATING_OUT / AVOID /
  NEUTRAL). Missing benchmark or missing ETF → NEUTRAL floor (score 50). US
  sector ETFs + leader baskets are static in-module; KR via CSV.
- **`data/kr_sector_map.csv`** — `sector,proxy_etf,proxy_etf_name,constituents`
  for 10 KR sectors (semicolon-joined 6-digit codes).
- **`stock_cli.py`** — `cmd_sector_rs` (mirrors `cmd_regime`): one batch fetch
  of benchmark + all ETFs + all constituents, builds verdicts via
  `rank_sectors`, prints JSON. `--write` atomically persists the **per-market**
  `data/sector_rs_{us,kr}.json` (os.replace via temp file). Registered as
  `sector-rs --market {US,KR,us,kr} [--days 400] [--write]`.
- **`scheduler/candidate_discovery.py`** — `_load_sector_verdicts(market)`
  (reads the per-market JSON, never-raise → `{}`) + `apply_sector_boost` (stamps
  `sector_verdict`/`sector_stage` in place, returns a bounded
  `{ticker: multiplier}` map; empty verdicts → strict 1.0 no-op).
- **Tests** — `tests/test_sector_rs.py` (17) + `scheduler/tests/test_sector_boost.py` (9).
- **Skill** — `.claude/skills/sector-rotation-rs-screener/SKILL.md` (Korean-first
  description, full workflow) + a one-line cross-ref atop `sector-analyst`.
- **`.gitignore`** — ignores the two regenerated snapshot JSONs.

## How

- Reused `indicators.compute_horizon_metrics` / `HorizonMetrics` for all per-
  ticker metrics, and `HorizonMetrics.overextension_level` for the LATE stage
  test (instead of recomputing) so the stage read matches the rest of the
  pipeline. Reused the `regime.py` NEUTRAL-floor pattern for the never-certify-
  against-unknown-benchmark contract.
- Score blend: RS clamped to ±10% then mapped to [0,1]; breadth already [0,1];
  stage EARLY/MID/LATE → 1.0/0.6/0.2; weights 0.5/0.3/0.2 → ×100.
- The boost is multiplicative on the existing sort key (e.g.
  `abs(return_5d_pct) * multiplier`) and bounded to [0.6, 1.3], so it nudges
  rather than dominates momentum ranking. The empty-map path returns all-1.0 and
  mutates nothing — tested as byte-identical to the baseline ordering.
- `cmd_sector_rs` fetches every ticker in a single `get_price_history_batch`
  call to keep the run cheap; each ticker degrades to `(None, None)` on a
  provider miss so one delisted constituent never aborts the sector.
- TDD: wrote `test_sector_rs.py` first; the `test_stage_late_on_overextension`
  failure surfaced that I was recomputing overextension from MA20 rather than
  reading the precomputed field — fixed by reading `metrics.overextension_level`.

## Code locations

- `mcp-market-data/sector_rs.py` — `SectorVerdict`, `classify_stage`,
  `compute_breadth`, `compute_sector_verdict`, `rank_sectors`, `_classify_verdict`,
  `_normalize_score`, US static baskets.
- `stock_cli.py` — `cmd_sector_rs`, `_sector_specs`, `_load_kr_sector_map`,
  `_metrics_and_closes`, `_write_sector_rs_json` (after `cmd_regime`); subparser
  registration after the `regime` parser; imports near the `regime` import.
- `scheduler/candidate_discovery.py` — `_load_sector_verdicts`,
  `apply_sector_boost`, `_sector_multiplier` (end of file).
- `data/kr_sector_map.csv`.
- Tests: `tests/test_sector_rs.py`, `scheduler/tests/test_sector_boost.py`.
- Skill: `.claude/skills/sector-rotation-rs-screener/SKILL.md`; cross-ref in
  `.claude/skills/sector-analyst/SKILL.md`.

## INTEGRATION TODO (A.2)

The daily-briefing wiring is owned by a separate in-flight worktree (WT-A.2) and
was intentionally **not** touched here (`scheduler/daily_briefing.py`,
`scheduler/prompts/briefing_*.md`, `.claude/skills/daily-briefing/SKILL.md` are
all untouched). When A.2 integrates this:

1. **Before discovery**, refresh the snapshot for the market being briefed:
   run `stock-cli sector-rs --market <MKT> --write` (or call `cmd_sector_rs`
   in-process) so `data/sector_rs_<mkt>.json` is current for that briefing.
2. **In the discovery step** (where `discover_kr_candidates` /
   `discover_us_candidates` produce the candidate list), call:
   ```python
   verdicts = _load_sector_verdicts(market)
   multipliers = apply_sector_boost(candidates, verdicts)
   ```
   and fold `multipliers[c.ticker]` into the existing ranking sort key
   (currently `abs(c.return_5d_pct)`), e.g.
   `key=lambda c: abs(c.return_5d_pct) * multipliers.get(c.ticker, 1.0)`.
3. Optionally surface `candidate.sector_verdict` / `sector_stage` in the
   prompt's candidate block so the LLM sees the rotation context.

Because `apply_sector_boost({})` is a strict no-op, A.2 can land the call sites
first and the snapshot-generation step second without changing ranking until the
file exists.

## Backtest note

No standalone backtest harness was built. Sector-cohort validation will reuse
`scheduler/asof_backtest.py` after merge (per the stage constraint).

## Retrospective

- Reusing `HorizonMetrics` end-to-end (including the overextension field) kept
  the module small and consistent with `regime.py`; the TDD loop caught the one
  place I drifted (recomputing overextension). Carry forward: when a dataclass
  already carries a derived field, read it rather than recompute.
- The strict-no-op contract for `apply_sector_boost({})` is what lets WT-A.2
  land incrementally — worth designing for explicitly whenever two worktrees
  share an integration seam.
