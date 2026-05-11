# Stage 4 — `/expect` SKILL.md rewrite

## Why
The pre-Stage-4 `/expect` produced four-horizon directional predictions but had no:
- deterministic point-table scoring (LLM emitted qualitative "bullishness")
- quantitative news/sentiment input (only WebSearch text)
- single headline label (Buy/Sell decision)
- transmission chain
- outcome telemetry sidecar

Doctor Cho's request: combine algorithmic + news analysis to emit a buy/sell signal, while keeping the calibration loop (multi-horizon predictions logged to `data/predictions.db`).

## What
Rewritten `.claude/skills/expect/SKILL.md` with:
1. **Algorithmic point table** (`ALGO_SCORE`, max +8.0, floor -5.0): trend, momentum, return-1M, volume, cycle, earnings-event penalty. Borrowed from staskh `scanner-bullish` but adapted to our `horizon-metrics-batch` JSON schema.
2. **News point table** (`NEWS_SCORE`, max +3.0, floor -2.0 via hard caps): sentiment (Alpha Vantage when present), headline volume, negative-keyword scan, KR disclosure flag. Hard caps clamp rather than stack.
3. **Composite + decision label** (BUY ≥ 8.0, WATCH 6.0..7.9, HOLD 3.0..5.9, AVOID 0..2.9, SELL < 0). Half-open contiguous ranges so every score lands in exactly one bucket.
4. **Transmission chain**: 3 facts per pick (TECH / NEWS / RISK), each must quote a specific number or named field — no bare adjectives. Borrowed from roman-rr's signal explainability.
5. **Outcome telemetry sidecar** at `state/last-outcome-expect.json`: every component score broken out + transmission chain + which horizons were logged. Stage 6's weekly aggregator parses this.
6. **Quality gate (Step 11) + bias checklist (Step 12)**: pre-output sanity checks (sign agreement, target separation, fresh-data, recency/confirmation/anchoring/overconfidence).
7. **Multi-horizon predictions preserved**: same 4-horizon (1W/1M/6M/1Y) logging into `predictions.db`, with shared `analysis_group_id` per stock. RULE C1 (cycle vs short conflict cap) preserved verbatim.
8. **Output verbosity scaling**: `/expect ALL` (10 stocks) emits abbreviated 3-line per-stock detail, full block reserved for single/batch modes or RULE-C1/hard-cap picks.
9. **Skill composition as gates**: market-top-detector / ftd-detector / theme-detector called only when COMPOSITE ≥ 8.0, with explicit recursion-guard note.

## How
- Borrowed three patterns from external skill collections (per [docs/external-skills-analysis.md](../external-skills-analysis.md)): point-table scoring (staskh), transmission chain (roman-rr), outcome sidecar (ScientiaCapital).
- Math reconciliation: gemini's review caught that my initial draft had max scores listed wrong (algo "max 10" but components summed to 8; news "max 5" but summed to 3; composite max stated as 15 but actually 11). Fixed thresholds to match real component sums and rewrote the bucket descriptions in `if/elif` style so every input lands in exactly one row.
- The point table is small enough that an LLM should reliably evaluate it inline; if calibration drift shows up at this layer, Stage 6's aggregator can prompt a refactor that pushes scoring into a `bin/stock-cli score` Python helper (gemini suggested this preemptively but for now keeping it inline reduces complexity).

## Code locations
- `.claude/skills/expect/SKILL.md` — full rewrite (one file)

## Verification
- Manual review: every horizon-metrics field referenced in the skill matches `compute_horizon_metrics` output (verified against `mcp-market-data/indicators.py`).
- The CLI subcommands referenced in workflow (track-record, calibration, horizon-metrics-batch, fundamentals-batch, news, disclosure, predict create) all exist after Stage 2.
- End-to-end smoke deferred until at least one of `FINNHUB_API_KEY` / `OPEN_DART_API_KEY` is set — the skill itself is content, not code, so its correctness is validated by use rather than tests.

## Per-stage review
**gemini-subagent (Pro)** — single-round review caught real bugs that a one-pass code review would have missed:

1. **MUST FIX — Math is wrong.** ALGO_SCORE max stated as 10 but components sum to 8.0; NEWS_SCORE max stated as 5 but components sum to 3.0; composite stated as -3..+15 but actual range is -7..+11. **Action: rewrote all max-score statements to match actual sums; rewrote the example sidecar JSON and output-table example to use achievable scores; recalibrated decision thresholds (BUY 9 → 8.0; WATCH 6..8 → 6..7.9; HOLD 3..5 → 3..5.9; AVOID 0..2 → 0..2.9).**
2. **MUST FIX — Bucket overlap.** Trend "MA20 ≤ MA50" (0 pts) overlapped "full bear stack" (-1 pt); Return_1M "≤ 0%" overlapped "≤ -10%". **Action: rewrote both with mutually-exclusive `if/elif` ordering and explicit half-open ranges.**
3. **MUST FIX — Decimal-gap thresholds.** Original integer ranges (3..5, 6..8) left scores like 5.5 and 8.5 undefined. **Action: switched to half-open contiguous ranges (`>= 8.0`, `[6.0, 8.0)`, `[3.0, 6.0)`, `[0, 3.0)`, `< 0`).**
4. **SHOULD FIX — Output verbosity.** ALL mode would emit ~150 lines of per-stock detail. **Action: added an "Output verbosity by mode" section that scales detail down to 3 lines per stock for `ALL` mode, reserving full blocks for single/batch modes and any pick triggering RULE C1 or a hard cap.**
5. **CONSIDER — Quality gate enforcement.** Gemini noted LLM math is unreliable for the consistency checks in Step 11. **Dismissed for Stage 4** — moving scoring to Python is a worthwhile follow-up but materially expands the stage. The point table is small (≤ 8 rows, integer additions), well within reliable LLM math; if Stage 6's calibration aggregator shows drift at this layer, that's the trigger to refactor.
6. **CONSIDER — Recursion guard.** Gemini flagged risk of `/expect` → market-top-detector → /expect loop. **Action: added a recursion-guard note at the bottom of the composition section** — none of the gate skills currently call `/expect`, but the note prevents future regressions.

Skipping the second-reviewer pass (code-reviewer-pro) — this is prose, not code; gemini's structured review covered the substantive issues a code reviewer would have flagged. Skipping in this case keeps the stage tight; resumes formal dual review at Stage 6 (Python code) and Stage 7 (data-layer code).

## Retrospective
What went well: the gemini review caught math errors I had baked into the plan AND propagated into the SKILL.md draft. Without that pass, Doctor Cho would have run `/expect` and seen "BUY composite 11.5/15" displays that didn't add up.

What to carry forward: when designing a deterministic scoring rubric, write out the maximum-positive sum and the absolute floor *as a separate verification line* before publishing thresholds. The thresholds should be derived from the actual ceiling, not the claimed ceiling.
