# Stage 25 — Weekly gold trend analysis job

**Why.** Doctor Cho is dollar-cost-averaging into KRX 금현물 (physical gold, holds 2g) and wanted a
recurring weekly checkpoint — like the existing stock crontab jobs — answering two questions: is now a
reasonable spot to add this week's tranche, and is gold structurally biased to rise long-term. A backing
deep-research pass (WGC / IMF / Fed / J.P. Morgan, 2026-07-03) established the macro facts that seed the
scorecard: central-bank buying is structurally elevated (~1,000t/yr since 2022 vs ~500t prior decade),
the worst historical gold bears required Volcker-style punitive real rates (~5–8%, currently absent), and
the "dollar collapse" narrative is overstated (USD reserve share ~58%, flat since 2022).

**What.** A new weekly scheduler job that scores KRX gold (via ACE KRX금현물 ETF `411060` as the tradeable
proxy) and emits a Korean `ACCUMULATE / HOLD / PAUSE` verdict to Telegram + a report file. It combines:
- a deterministic **technical** score (weekly-entry timing): trend vs MA50/MA200, drawdown-from-52w-high,
  RSI band;
- a deterministic **macro** scorecard (long-term bias): central-bank buying + real-rate regime + dollar
  structural drift + FX (원/달러), scored by Python rules from a mix of live data and a quarterly-refreshed
  config file;
- one optional `claude -p` Korean summary paragraph (fail-open);
- an optional approximate personal 2g position P&L line (spot USD/oz × 원/달러 ÷ 31.1035, labelled 근사);
- 12-week rolling state (`state/gold_trend.json`) for week-over-week deltas — **never** `predictions.db`.

Verdict logic: hard **PAUSE** flags (RSI > 75, real-rate *punitive*, or a manual `risk_off` kill-switch)
override; else **ACCUMULATE** when macro ≥ 55 and technical ≥ 50; else **HOLD**.

**How.** Built over 8 TDD tasks (config → technical → macro → verdict → rendering → state → fail-open
fetchers → orchestration) via subagent-driven development: a fresh implementer per task, a spec+quality
review per task, then a whole-branch opus review. Fetchers are fail-open (any failure returns a sentinel;
`build_context` converts each into a labelled ⚠ degraded line; the report is written to disk before the
best-effort Telegram send). Runs Sunday 21:00 KST, before the 22:00 weekly calibration.

A key calibration decision surfaced from the first live dry-run: real-rate `restrictive_above_pct` (2.0)
originally did double duty as both the sub-score floor AND the hard-PAUSE veto, so live DFII10 ≈ 2.2%
force-PAUSEd accumulation — contradicting the research (2.2% is a headwind, not Volcker-punitive). Per the
owner's decision the two were **decoupled**: `restrictive_above_pct` (2.0) now only floors the sub-score,
and a new `punitive_above_pct` (3.5) drives the hard PAUSE veto. After the change the dry-run returns
🟢 ACCUMULATE at 2.2%.

**Code locations.**
- `scheduler/gold_trend.py` — the whole job (scoring functions, fail-open fetchers, `build_context`, `main`).
  Real-rate decouple: `_real_rate_subscore` (4-arg, punitive checked before restrictive), `compute_macro`
  (`punitive_flag`), `decide_verdict` (`macro["punitive_flag"]`).
- `data/gold_macro_factors.yaml` — slow structural inputs + weights/thresholds (`punitive_above_pct: 3.5`);
  refresh quarterly.
- `tests/test_gold_trend.py` — 44 unit tests (2 network-marked), incl. band boundaries and fail-open forced-
  failure tests for all four fetchers.
- `scheduler/crontab.example` — Sunday 21:00 KST line.
- `CLAUDE.md` — new "Weekly gold trend" scheduler subsection.
- `.gitignore` — `reports/gold-trend-*.md` (reports can carry personal P&L).
- Spec: `docs/superpowers/specs/2026-07-04-gold-weekly-trend-design.md`; plan:
  `docs/superpowers/plans/2026-07-05-gold-weekly-trend.md`.

**Retrospective.** TDD + per-task review caught real gaps (tautological/boundary tests) early; the most
valuable catch was the whole-branch review flagging that the report wasn't gitignored (personal-data leak
risk) and that the day-one output was a PAUSE that contradicted the tool's own thesis — a threshold-coupling
smell that a purely green test suite would never have surfaced. Running the live dry-run before declaring
done was what exposed it. Open follow-ups: `punitive_above_pct` 3.5 is a seeded guess worth revisiting; the
per-gram position figure is an approximation, not the literal KRX 금현물 price.
