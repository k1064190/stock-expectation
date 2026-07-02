# Stage 24 — Deterministic macro risk-off switch (NORMAL / ELEVATED / RISK_OFF)

## Why

During the early-June 2026 macro shock (SPY -4.5% from 6/2 to 6/10) the system
logged ~229 BULL predictions on 6/1–6/5; that weekly cohort averaged **-11.3%**
with 135 misses. LIVE BEAR predictions are hard-rejected by design
(`mcp-prediction-store/models.py` ~line 521, measured ~0% BEAR win rate), so the
system is structurally long-only — macro risk had **no expression channel** and
the system walked straight into the drawdown. The macro-news feed (Stage 21)
already carried the shock headlines; nothing acted on them.

## What

- **`assess_macro_risk(items) -> dict`** in `mcp-market-data/macro_news.py`:
  deterministic keyword tripwire (not NLP) over the already-fetched macro
  headlines. Five buckets, English + Korean keywords, case-insensitive
  substring match: `war_conflict` (w2), `market_crash` (w2),
  `emergency_central_bank` (w2), `oil_supply_shock` (w1), `tariff_sanctions`
  (w1). Each item counts once, at its most severe matching bucket. Thresholds
  are module constants: score ≥ 2 → `ELEVATED` (one severe or two moderate
  headlines), score ≥ 6 → `RISK_OFF` (corroborated shock, e.g. three severe
  headlines). Output: `{risk_level, risk_score, matched (evidence), note}`.
- **Fail-open, visibly**: RSS + GDELT both unreachable → empty items →
  `NORMAL` with an explicit note ("risk not assessed (fail-open NORMAL)") —
  same philosophy as the catalyst gate's `gate_unavailable`.
- **CLI**: `stock-cli macro-news` JSON now includes a `risk` key (no new flag —
  smallest surface).
- **Daily briefing**: `_macro_block()` prepends a `MACRO RISK: <level>` line +
  gate instruction to the macro-headlines block in both US and KR prompts —
  `RISK_OFF` → log NO new BULL this run (labels cap at WATCH); `ELEVATED` →
  additional -0.05 confidence trim (stacks with the R3 trims).
- **expect skill**: one new bullet inside RULE R3 ("Macro risk-off switch")
  with the same RISK_OFF / ELEVATED / fail-open rules; R1/R2/R3 structure
  untouched (diff kept minimal — another branch edits the same file).

## How

Kept it a tripwire: severity-weighted keyword buckets and two integer
thresholds, all module constants (no config surface). Phrases are deliberately
specific ("nuclear threat" not "nuclear", "retaliatory tariff" not "tariff") so
routine news flow doesn't fire the switch; corroboration (multiple matched
headlines) is required to reach RISK_OFF. The switch reuses the existing
macro-news fetch (RSS primary / GDELT fallback, PR #49) — no new network
surface. Consumption goes through the existing prompt-block channel and the
existing R3 gate language, so trims stack exactly like the earnings/macro
event trims (-0.05 steps; caps change labels).

TDD: 13 new tests (11 in `test_macro_news.py`: NORMAL/ELEVATED/RISK_OFF item
sets, Korean-language matching, once-per-item counting, fail-open, prompt
rendering; 2 in `test_daily_briefing.py`: RISK_OFF line + no-new-BULL rule in
the briefing block, fail-open note). No network in tests. Full suite:
681 passed, 24 network-marked deselected.

## Code locations

- `mcp-market-data/macro_news.py` — `MACRO_RISK_BUCKETS` (line 342),
  thresholds (447-448), `_match_risk_bucket` (451), `assess_macro_risk` (467),
  `format_macro_for_prompt` risk rendering (521)
- `stock_cli.py` — `cmd_macro_news` adds `"risk"` to the JSON payload
- `scheduler/daily_briefing.py` — `_macro_block()` passes
  `assess_macro_risk(items)` into `format_macro_for_prompt`
- `.claude/skills/expect/SKILL.md` — RULE R3, new "Macro risk-off switch" bullet
- `mcp-market-data/tests/test_macro_news.py`,
  `scheduler/tests/test_daily_briefing.py` — new tests

## Retrospective

Reusing the Stage 21 feed meant the whole switch is ~150 lines + tests, with
zero new dependencies or keys. The thresholds (2/6) are educated guesses —
worth replaying against the June headline archive once available, and the
keyword lists will need occasional curation (they're constants by design, so
tuning is a reviewed PR, not config drift).
