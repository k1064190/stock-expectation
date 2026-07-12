# Stage 5 — Per-stock claude -p deep-dive fan-out

## Why

The daily briefing analyzes 5-6 candidates in one LLM call, so every stock
gets shallow, shared context; /expect's LLM_CONTEXT effort collapses to 0 for
most names. An independent per-stock deep dive (Bull/Bear/Judge with the full
headline set and its own `bin/stock-cli` access) raises context rigor exactly
where the one-shot prompt is thinnest.

## What

- `scheduler/deep_dive.py`: single-ticker dive prompt (strict-JSON contract:
  `ticker / context_score(-5..+3) / conviction / risks / catalysts / summary`;
  prediction logging explicitly forbidden), bounded fan-out
  (ThreadPoolExecutor, parallelism 2, 420s/dive, cap 6), hardened parsing
  (last ```json block, ticker match required, score clamp, conviction
  whitelist, list sanitization), per-ticker fail-open.
- `daily_briefing.py`: `--deep-dive` (default **off**) + `--deep-dive-cap`;
  results injected into the briefing prompt with the instruction to adopt each
  dive's `context_score` as that stock's LLM_CONTEXT pillar and store the dive
  verbatim in `components.deep_dive`. API mode ignores the flag.
- Dives never log predictions — the single logging path keeps R1/R2/R3 and the
  store gates authoritative.

## How

TDD: 11 module tests + 2 wiring tests first. The subprocess wrapper mirrors
`call_claude_code` but is self-contained in `deep_dive.py` (no circular
import). Worst-case wall clock at defaults ≈ 6 dives × ≤7 min / 2 workers ≈ 21
min/market — hence default-off until a week of manual runs looks sane.

## Code locations

- `scheduler/deep_dive.py` — full module
- `scheduler/daily_briefing.py` — `_deep_dive_block`, `build_claude_code_prompt`
  params, `run_briefing` + argparse plumbing
- `scheduler/tests/test_deep_dive.py`, `scheduler/tests/test_daily_briefing.py`

## Verification

- `uv run pytest -m "not network"` — 937 passed (13 new)
- Live E2E: one real `claude -p` dive for NVDA succeeded — the dive fetched its
  own `stock-cli` data and returned `context_score +0.5 (MEDIUM)` with five
  specific risks (defensive 1M sector rotation, +8.3% weekly bounce on 0.82
  vol-ratio, ASIC substitution, crowding, 8/27 earnings bar) and four cited
  catalysts — markedly richer than the shallow one-shot path
- `--help` smoke shows both flags

## Review loop

- **Antigravity (Gemini 3.1 Pro High)**: 1 HIGH — headlines interpolated raw
  into the dive prompt (injection surface); fixed with `<headlines>` fencing +
  untrusted-data instruction and a regression test. 1 MEDIUM — bare-JSON with
  conversational preamble failed the fallback; fixed with outermost-brace
  span. 3 LOW — null summary → "None" string (fixed), ARG_MAX and
  orphaned-child concerns (documented as comments; same pattern as the
  existing `call_claude_code`).
- **code-reviewer subagent**: verified all fixes; 1 warning (multi-block parse
  untested — 3 tests added) + 2 suggestions (brace-in-string test added,
  debug log at parse-accept added). "Merge."
- **Codex**: CLI + GitHub bot quota-exhausted — deferred to the PR bot round.

## Retrospective

The dive's own data access is what makes it valuable: the live NVDA run pulled
sector-RS, volume, and the earnings calendar unprompted. Keep it default-off
until a week of manual runs establishes the cost/quality trade.
