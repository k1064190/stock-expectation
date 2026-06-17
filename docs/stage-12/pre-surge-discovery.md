# Stage 12 — Pre-surge candidate discovery (WT-A.1)

## Why

The daily-briefing cron only ever recommends stocks that **already surged**. Its sole
candidate funnel (`scheduler/candidate_discovery.py`) keeps a ticker only if
`|5d return| ≥ 15%` OR `vol_ratio ≥ 2.0`, sorts by biggest move first, and the prompt
hard-restricts the LLM to that list. A backtest of the closed-buy history (178 ticker-days)
showed the hit rate falls monotonically with how much a pick had already run: `10–20%`
trailing-month entries hit **50%**, but `20–40%` hit 27% and `>40%` hit 24%; surged-`>15%`
picks hit 26% vs 37% for not-surged. The system is structurally buying high. This stage adds
the orthogonal "not yet extended" candidate stream the funnel was missing — the foundation of
the blended funnel (WT-A.2) and the as-of validation harness (WT-A.3).

## What

- A pure, replayable scorer that matches a ticker against **4 pre-surge setups**: `base_pivot`
  (volatility-contraction coil near MA20/50, volume dry-up→pickup, not extended), `pullback`
  (dip to MA20/MA50 inside an MA20>MA50>MA200 stack), `rs_leader` (beats its index but still in
  the 5–20% band), and `pre_earnings` (US-only: earnings in 5–10 days + compression + drift).
- A `discover_presurge_candidates(market, …)` pipeline reusing the legacy universe enumeration,
  batch fetch, and name-fill — never-raises (collapses to `[]` on provider failure).
- The **canonical `Candidate` schema** for the whole A-chain: added 5 defaulted fields
  (`discovery_source`, `setup_type`, `watch_only`, `sector_verdict`, `sector_stage`) so WT-A.2
  and WT-C consume them without redefining the dataclass.
- A `stock-cli screen-presurge --market US|KR [--top-n --min-score --days --with-earnings]`
  command exposing the engine as JSON.

## How

- Reused `mcp-market-data/indicators.py` `compute_horizon_metrics` (MA/RSI/returns/vol_ratio/
  `overextension_level`) for every detector; added two pure helpers `compute_return_stdev` and
  `contraction_ratio` (non-overlapping recent-vs-prior return-stdev windows).
- The scorer `score_presurge_setups(metrics, closes, volumes, earnings_in_days, benchmark_…)`
  is pure (no I/O, no `datetime.now`) so WT-A.3 can replay it on historical bar slices.
  `best_setup` picks max score, breaking ties by priority pullback>base_pivot>rs_leader>pre_earnings.
- `--with-earnings` does a best-effort FMP earnings fetch that degrades to `{}` (no key / error),
  keeping the US pre-earnings setup optional and the pipeline robust.

## Code locations

- `scheduler/pre_surge_discovery.py` — engine (scorer, detectors, pipeline, earnings helper).
- `mcp-market-data/indicators.py:247-308` — `compute_return_stdev`, `contraction_ratio`.
- `scheduler/candidate_discovery.py:48-95` — extended `Candidate` dataclass.
- `stock_cli.py` — `cmd_screen_presurge` + `screen-presurge` subparser.
- `scheduler/tests/test_pre_surge_discovery.py` — 24 offline tests (helpers, each detector
  boundary, tie-break, pipeline tag/filter/sort + never-raise).

## Verification

- `uv run pytest scheduler/tests/test_pre_surge_discovery.py scheduler/tests/test_candidate_discovery.py -q`
  → 51 passed (the candidate-discovery suite proves the dataclass extension is backward-compatible).
- Live: `bin/stock-cli screen-presurge --market US --top-n 10` surfaced GOOG/KO/NVDA (pullback),
  C (rs_leader) — modest 5d moves, not parabolic chases. KR returned defensive/value pullbacks
  (SK텔레콤, KT&G, …). Both markets exit 0.

## Review loop

- **code-reviewer-pro**: clean — math (window slicing, ddof, non-overlapping windows), never-raise,
  backward-compatible Candidate extension, look-ahead-safe pure scorer all verified; 0 findings.
- **gemini -m pro**: NONE.
- **codex -m gpt-5.5 (1 P1, actioned)**: the pullback detector didn't apply the shared 20% trailing
  parabolic cap, so an already-run name pulling back near its MAs (not EXTREME) could be emitted as
  pre-surge — added a `return_1m >= PARABOLIC_RETURN_1M` guard to `_detect_pullback`.
- Outcome: 1 codex P1 fixed; reviews otherwise clean.

## Retrospective

- What went well: reusing the existing HorizonMetrics + universe enumeration kept the new module
  small and made the detectors trivially testable via constructed metrics; the pure scorer cleanly
  separates detector logic (unit-tested directly) from the I/O pipeline (tested with a FakeProvider).
- Carry forward: detector thresholds (contraction ≤0.75, RSI bands, 5–20% RS band) are heuristics
  from the investigation — they are **unvalidated** until WT-A.3's as-of backtest runs; that harness
  is the ship gate before any of this reaches cron. KR has no earnings feed, so `pre_earnings` is
  US-only (asymmetric lift expected).
