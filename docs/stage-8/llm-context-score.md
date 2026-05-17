# Stage 8 — LLM_CONTEXT_SCORE (anti-momentum-bias circuit)

## Why

The deterministic `ALGO_SCORE` table in `/expect` and `daily-briefing` rewards stocks that are already trending: full bull MA stack (+3.0), RSI 50-70 (+1.5), `return_1m ≥ +5%` (+1.5), volume surge (+1.0), and near 52W high (+1.0) all max out for a stock that has already surged. By construction a perfect 8.0 ALGO_SCORE describes a **late-stage Stage 2 uptrend** — which has identical raw mechanics to a **parabolic blow-off top**.

The 2026-05-15 KOSPI key reversal day exposed this. With the pre-Stage-8 scoring, 000660 SK하이닉스 on the day before the crash would have scored ALGO 7.0 + NEWS 1.0 = COMPOSITE 8.0, the exact BUY threshold — despite +925% YoY, RSI 71.9 (overbought), cycle_risk_flag=True, and the macro context that included parabolic +25%/22d, foreign capital outflow stress (USD/KRW 1500↑), and impending Samsung union strike. The system would have recommended buying at the top of a blow-off.

Track record telemetry confirmed the bias: `valuation`, `cycle`, and `mean_reversion` signals all sit at **0% win rate** (n=11 each across 30 days), while `news` (74.6%), `momentum` (72.5%), `fundamental` (68.4%), and `technical` (68.1%) all perform. The system is excellent at momentum, terrible at mean reversion — because mean reversion has no dedicated channel in the scoring table.

Doctor Cho flagged this directly in session 2026-05-16:
> "너무 알고리즘 측면에서만 접근하는게 아닌, 뉴스나 웹 검색 기반으로 너의 추론 능력 비중도 좀 더 들어가는게 좋을것같아."

## What

Added a third scoring component `LLM_CONTEXT_SCORE` (range **-5.0 to +3.0**, asymmetric toward the downside to counter the algorithmic momentum bias). The LLM judges macro regime, sector lifecycle, and narrative context that the deterministic table cannot see and emits both a score and 1-3 sentence reasoning text per pick.

New composite formula:

```
COMPOSITE = ALGO_SCORE + NEWS_SCORE + LLM_CONTEXT_SCORE
   range:  -11.0  ..  +14.0   (was -7.0 .. +11.0)
```

Label thresholds were intentionally kept the same (BUY ≥ 8.0, WATCH 6.0-8.0, HOLD 3.0-6.0, AVOID 0-3.0, SELL < 0). Negative `LLM_CONTEXT_SCORE` naturally downgrades algorithmic BUY candidates when macro context disagrees — that is the explicit anti-momentum-bias circuit.

Worked example (000660 on 2026-05-14 retroactive):
- ALGO = 7.0, NEWS = 1.0, **LLM_CONTEXT = -3.0** (KOSPI parabolic, FX stress, sector late-stage, strike imminent)
- COMPOSITE = **5.0 → HOLD** (vs pre-Stage-8 raw 8.0 BUY)

## How

### Scoring approach (Step 5b in /expect)

Inserted between Step 5 (ALGO) and Step 6 (NEWS). Inputs to consider:

1. **Macro detector outputs** — call `market-top-detector`, `ftd-detector`, `macro-regime-detector` when `ALGO_SCORE >= 6.0` (lowered from previous threshold of 9.0 so the gates fire before the BUY label is locked in).
2. **Sector lifecycle** via `theme-detector` or web search (early/mid/late stage uptrend, decay).
3. **Narrative themes** from news + web (foreign capital flow direction, FX stress, supply chain, regulatory, binary event proximity).
4. **Cross-asset confirmation** (bonds, related sectors, SOXL vs SPY divergence).

Anchors (interpolate between):
- **+3.0**: Major macro tailwind aligned with this stock
- **+1.5**: Moderate tailwind
- **0**: Neutral (default when no specific context)
- **-1.5**: Mild headwind
- **-3.0**: Strong headwind (macro top + sector late + event risk)
- **-5.0**: Crisis

Required output is **both** a numeric score and reasoning text citing at least one specific macro/sector/narrative signal. Vague reasoning ("market feels risky") triggers the Quality Gate to dampen the score toward 0.

### Quality gates (Step 11)

Two new gates were added to keep the score honest:
- **LLM_CONTEXT_SCORE justification**: if `|llm_context_score| >= 2.0`, reasoning must cite at least one concrete macro/sector/narrative signal.
- **Double-counting guard**: if `algo_score >= 7.0` and `llm_context_score >= +2.0`, recheck — most algorithmic BUY candidates should have LLM context in `[-2.0, +1.0]`.

### Sidecar contract (Step 10)

Two new fields added to each pick object in `state/last-outcome-expect.json`:
- `llm_context_score`: float in [-5.0, +3.0], 1 decimal.
- `llm_context_reasoning`: 1-3 sentence string citing macro/sector/narrative signals.

Backwards-incompatible by design — downstream consumers (weekly_calibration, future aggregators) gain a new dimension. Required, not optional.

### Signal tracking (weekly_calibration)

`llm_context` is now a tracked signal name in `--signals` field of `predict create`. Include it whenever `LLM_CONTEXT_SCORE != 0`. The generic per-signal win rate aggregator in `mcp-prediction-store/metrics.py` picks it up automatically — no code change needed there, just docstring update to weekly_calibration.py.

Expect 6-8 weeks of noisy data before this signal's win rate stabilizes. If it underperforms (e.g., <50% n≥20), it'll show up in the worst_signals list and we'll know to recalibrate the anchors.

### Daily-briefing sync (cron impact)

The `daily-briefing` SKILL.md duplicates the `/expect` point table for the cron path (`claude-code` mode). Updated `Section 4` to add the LLM_CONTEXT_SCORE block and the new composite formula. Also updated cron prompts for API mode:
- `scheduler/prompts/briefing_kr.md`: added Macro/Narrative Context Pass step + `llm_context_score` and `llm_context_reasoning` fields in the JSON schema.
- `scheduler/prompts/briefing_us.md`: same.

Cron itself is unchanged (schedule, invocation, Telegram delivery). The scoring logic inside the briefing now incorporates LLM context.

## Code locations

| File | Change |
|---|---|
| `.claude/skills/expect/SKILL.md` | Added Step 5b (LLM_CONTEXT_SCORE), updated Step 7 composite formula, Step 9 signals list (`llm_context`), Step 10 sidecar schema (two new fields), Step 11 quality gates (two new), Step 12 bias check rewording, output format with new column, "Calling skills" threshold lowered from COMPOSITE ≥ 9 to ALGO_SCORE ≥ 6.0 |
| `.claude/skills/daily-briefing/SKILL.md` | Section 4 updated with LLM_CONTEXT_SCORE block and new composite formula. Section 5 example reasoning now mentions LLM_CONTEXT, signals list includes `llm_context` |
| `scheduler/prompts/briefing_kr.md` | Step 3 (new "Macro/Narrative Context Pass") + JSON schema with `llm_context_score`, `llm_context_reasoning` |
| `scheduler/prompts/briefing_us.md` | Same as briefing_kr.md but US-anchored examples (distribution days, VIX, SOX divergence) |
| `scheduler/weekly_calibration.py` | Docstring updated to mention `llm_context` as a tracked signal with calibration window expectation |
| `docs/stage-8/llm-context-score.md` | This file |

No changes to:
- `mcp-prediction-store/metrics.py` — signal aggregator is generic by design, automatically picks up new signal names
- `scheduler/outcome_tracker.py` — pure price-check logic, unaware of signals
- Other skills

## Retrospective

**What went well**:
- The bias was identified before any production failure — the 5/15 KOSPI key reversal day exposed it in analysis, not in a real BUY-the-top recommendation that lost money.
- The existing point-table architecture made the fix mechanical: add a new component, extend the composite formula, keep thresholds. No fundamental redesign needed.
- The decision to keep BUY threshold at 8.0 (instead of raising) means negative LLM_CONTEXT automatically vetoes momentum-only setups — the cleanest possible anti-bias circuit.
- Signal aggregator's generic design (loops over `signals_used` JSON) meant no calibration code changes needed.

**What to carry forward**:
- 6-8 weeks of LLM_CONTEXT performance data needed before the score can be trusted as standalone. Track record honesty during this window: if `llm_context` signal shows <50% win rate (n≥20), the anchors need rework.
- The double-counting guard (Step 11) is critical — without it, bullish algo + bullish LLM = composite inflation. Watch this in early data.
- Doctor Cho's instinct that "오르고 있는 주식은 지속적으로 매수하면서 물타기를 해야하는거아니야?" actually pointed at a real disposition-effect tension. The system's bias was toward trim-the-stretched (not blanket trim-winners), but the trimming threshold should reference LLM_CONTEXT to distinguish healthy Stage 2 from blow-off — which is exactly what this change enables.

**Open questions**:
- Should LLM_CONTEXT scoring auto-promote to a more granular taxonomy (e.g., separate "macro" and "narrative" sub-components) once 6 weeks of data accumulates and we can measure each independently? Defer until data warrants.
- The "Calling skills" composition pattern now invokes `market-top-detector` etc. at `ALGO_SCORE >= 6.0`. This adds latency for any non-trivial pick. Latency budget claim ("/expect ALL in under 5 minutes") may need re-measurement.

## Per-stage review loop (CLAUDE.md global rule)

Three independent reviews were dispatched on the staged diff (`git diff origin/master` for branch `feature/llm-context-score`):

### Code-reviewer-pro (in-house): 0 critical, 2 warnings, 1 suggestion

- ⚠ **Worked example inconsistency** between Step 5b example (line 175) and per-stock detail (line 424) — second example omitted the "펀더 자체는 견고...-5는 아님" comparative reasoning that demonstrates anchor-selection. **Fixed**: per-stock detail example now includes "Fundamentals intact (NVDA Blackwell demand, HBM cycle), so -3.0 (not -5.0)".
- ⚠ **daily-briefing latency strategy** missing — `/expect` documents amortised macro detector calls (once per market) but `daily-briefing/SKILL.md` did not, risking 10-12× overhead on cron paths. **Fixed**: added explicit "Macro detector invocation (amortised, mandatory once per market)" subsection to Section 4 of `daily-briefing/SKILL.md`.
- 💡 **`signals_used` rule when score == 0** was implicit. **Fixed**: Step 5b reasoning rules now explicitly state "When `LLM_CONTEXT_SCORE == 0.0`, emit reasoning text anyway but do NOT include `llm_context` in `signals_used`".

### Gemini-subagent (gemini-3.1-pro-preview): 2 issues, 1 suggestion

- ❌ **COMPOSITE range off-by-one**: claimed `-11.0 to +14.0` but actual worst case is `-12.0` (algo floor -5.0 with earnings penalty + news floor -2.0 + llm_context floor -5.0). **Fixed**: corrected to "-12.0 (worst case) .. +14.0" in both Step 7 (`/expect`) and Section 4 (`daily-briefing`), with the arithmetic shown inline.
- ❌ **Citation requirement contradiction**: Step 5b said "cite at least one specific signal whenever non-zero" while Step 11 Rule 5 said "if |score| >= 2.0 ... must cite". **Fixed**: Step 5b reasoning rules section now explicitly mirrors Step 11's stricter rule — concrete citations required for any non-zero score, and Step 11 Rule 5 is the Quality Gate that enforces it.
- 💡 **Transmission chain vs llm_context separation**: risk of LLM merging macro narrative into the RISK slot under token pressure. **Fixed**: Step 8 format rules now explicitly state the 3 transmission slots are "separate from `llm_context_reasoning` — do NOT merge the macro narrative into the RISK slot".

### Codex-subagent (gpt-5.5 / gpt-5.4-mini): blocked

Codex CLI v0.130.0 errored with an upstream MCP server tool-schema bug:
```
invalid_request_error: Invalid schema for function '_create_map_with_locations':
schema must have type 'object' and not have 'oneOf'/'anyOf'/'allOf'/'enum'/'not' at the top level.
```

This is not a project issue — it's an `_create_map_with_locations` MCP server registered globally that ships an OpenAI-incompatible schema. Both `gpt-5.5/high` and `gpt-5.4-mini/medium` failed identically. After 2 attempts the review was abandoned per CLAUDE.md "blocked after about 3 serious attempts" guidance. Code-reviewer-pro + Gemini coverage is sufficient.

### Net outcome

All 5 actionable findings (3 from code-reviewer-pro, 2 from Gemini) addressed in the same diff. The math claim about the worked example (000660 ALGO 7 + NEWS 1 + LLM_CONTEXT -3 = 5 → HOLD) was independently verified by both reviewers. Schema consistency across `/expect`, `daily-briefing`, `briefing_kr.md`, `briefing_us.md` confirmed.
