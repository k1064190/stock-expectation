# Stage 12 — Blended funnel + cron gate enforcement + store-level BULL gate (WT-A.2)

## Why

The cron briefing's only candidate stream was momentum (`|5d|≥15%` OR `vol≥2x`), and the prompt
restricted the LLM to that list — so it structurally could only recommend already-surged names.
The investigation tied this to a ~30% LIVE BULL hit rate; the WT-A.3 backtest showed the parabolic
(>40% trailing-month) momentum cohort is the worst (≈24–55%) while a not-yet-extended stream is
competitive. Critically, the documented R1/R2 gates were **not enforced in production**: the cron
prompt templates never mentioned them, and under `codex-cli` the LLM logs predictions itself via
`bin/stock-cli predict create`, bypassing any in-process funnel logic. Doctor Cho chose **gate +
additive blend + cohort tagging**.

## What

1. **Store-level BULL gate** (the one enforcement that survives cron) — `insert_prediction`
   hard-rejects a `--source LIVE` BULL whose `components` show `overextension=EXTREME` or
   `return_1m > 0.20`, mirroring the existing LIVE-BEAR rejection. Fail-open when components absent.
2. **Blended funnel** — `assemble_blended_candidates` merges the pre-surge stream (first) + a capped
   momentum slice + anchors-as-macro-reference, de-biased (no longer biggest-mover-first), deduped,
   each candidate tagged `discovery_source`/`setup_type`, with an optional WT-C sector boost hook.
3. **Cron + API prompt rewiring** — `build_claude_code_prompt` and `fetch_*_market_data` use the
   blend; both briefing prompt templates and the daily-briefing SKILL (new §4.5) now spell out
   R1/R2, the parabolic cap, `reward:risk ≥ 1.5`, and the mandatory `components`
   (`overextension`+`return_1m`+`discovery_source`+`setup_type`).

## How

- `_check_overextension_gate(pred)` in `mcp-prediction-store/models.py` reads the components the
  briefing already passes — no network in the store layer, fully unit-testable.
- `scheduler/blended_funnel.py` keeps the merge/format pure (`blend_streams`,
  `format_blended_for_prompt`) and isolates I/O in `assemble_blended_candidates`, reusing the
  existing discover functions.
- The cap is enforced at the **store** (hard) and surfaced in the **prompt** (advisory) — defense in
  depth, since codex bypasses the in-process path.

## Code locations

- `mcp-prediction-store/models.py` — `_check_overextension_gate` + `insert_prediction` guard.
- `scheduler/blended_funnel.py` — blend + format + assemble.
- `scheduler/daily_briefing.py` — both cron (`build_claude_code_prompt`) and API
  (`fetch_us/kr_market_data`) call sites; removed now-unused candidate_discovery imports.
- `scheduler/prompts/briefing_{us,kr}.md`, `.claude/skills/daily-briefing/SKILL.md` §4.5 + components example.
- Tests: `mcp-prediction-store/tests/test_models.py` (6 gate cases),
  `scheduler/tests/test_blended_funnel.py` (5 blend/format cases).

## Verification

- `uv run pytest -q -m "not network"` → 460 passed (no regressions).
- **Store gate E2E via CLI** (the codex path): EXTREME LIVE BULL → *gated*; `return_1m=0.30` → *gated*;
  `return_1m=0.12` pre-surge → *created*.
- Cron prompt build (`build_claude_code_prompt('US')`) renders Pre-surge / Momentum / Anchors
  sub-sections (tagged rs_leader/pullback) and contains R1/R2/PARABOLIC CAP/COMPONENTS text.

## Ship-gate status & decision

The WT-A.3 ship gate (EXPIRED-aware) **did not pass**: at 1W pre-surge is conclusively worse
(−13.3pp, dead-money expiry 60% vs 33%), and at 1M the cohorts are tied (−1.9pp, CI spans 0). So
this stage ships as **additive + tagged**, NOT a momentum replacement: the **store-level
overextension gate is the proven, independently-justified win** (removes the parabolic tail), and
pre-surge is kept for diversification + forward measurement — but **anchored at 1M+** (a base needs
weeks; at 1W it just expires). The prompts/SKILL now encode that horizon rule.

## Review loop

- **code-reviewer-pro** (A.2 diff): 0 critical, 1 warning — `isinstance(trailing,(int,float))` lets a
  `bool` slip through (`True>0.20` would falsely gate). **Fixed** (`and not isinstance(trailing,bool)`)
  + added `test_live_bull_boolean_trailing_return_is_not_treated_as_parabolic`. Integration checks
  (both modes wired, imports clean, error path safe) passed.
- **gemini -m pro**: substantive review landed on WT-A.3 (the EXPIRED gate P0, actioned there); its
  effect on A.2 is the corrected ship-gate framing + the 1M-horizon rule above.
- **codex -m gpt-5.5 (3 P1, actioned)**: (a) a numeric *string* `return_1m: "0.25"` bypassed the
  gate's `isinstance` check → added float-coercion of string forms. (b) `blend_streams` didn't seed
  `seen` with anchor tickers → a pre-surge anchor could appear as both pick and anchor → seed fixed.
  (c) API-mode `log_predictions` inserted LIVE BULL with `components=None` (gate fails open) → added
  `_augment_gate_components` which recomputes overextension/return_1m from fresh bars before insert
  (fail-open). 
- Outcome: 1 warning + 3 P1 fixed & tested; residual: the codex-cli path still trusts the LLM to pass
  components (mitigated by the SKILL mandate + verified gate); authoritative compute there is noted
  future hardening.

## INTEGRATION TODO (post-merge)

- WT-C: call `sector-rs --write` before discovery, pass `_load_sector_verdicts(market)` into
  `assemble_blended_candidates(..., sector_verdicts=...)`.
- WT-D: call `events.evaluate_gate` over candidate tickers, inject an `{event_gate}` block, add
  RULE R3 to the briefing prompts/SKILL (it stacks under R1/R2 here).

## Retrospective

- What went well: the store-level gate is small, testable, and the *only* change that actually binds
  codex-cli — proving it E2E via the CLI confirmed the enforcement the prompt alone could never give.
- Carry forward: the prompt-level cap still depends on the LLM passing honest components; the store
  gate is the backstop. A future hardening could have `cmd_predict_create` recompute overextension
  from fresh bars when components are missing, closing the fail-open bypass.
