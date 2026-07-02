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

- `mcp-market-data/macro_news.py` — `MACRO_RISK_BUCKETS` (line 378),
  thresholds (485-486), `_match_risk_bucket` (489), `assess_macro_risk` (505),
  `format_macro_for_prompt` risk rendering (572),
  `_fetch_macro_news_with_meta` freshness meta (149)
- `stock_cli.py` — `cmd_macro_news` adds `"risk"` to the JSON payload
- `scheduler/daily_briefing.py` — `_macro_block()` passes
  `assess_macro_risk(items)` into `format_macro_for_prompt`
- `.claude/skills/expect/SKILL.md` — RULE R3, new "Macro risk-off switch" bullet
- `mcp-market-data/tests/test_macro_news.py`,
  `scheduler/tests/test_daily_briefing.py` — new tests

## Review

Code review of PR #52: **no blockers, no should-fix**. One nit — the
`"trade war escalat"` substring could theoretically match e.g. "trade war
escalator" — dismissed as acceptable by design: matched evidence is surfaced
to the LLM alongside the level, and the bucket is weight 1 against an
ELEVATED threshold of 2, so a single-item false positive cannot trip
ELEVATED/RISK_OFF on its own. No code change.

### Codex round 1 (commit `0bdc8e4`) — 3 P2 findings, all verified and fixed

1. **RISK_OFF not enforced outside the prompt** — verified: in `--mode api`
   the risk level only rendered a prompt instruction; `log_predictions()`
   would still insert any parsed LIVE BULL. Fixed: `build_api_prompt` now
   returns `(prompt, macro_risk_level)` (risk computed once via the new
   `_macro_block_and_risk()`; `_macro_block()` unchanged for CLI modes), and
   `log_predictions(..., macro_risk_level=...)` deterministically skips LIVE
   BULL inserts under RISK_OFF with a visible log line (mirrors the LIVE BEAR
   skip; placed before the gate-component augmentation so no bars are fetched
   for a dropped row). The ELEVATED -0.05 trim stays prompt-only — the model
   already trims per instruction, and re-trimming in code would double-trim.
   Claude-code / codex-cli modes remain prompt-enforced: there the LLM logs
   via `bin/stock-cli` and deterministic CLI-side enforcement would couple
   the store to live network state (out of scope).
2. **Stale cache scored as live risk** — verified true: `fetch_macro_news`
   serves an arbitrarily old cache when all fetch attempts fail, and
   `get_macro_news` labelled it plain `"gdelt"`, so an old shock snapshot
   could keep RISK_OFF active with no note. Fixed: the fetch internals now
   expose freshness meta (`_fetch_macro_news_with_meta` → live / cache /
   stale-cache / empty; public `fetch_macro_news` signature unchanged),
   `get_macro_news` surfaces the stale path as source `"gdelt-stale"`, and
   `assess_macro_risk(items, stale=True)` degrades to NORMAL with a visible
   "stale feed" note — headlines are still shown as context.
3. **Generic emergency-meeting keywords** — verified: "emergency meeting" /
   "unscheduled meeting" / "긴급 회의" (weight 2) matched routine UN/government
   headlines on the BBC/Yonhap world feeds, tripping ELEVATED on one hit.
   Fixed: bucket is now central-bank-scoped only — "emergency rate cut/hike",
   "intermeeting cut", "emergency fomc", "central bank emergency", "currency
   intervention", "긴급 금리", "긴급 금통위", "외환시장 개입".

Six tests added for the round-1 fixes (RISK_OFF api-mode BULL skip,
stale-cache meta + source label + degrade at both the module and briefing
level, UN-emergency-meeting non-match); suite 687 passed / 24 network-deselected.

### Second-opinion review (Gemini via antigravity, commit `16a7056`)

1. **KR keyword false-positive traps (blocker)** — verified and fixed:
   - bare "전쟁" (war_conflict, w2) matched metaphorical finance headlines
     (무역/반도체/가격 전쟁) → replaced with "전쟁 발발" / "전면전"; "무역 전쟁"
     added to tariff_sanctions (w1), mirroring the EN "trade war escalat".
   - bare "디폴트" (market_crash, w2) matched the routine pension term
     "디폴트옵션" → replaced with "국가 디폴트" / "채무불이행" ("국가 부도" kept).
   - bare "폭락" (market_crash, w2) matched single-stock plunges → scoped to
     market-wide forms ("증시/코스피/주식시장/글로벌 증시 폭락").
   Negative tests added for all three traps.
2. **Syndicated-duplicate inflation (should-fix)** — verified true: upstream
   dedup is URL-only, so one wire story republished verbatim by 3 outlets
   summed to 6 → RISK_OFF without real corroboration. Fixed:
   `assess_macro_risk` now collapses near-identical titles (casefold,
   alnum-only normalization) before scoring; different headlines about the
   same event still corroborate, by design.
3. **Boundary test gap (should-fix)** — confirmed: the RISK_OFF-skip test
   exercised `log_predictions` directly but nothing asserted the
   `build_api_prompt` → `(prompt, macro_risk_level)` shape. Added
   `test_build_api_prompt_returns_prompt_and_risk_level`.
4. **Dismissed (nit)** — "only LIVE BULL intercepted; NEUTRAL/BEAR label
   capping left to the LLM": BEAR is already hard-rejected at the store level
   and NEUTRAL carries no long exposure, so the deterministic skip covers
   exactly the harmful class. No code change.

### Codex round 2 (commit `16a7056`) — 3 findings, all verified and fixed

- **A. GDELT fallback query missed the risk vocabulary (P2)** — verified: the
  curated query had no war/geopolitical terms, so on the RSS-down path the
  switch stayed blind to exactly the shocks it exists for. Fixed: severe
  terms (war, invasion, missile, nuclear, blockade, airstrike, "market
  crash", "circuit breaker", "sovereign default") appended to the ONE
  existing OR-group (no second GDELT call — 1 req/5s IP limit); test asserts
  the vocabulary and the single-group shape.
- **B. Round-1 CB narrowing overshot (P2)** — verified: "Fed holds emergency
  meeting" no longer matched. Fixed with a bucket-local two-term matcher
  (`_is_central_bank_emergency`): emergency-meeting wording ("emergency
  meeting/session", "긴급 회의/긴급회의") counts only alongside a central-bank
  token (word-bounded fed/fomc/ecb/boj/bok — "fedex" excluded — or substring
  "central bank"/연준/한은/금통위 for particle-attaching Korean). UN emergency
  meetings still score zero; not generalized beyond this bucket.
- **C. Stale meta dropped for empty cache (P3)** — verified: a valid-but-empty
  expired cache returned `([], "stale-cache")` but the truthiness check
  reported source "none", losing the stale-feed note. Fixed:
  `get_macro_news` propagates "gdelt-stale" whenever meta is stale-cache.

Six more tests for these two rounds (KR trap negatives, syndicated-title
dedup, CB two-term matcher positives + FedEx negative, stale-empty source,
query vocabulary, api-prompt boundary); suite 693 passed / 24 network-deselected.

### Post-merge live finding (2026-07-02)

Live verification on merged master caught a real-world false positive: the
Yonhap headline "(LEAD) Parliamentary committee enters blockaded ballot
counting site for inspection" matched war_conflict via bare "blockade"
(weight 2) → ELEVATED, which would have wrongly trimmed every pick in the
day's briefing by 0.05. Domestic-politics "blockaded X" (and the Korean
국회/도로 봉쇄) is common in Yonhap output. Fixed in a follow-up PR: "blockade"
→ "naval blockade" / "strait blockade" / "port blockade", "봉쇄" → "해상 봉쇄" /
"해협 봉쇄" ("strait of hormuz" still covers the classic case); regression test
uses the actual live headline plus military-context positives.

## Retrospective

Reusing the Stage 21 feed meant the whole switch is ~150 lines + tests, with
zero new dependencies or keys. The thresholds (2/6) are educated guesses —
worth replaying against the June headline archive once available, and the
keyword lists will need occasional curation (they're constants by design, so
tuning is a reviewed PR, not config drift).
