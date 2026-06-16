# Stage 11 · L1 — LLM_CONTEXT debate rigor gate

## Why
LLM_CONTEXT (the macro/sector/narrative channel) graded weak (~19% win) and is
the hardest pillar to harden because it is LLM-judged. We can't make the
*judgment* better in code, but we can refuse to trust a **sloppy** one: a strong
score with no cited evidence, a one-sided debate, or an out-of-range value.

## What
- `mcp-market-data/llm_context.py`: `validate_llm_context(debate)` returns rigor
  violations; `clamp_score`; `score_from_debate`.
- `stock-cli lint-llm-context '<json>'` (exit 1 on violations) so `/expect` can
  self-gate before trusting the score.
- Step 5b now structures the bull/bear debate as JSON and lints it; a non-clean
  lint means **dampen the score toward 0**.
- 14 tests in `tests/test_llm_context.py`.

## How — what the gate enforces
- `score` ∈ [-5, +3], finite (NaN/Infinity rejected).
- `winner` agrees with the score's sign (0 ⇒ neutral).
- A **genuine** bear case: ≥1 `bear_point` with a real (non-blank string) claim.
- When **|score| ≥ 2.0**, the winning side has ≥1 point with non-blank **string**
  evidence and a `signal_type` ∈ {macro, sector, event, flow, valuation,
  technical, narrative}.

## Honest scope (it's a gate, not a judge)
This enforces debate *shape* and *discipline*, not truth. An LLM could still emit
shallow-but-well-formed evidence. That's the right division of labor: the linter
blocks structural sloppiness deterministically; the human/second-pass review
judges content. Crucially, llm_context's score is now persisted via H1's
`--components`, so `component-contribution` will measure whether this pillar
actually adds alpha forward — turning "is the LLM channel noise?" into a
measured question instead of a guess.

## Review loop (code-reviewer + codex + gemini)
Both reviewers called the first cut "ceremony, not rigor" and found concrete
bypasses — all fixed:
- **Fake bear case** (`bear_points: [{}]` / `[""]`) passed → now requires a
  bear point with a real string claim.
- **Non-string evidence** (`evidence: {}`) stringified to truthy and counted as a
  citation → now only non-blank string evidence qualifies.
- **NaN score** clamped to a max-bullish 3.0 → `clamp_score` returns None for
  non-finite and `validate` flags it.
- **score==0 winning-side** latent bug → explicit `None` (no evidence rule at 0).
- Added tests for every closed bypass + score==0 boundary.

## Code locations
- `mcp-market-data/llm_context.py` — `validate_llm_context`, `clamp_score`,
  `score_from_debate`, `_point_is_cited`, `_point_has_claim`.
- `stock_cli.py` — `cmd_lint_llm_context` + parser.
- `.claude/skills/expect/SKILL.md` — Step 5b rigor gate.
- `tests/test_llm_context.py`.

## Retrospective
- The reviewers were right that v1 was satisfiable trivially; tightening the
  bear-claim and string-evidence checks turned it into a gate that actually bites.
- The real win for the LLM pillar is measurement (H1) + this discipline gate
  together: noise gets dampened deterministically, and what survives is now
  attributable.
