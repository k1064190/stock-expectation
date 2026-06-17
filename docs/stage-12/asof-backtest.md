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

## Empirical findings (US, live yfinance)

| run | cohort | n | hit-rate | avg-ret | payoff |
|---|---|---|---|---|---|
| **1M**, Sep'25–May'26, step 14 | presurge | 344 | 64.2% | +0.4% | 0.70 |
| | momentum | 71 | **70.0%** | +1.5% | 0.76 |
| **1W**, Sep'25–Jun'26, step 7 | presurge | 775 | **72.6%** | +0.3% | 0.66 |
| | momentum | 170 | 63.7% | +0.0% | 0.74 |

- **1W**: pre-surge beats momentum by **+8.9pp**; the momentum **>40% parabolic bucket is the
  worst (54.5%, n=23)** — reproducing the investigation's "parabolic chases lose". But the strict
  gate still **FAILS** (95% CI = [-1.0, +18.3]pp; lower bound just below 0).
- **1M** (a strong-bull window): momentum was slightly **better** (70% vs 64%); its >40%/20-40%
  buckets hit 100%/67%. Chasing works in a sustained uptrend.
- **Conclusion**: the pre-surge edge is **real but short-horizon and regime-dependent**, and **not
  statistically conclusive** over these windows. The harness correctly refuses to certify a blanket
  "pre-surge beats momentum" claim.

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

## Retrospective

- What went well: building the gate first paid off immediately — it turned an assumed win into a
  measured, nuanced result and prevented shipping an over-claimed change. The pure as-of scorer from
  WT-A.1 replayed cleanly with zero look-ahead.
- Carry forward: the fixed +3%/-5% exit is non-discriminating in a bull (everything hits +3%); a
  follow-up could parameterize target/stop (or use each pick's natural R:R) and run a KR pass + a
  regime-split (trend vs chop) to locate exactly where pre-surge earns its keep.
