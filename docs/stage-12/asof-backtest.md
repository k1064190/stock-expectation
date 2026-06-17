# Stage 12 — As-of backtest / ship gate (WT-A.3)

## Why

The plan's premise — "surface candidates before they rise and accuracy improves" — is a
hypothesis, and Doctor Cho's top priority is **performance**. Per the build-harness-first
decision, nothing in the A-chain reaches cron until an as-of backtest shows the pre-surge cohort
actually beats the momentum cohort. This worktree is that gate.

## What

A point-in-time A/B harness that, for a series of historical as-of dates, reconstructs **both**
candidate streams (legacy momentum vs new pre-surge), simulates a BULL long entry at each pick's
as-of close under the **same exit rules as the live `outcome_tracker`** (MISS-before-HIT on close,
default +3%/-5%, EXPIRED at the timeframe's trading-day count), forward-evaluates each pick on its
actual subsequent bars, and reports hit-rate / return / payoff **per discovery_source and per
trailing-20d bucket**. Ship gate = bootstrap 95% CI lower-bound > 0 on the
(pre-surge − momentum) hit-rate delta.

## How

- `scheduler/asof_discovery.py` — pure as-of versions of both streams operating on pre-sliced bars
  (no I/O, no `datetime.now`), reusing `candidate_discovery._pct_return/_vol_ratio` and
  `pre_surge_discovery.score_presurge_setups`.
- `scheduler/asof_backtest.py` — over-fetches a long window once, slices to bars ≤ as-of (since
  `get_price_history` has no `end=`), forward-simulates with a **censoring guard** (skip picks
  without a full forward horizon — no look-ahead, no truncation bias), aggregates, runs the
  bootstrap delta CI, and prints a table or JSON. CLI `--market --start --end --step-days
  --horizon --top-n --min-score --fetch-days`.
- 13 offline tests (`scheduler/tests/test_asof_backtest.py`): slicing, simulator HIT/MISS/EXPIRED +
  censoring, bucket boundaries, the gate pass/fail, and the as-of discovery functions.

## Empirical findings (US, live yfinance) — EXPIRED-aware (post-review)

The ship gate counts EXPIRED (target/stop never touched within the horizon) as a **non-hit**, so a
cohort that mostly *sits* (dead money) can't look good on a HIT/(HIT+MISS)-only rate. (This was a
gemini-review P0 fix — see Review loop below; it changed the conclusion.)

Gate = block bootstrap by as-of date (codex fix); momentum volume-arm now excludes down-movers (codex fix).

| run | cohort | n | hit_rate (HIT/(HIT+MISS)) | hit_all (EXPIRED=0) | expired | gate |
|---|---|---|---|---|---|---|
| **1W**, Sep'25–Jun'26 | presurge | 775 | 72.6% | **29.0%** | 465 (60%) | |
| | momentum | 104 | 60.3% | **45.2%** | 26 (25%) | **FAIL −16.2pp** CI[−30.9,−3.0] |
| **1M**, Sep'25–May'26 | presurge | 344 | 67.2% | 59.6% | 39 (11%) | |
| | momentum | 65 | 64.5% | 61.5% | 3 | **FAIL −1.9pp** CI[−14.8,+10.6] |

- **1W is the wrong horizon for pre-surge**: 60% of pre-surge picks **expire dead** (a base/pullback
  doesn't move +3% in a week), so capital-efficiency-adjusted it is **conclusively worse** (−16.2pp,
  whole CI below 0).
- **1M**: pre-surge expiry collapses to 11% and the cohorts are **statistically tied** (−1.9pp, CI
  spans 0). The momentum **>40% parabolic bucket** remains weak (54.5% at 1W).
- **Conclusion**: on this data pre-surge **does not beat momentum at any horizon** once dead-money is
  counted; it is *competitive at 1M+* and *dead-money at 1W*. The actionable result: **log pre-surge
  picks at 1M+**, and the genuine, independently-justified win is the **store-level overextension
  gate** (kills the parabolic tail) — not a momentum replacement. The earlier "1W +8.9pp" was an
  artifact of excluding EXPIRED.

## Implication for WT-A.2 (reshapes it)

1. **Do NOT replace momentum** — confirmed; the blended funnel (capped momentum + additive
   pre-surge) is the right design.
2. **The store-level overextension gate is the highest-value, independently-justified change**: it
   blocks exactly the parabolic blow-off tail (RSI>75 ≈ 26% hit in the investigation; >40% bucket
   54.5% here) without discarding the working momentum core. Ship it regardless of the gate.
3. **Pre-surge ships as an additive, cohort-tagged diversification stream** for ongoing forward
   measurement (1W delta +8.9pp is promising), **not** as a proven momentum-beater, and **not**
   auto-promoted to cron on faith.

## Code locations

- `scheduler/asof_backtest.py`, `scheduler/asof_discovery.py`, `scheduler/tests/test_asof_backtest.py`.

## Review loop

- **code-reviewer-pro**: examined look-ahead, MISS-before-HIT, bootstrap indexing — no bugs; noted
  the EXPIRED denominator was "documented, acceptable".
- **gemini -m pro (P0, actioned)**: EXPIRED outcomes were excluded from the gate arrays — biasing the
  delta toward whichever cohort expires more. Fixed by counting EXPIRED as 0 in the gate and adding
  `hit_rate_all` + an `expired` column to the report. **Re-running flipped the verdict** from
  "+8.9pp promising" to "−13.3pp conclusively worse at 1W" — the highest-value review catch of the
  whole effort.
- **codex -m gpt-5.5 (2 P1, actioned)**: (a) momentum volume-arm admitted down-movers as BULL longs
  (`vr>=2` ignored `ret5`) — biased momentum down / pre-surge up; fixed by requiring `ret5>=0` on the
  volume arm. (b) naive per-pick bootstrap ignored as-of clustering (CI too narrow, could false-pass)
  — replaced with a block bootstrap resampling whole as-of dates. Both re-validated; the verdict held
  and the CI is now honestly wider.
- Outcome: gemini P0 + 2 codex P1 fixed & re-validated; code-reviewer suggestions (doc clarity) folded
  into docstrings.

## Retrospective

- What went well: building the gate first paid off immediately — it turned an assumed win into a
  measured, nuanced result and prevented shipping an over-claimed change. The pure as-of scorer from
  WT-A.1 replayed cleanly with zero look-ahead.
- Carry forward: the fixed +3%/-5% exit is non-discriminating in a bull (everything hits +3%); a
  follow-up could parameterize target/stop (or use each pick's natural R:R) and run a KR pass + a
  regime-split (trend vs chop) to locate exactly where pre-surge earns its keep.
