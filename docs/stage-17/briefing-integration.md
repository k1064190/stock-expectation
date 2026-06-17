# Stage 17 — Briefing integration: sector boost (WT-C) + event gate R3 (WT-D)

## Why

WT-C (sector-rotation RS) and WT-D (catalyst-event-gate) shipped their cores standalone, deferring
the `daily_briefing.py` wiring to avoid colliding with WT-A.2's prompt rewrite. With the A-chain
merged, this stage threads both into the cron + API briefing paths so the LLM actually receives the
sector rotation context and the R3 event-risk gate.

## What

- **Sector boost (WT-C)**: before discovery, the briefing refreshes `data/sector_rs_<market>.json`
  and passes the loaded verdicts to `assemble_blended_candidates(sector_verdicts=...)`. The blend
  stamps `sector_verdict`/`sector_stage` on each candidate, reorders by the bounded sector multiplier
  (FAVOR/EARLY ×1.3 … AVOID/LATE ×0.6), and surfaces `sector=` in the candidate block. Strict no-op
  when the snapshot is absent.
- **Event gate R3 (WT-D)**: the briefing calls `evaluate_gate(asof, candidate_tickers, market)` once
  per market (fail-open) and injects a `## Event Risk` block; new **RULE R3** lines in both cron
  prompts, both API prompt templates, and the daily-briefing SKILL tell the LLM to WATCH-cap earnings
  ≤2 trading days and trim on earnings/macro (stacking under R1/R2). KR is macro-only.

## How

- `_sector_verdicts_for(market)` shells out to `stock-cli sector-rs --write` via `sys.executable`
  (no uv re-entry, no import cycle), then `_load_sector_verdicts` — both fail-open to `{}`.
- `_event_gate_block(market, tickers)` + `_format_event_gate_for_prompt(gate)` render the R3 block;
  `evaluate_gate` is itself fail-open (missing FMP key / fetch error → `gate_unavailable`, rendered as
  a "treat as zero" note). Wired into all four call sites: `fetch_us/kr_market_data` (API) and both
  branches of `build_claude_code_prompt` (cron).

## Code locations

- `scheduler/daily_briefing.py` — `_sector_verdicts_for`, `_event_gate_block`,
  `_format_event_gate_for_prompt`; 4 call sites wired; R3 rule added to both cron branches.
- `scheduler/prompts/briefing_{us,kr}.md` — R3 rule. `.claude/skills/daily-briefing/SKILL.md` §4.5 —
  sector-boost + R3 documentation.
- `scheduler/tests/test_daily_briefing.py` — 3 event-gate formatter tests.

## Verification

- `uv run pytest -m "not network"` → 588 passed.
- E2E: `build_claude_code_prompt('US')` renders the `## Event Risk` block, the `GATE R3` rule, the
  pre-surge stream, and `sector=` tags (the sector snapshot is generated + consumed in-process).
- All paths fail-open: no FMP key → R3 "unavailable"; no sector file → boost no-op.

## Review loop

- (to be recorded after the stage review)

## Retrospective

- The two cores were designed with compatible seams (`_load_sector_verdicts` returns exactly the
  `{ticker: {...}}` shape `blend_streams` consumes; `evaluate_gate` is fail-open), so the wiring was
  additive with no contract surgery — the deferred-integration bet paid off.
- The sector snapshot refresh is a subprocess for isolation; if briefing latency matters, a future
  pass could compute it in-process via a shared `sector_rs` helper.
