# Stage 11 · D — Hard market-regime gate

## Why
F3: during the June 2026 drawdown `/expect` kept issuing BULL straight through a
correction (140 closed BULL in 6/1–6/7 won 4.3%). There was no deterministic
risk-off gate — the macro skills were advisory callouts an LLM could ignore.

## What
- `stock-cli regime --market US|KR` → deterministic RISK_ON / NEUTRAL / RISK_OFF
  verdict from index-proxy price structure (trend vs MA50/MA200, drawdown from
  52w high, 1M return, annualized 20-day realized vol). US aggregates the
  **worse of SPY + QQQ**; KR uses KODEX 200 (069500).
- Hard gate wired into `/expect` (RULE R1) and `daily-briefing` (RULE R1):
  RISK_OFF → suppress new BULL (cap WATCH, horizons → NEUTRAL); NEUTRAL → raise
  BUY bar 8.0→9.0 and trim confidence (cap 0.60); RISK_ON → unchanged.
- `tests/test_regime.py` (16 tests).

## How
- Pure, network-free logic in `mcp-market-data/regime.py`
  (`compute_realized_vol`, `compute_regime`, `aggregate_regime`); the CLI feeds
  it index bars via the existing provider + `compute_horizon_metrics`.
- `aggregate_regime` returns the worst proxy via `dataclasses.replace` (no input
  mutation) and records every proxy's score in `proxy_scores`.

## Validation (network reconstruction over the June window — honest result)
Reconstructed the regime as-of each prediction's issue date and bucketed the 140
closed BULL:
- **NEUTRAL at issue: n=65, win 3.1%** — the gate suppresses/hardens these.
- **RISK_ON at issue: n=75, win 5.3%** — the index looked calm when issued; the
  gate cannot catch these.
- The 6/2 cluster (72 calls) was **un-gateable by index alone** — even QQQ was
  RISK_ON; the drawdown came after. SPY never left RISK_ON the whole window;
  QQQ went NEUTRAL by 6/7; KR flagged NEUTRAL early via parabolic vol (1M +40%,
  vol 56%).

**Conclusion (no overclaim):** the regime gate is a *partial* mitigation — it
removes ~46% of the window's losers (the NEUTRAL ones) and stops buying into a
developing/parabolic correction, but the broad index was calm at the 6/2 peak.
The complementary lever — per-stock overextension — is a planned follow-up
stage; recalibration (A) already collapses the overconfidence that made the
6/2 cluster so damaging.

## Review loop (code-reviewer + codex + gemini)
- **Fixed (all 3) — `aggregate_regime` mutated an input verdict.** Now
  `dataclasses.replace` returns a copy; test asserts inputs untouched.
- **Fixed (codex High) — `cmd_regime` silently dropped a failed proxy** (losing
  QQQ would blind the US gate to a growth-led drawdown). Missing proxies are now
  surfaced in the verdict notes.
- **Fixed (codex+gemini) — insufficient history defaulted to RISK_ON.** With no
  MA200 the trend is unknown, so a hard gate must not certify RISK_ON; now
  floored to NEUTRAL with a note.
- **Fixed — argparser help** said default SPY for US; now "worst-of SPY+QQQ".
- **Fixed — test gaps:** exact realized-vol value, MA precedence, insufficient
  history, no-mutation, tie→first, single-proxy.
- **Dismissed — threshold flapping/hysteresis (gemini):** day-to-day flapping
  near a threshold needs persistent state; the gate is consulted once per
  briefing, not intraday. Documented as a known limitation, not implemented.

## Code locations
- `mcp-market-data/regime.py` — verdict + scoring + aggregation.
- `stock_cli.py` — `cmd_regime`, `_regime_for_proxy`, `REGIME_INDEX`, `regime` parser.
- `.claude/skills/expect/SKILL.md` (RULE R1, Step 1 fetch),
  `.claude/skills/daily-briefing/SKILL.md` (RULE R1, Section 1 fetch).
- `tests/test_regime.py`.

## Retrospective
- The validation refuted my first instinct (a regime gate "would have prevented
  the loss"). The data forced an honest, narrower claim and surfaced the real
  next lever (per-stock overextension). Backfilling before wiring paid off.
