# External Repo Analysis — Vibe-Trading (HKUDS)

Clone: `/home/cwh/projects/stock-expectation/research/external/Vibe-Trading`
Analyzed: 2026-06-02 (read-only)

## Overview & License

Vibe-Trading is an agentic quant-research platform: an LLM "trader" agent + MCP
server + React frontend, backed by an "Alpha Zoo" (Alpha101 / GTJA191 / Qlib158 /
academic factors), a backtest engine with statistical validation, and a multi-agent
"swarm" runtime. Most of it is far larger in scope than our project (web UI, multi-market
futures/crypto engines, MCP servers, swarm orchestration) and not portable. The genuinely
valuable, portable parts are concentrated in two areas:

1. **Signal/factor quality scoring** (`agent/src/factors/`) — IC/IR computation and the
   `alive / reversed / dead` (+ strict `confirmed_alive / train_only / reversed_strict / noise`)
   classification.
2. **Backtest validation** (`agent/backtest/validation.py`) — Monte-Carlo permutation test,
   bootstrap Sharpe CI, walk-forward consistency, plus PIT/lookahead handling and a
   reproducibility "run card".

**License: MIT** (`LICENSE`). We can **copy code**, not just ideas — with attribution
(retain the MIT copyright notice in any file we lift substantially). One caveat from
`NOTICE`: the factor *formulas* under `zoo/` bundle Apache-2.0 Qlib definitions and
reimplemented paper formulas; we are not porting those, so it does not affect us. The
validation/IC code we care about is original MIT code with no extra encumbrance.

## Architecture & Key Modules

- `agent/src/factors/factor_analysis_core.py` — pure IC math (`compute_ic_series`,
  `compute_group_equity`). No I/O, easy to lift.
- `agent/src/factors/bench_runner.py` — runs IC over a zoo×universe and buckets each
  factor into `alive / reversed / dead` via `categorise()` (`bench_runner.py:46-55`).
- `agent/src/factors/bench_runner_strict.py` — the upgraded gate: same-universe random
  control + train/test OOS split → `categorise_strict()` (`bench_runner_strict.py:242-293`).
- `agent/src/tools/alpha_bench_tool.py` — universe loading + `_compute_forward_returns()`
  (the lookahead-safe alignment, `:476-483`) + survivorship-bias warning (`:330-352`).
- `agent/backtest/validation.py` — Monte-Carlo, bootstrap CI, walk-forward (all self-contained).
- `agent/backtest/metrics.py` — Sharpe/Sortino/Calmar/profit-factor + per-symbol /
  per-exit-reason breakdowns.
- `agent/backtest/run_card.py` — reproducibility card (config/strategy hash, data sources,
  artifacts with sha256).
- `agent/src/swarm/` — multi-agent orchestration (runtime.py, worker.py, store.py). Heavy,
  tied to their MCP/LLM stack; **not portable** to a CLI prediction tracker. Skipped.

## Signal/Factor Methodology — IC/IR & alive/reversed/dead

**IC (Information Coefficient)** — `factor_analysis_core.py:compute_ic_series` (`:8-47`):
per-date cross-sectional **Spearman rank correlation** between factor value and *forward*
return, computed vectorized as Pearson-on-ranks (`rank(axis=1)` then `corrwith`). A date is
dropped if fewer than 5 instruments have both values (`_MIN_VALID_PER_DATE=5`). Output is one
IC value per date.

**IR (Information Ratio)** — computed in the bench runner, not core:
`ir = ic_mean / ic_std` (`bench_runner.py:147`). It's the mean IC normalized by IC volatility
(time-series stability of the signal), the factor-research analog of a Sharpe ratio for a
signal.

**t-stat** — `t_stat(ic_mean, ic_std, n) = ic_mean / (ic_std/sqrt(n))` (`bench_runner.py:39-43`).

**Basic classification** — `bench_runner.categorise()` (`bench_runner.py:46-55`):
- `alive`    : `ic_mean > 0.02 AND ic_positive_ratio >= 0.55 AND |t| > 2`
- `reversed` : `ic_mean < -0.02 AND |t| > 2`  (signal works *inverted*)
- `dead`     : everything else.

**Strict classification (the important upgrade)** — `bench_runner_strict.py`. Its docstring
(`:9-30`) explains the flaw in the basic gate: a raw t-stat vs **zero** accepts factors whose
IC is driven by a shared cross-sectional beta (market/size), and across a large factor zoo
multiple-testing inflates false positives. The fixes:

1. **Same-universe random control** — `_shuffle_within_rows()` (`:99-138`) permutes the factor's
   finite values *within each date row*, preserving the cross-sectional distribution but
   destroying the signal→instrument mapping (a proper null with the same statistical envelope).
   `compute_random_ic_series()` (`:141-174`) averages IC across `n_seeds=5` shuffles.
2. **Paired alpha** — `alpha = signal_IC - random_IC` per date (`alpha_series_paired`, `:177-184`),
   then a one-sample t-stat of that paired series vs zero (`t_stat`, `:187-195`). So significance
   is measured against a *random baseline*, not zero.
3. **OOS train/test split** — split alpha series at `oos_split` date, compute `alpha_t_train`
   and `alpha_t_test` separately (`:469-480`). Boundary is train-inclusive/test-exclusive to
   avoid split-bar leakage.
4. **Four-way verdict** — `categorise_strict()` (`:242-293`):
   - `reversed_strict`  : `alpha_t_full <= -thr`, OR full passes but OOS sign-flips (`alpha_t_test <= -thr`).
   - `confirmed_alive`  : `alpha_t_full >= thr` AND (no OOS OR `alpha_t_test >= thr`).
   - `train_only`       : full passes but OOS decays into the noise band.
   - `noise`            : `|alpha_t|` in `[-thr, thr]`, or `ic_count < min_ic_count (30)`.
   `StrictThresholds` (`:82-93`) defaults `alpha_t_threshold=2.0` but documents raising it to
   **3.5** per Harvey-Liu-Zhu (2016) to correct for multiple testing across a big zoo.

The OOS sign-flip being bucketed as `reversed_strict` rather than `train_only` is a deliberate,
well-reasoned choice (`:264-268`): a factor that inverts out-of-sample is the strongest evidence
the in-sample edge was an artifact.

## Backtest Validation Techniques

All in `agent/backtest/validation.py`, self-contained (numpy/pandas only), wired into the engine
via `run_validation(config, ...)` (`:239-291`) reading a `config["validation"]` block:

- **Monte-Carlo permutation** — `monte_carlo_test()` (`:26-79`): shuffle the *order* of trade
  PnLs `n=1000` times; p-value = fraction of random orderings whose Sharpe ≥ actual. Tests
  whether the equity path is significantly better than random sequencing of the same trades.
  Seeded RNG (`default_rng(seed)`) for reproducibility.
- **Bootstrap Sharpe CI** — `bootstrap_sharpe_ci()` (`:97-143`): resample daily returns with
  replacement `n=1000` times → percentile CI on Sharpe + `prob_positive` (fraction of samples
  with Sharpe > 0). Directly answers "how stable is the risk-adjusted return".
- **Walk-forward** — `walk_forward_analysis()` (`:154-233`): split equity into N non-overlapping
  sequential windows, compute per-window return/Sharpe/maxDD/win-rate, then a **consistency_rate**
  (fraction of profitable windows). This is the lightweight time-stability check, not a re-fitting
  walk-forward optimizer.

**Lookahead / PIT guards:**
- Forward returns use `close.pct_change().shift(-1)` (`alpha_bench_tool.py:482`) so the
  *next-bar* return is aligned to the *current* factor timestamp — IC never correlates a factor
  with a same-bar return.
- The strict OOS split is boundary-safe (train ≤ split < test, `bench_runner_strict.py:469-480`).
- **Survivorship bias is surfaced, not hidden** (`alpha_bench_tool.py:330-352`): the SP500 loader
  logs a warning and forwards a `_meta.survivorship_bias` flag into the bench summary because it
  uses *current* Wikipedia constituents. Honest about a known bias rather than silently biased.
- **Reproducibility** — `run_card.py:write_run_card()` hashes config + strategy source (sha256),
  records data sources and artifact hashes, writes JSON+MD. Good provenance pattern.

## Portable Improvements For Our Project (ranked)

Our project already has the scaffolding: `mcp-prediction-store/metrics.py:get_signal_performance`
computes raw per-signal win-rate (`:233-278`), and `get_calibration_report` (`:169`) +
`brier_score` (`:149-155`) exist. The gaps the known problems describe map cleanly onto what
Vibe-Trading does better.

### 1. Significance-tested per-signal scoring + auto-prune list — **S/M, HIGH impact**
*What:* Upgrade `get_signal_performance` from raw win-rate to a **t-stat / binomial test against
a 50% null** (and optionally a permutation null), and emit a `verdict` per signal:
`alive / reversed / dead` analogous to `categorise()`. Signals like valuation/cycle/mean_reversion
with 0% win rate would be flagged `dead`/`reversed` with statistical backing, giving the weekly
calibration loop an objective prune list.
*Learn from:* `bench_runner.py:39-55` (`t_stat`, `categorise`) and the random-control idea in
`bench_runner_strict.py:99-138` (here it would be a label-shuffle permutation of HIT/MISS to get a
null win-rate distribution).
*Touches:* `mcp-prediction-store/metrics.py` (extend `SignalPerformance` + `get_signal_performance`);
surface in `scheduler/weekly_calibration.py`.
*Why it fits:* Directly attacks known problem (1) — signals are never pruned and have no IC/scoring.
Our per-signal win-rate is just a fraction with no error bar; a binomial/permutation test makes
"0% win rate over N=12" actionable vs "noise from small N".

### 2. Out-of-sample / walk-forward split for signal verdicts — **M, HIGH impact**
*What:* When grading a signal, split closed predictions by `outcome_date` into earlier (train) and
recent (test) halves and require the edge to persist in the recent window before trusting it.
Mirrors `train_only` vs `confirmed_alive`: a signal that worked historically but decayed recently
gets `train_only`, not `alive`.
*Learn from:* `bench_runner_strict.py:469-480` (boundary-safe date split) + `categorise_strict`
(`:242-293`).
*Touches:* `mcp-prediction-store/metrics.py` (new function reusing the split logic);
`scheduler/weekly_calibration.py` (report recency-decayed signals).
*Why it fits:* Directly attacks known problem (2) — win rate degrading toward 50%. A static
all-history win-rate hides exactly this decay; a train/test split exposes which signals are dying.

### 3. Bootstrap CI + Brier on win-rate and calibration — **S, MEDIUM-HIGH impact**
*What:* Add a bootstrap confidence interval around overall and per-bucket win-rate (and around the
Brier score), so the calibration report says "win-rate 0.54 [0.47, 0.61], not distinguishable from
0.50" instead of a bare point estimate. Combined with the existing Brier/calibration buckets, this
makes the near-constant ~0.6 confidence problem visible and quantified.
*Learn from:* `validation.py:bootstrap_sharpe_ci` (`:97-143`) — same resample-with-replacement
pattern, applied to a 0/1 outcome series instead of returns.
*Touches:* `mcp-prediction-store/metrics.py` (`get_track_record`, `get_calibration_report`).
*Why it fits:* Attacks known problems (2) and (3) — gives statistical teeth to "is the model
actually calibrated / better than a coin flip" and to confidence-bucket reliability.

### 4. Confidence recalibration from the calibration curve — **M, HIGH impact**
*What:* We already compute the calibration curve (predicted vs actual per bucket,
`metrics.py:169-230`) but never feed it back. Add a monotonic recalibration map (isotonic or a
simple per-bucket lookup) learned from closed predictions, applied to raw model confidence before
logging. This directly fixes the "confidence ~0.6 and uninformative" problem by stretching
confidences toward observed accuracy.
*Learn from:* Conceptually the strict gate's "measure against a baseline" philosophy; the
calibration-curve scaffolding is already ours — Vibe-Trading mainly validates that this is the
right axis to act on. (No drop-in function to copy here; this is an idea grounded in our existing
`get_calibration_report`.)
*Touches:* `mcp-prediction-store/metrics.py` (recalibration fit), `scheduler/daily_briefing.py` or
`/expect` logging path (apply the map at write time).
*Why it fits:* Directly attacks known problem (3) — near-constant uninformative confidence + no
calibration. This is the single highest-leverage item for confidence quality.

### 5. Permutation test for the prediction track record — **S, MEDIUM impact**
*What:* Add a Monte-Carlo permutation test answering "is our HIT rate significantly better than
randomly assigning HIT/MISS at the same base rate?" — a portfolio-level sanity check run in weekly
calibration.
*Learn from:* `validation.py:monte_carlo_test` (`:26-79`) — adapt from PnL-order shuffling to
outcome-label shuffling.
*Touches:* `mcp-prediction-store/metrics.py` + `scheduler/weekly_calibration.py`.
*Why it fits:* Attacks problem (2); a cheap, honest "are we actually beating random" gate. Lower
rank because per-signal significance (item 1) is more actionable than a single aggregate number.

### 6. Reproducibility run-card for weekly calibration reports — **S, LOW-MEDIUM impact**
*What:* Emit a small JSON "card" alongside each weekly calibration run: DB hash/row-count, date
window, prediction counts, config, sha256 of the report. Makes calibration runs auditable and
diffable over time.
*Learn from:* `run_card.py:write_run_card` (`:24-80`).
*Touches:* `scheduler/weekly_calibration.py`.
*Why it fits:* Operational hygiene; supports trusting the calibration loop's outputs over time.
Lowest rank — useful but doesn't move the four core problems directly.

**Skeptical notes / what NOT to port:**
- The Alpha Zoo factors, swarm runtime (`agent/src/swarm/`), MCP servers, futures/crypto engines,
  and React frontend are out of scope and would be net-negative complexity for a CLI prediction
  tracker.
- IC/`compute_ic_series` itself assumes a **cross-sectional panel** (many tickers ranked per date).
  Our predictions are sparse, irregular, single-ticker events — so we should port the *t-stat /
  random-control / OOS-split methodology* (items 1-2,5) rather than literal IC code. Our analog of
  "IC" is per-signal win-rate vs the 50% null. Be clear-eyed: don't paste `compute_ic_series` and
  expect it to apply to our schema.
- Walk-forward here is a consistency check, not a re-optimizer; that's fine for us — we want the
  cheap stability signal, not parameter re-fitting.

---

## Executive Summary (5 bullets)

1. **License = MIT** → we can copy code (with attribution), not just ideas; the value is in
   `agent/src/factors/` (IC/IR + alive/reversed/dead) and `agent/backtest/validation.py`
   (Monte-Carlo / bootstrap CI / walk-forward). The factor zoo, swarm, and web UI are out of scope.
2. **Significance-tested per-signal verdicts (item 1, S/M, HIGH):** replace raw per-signal win-rate
   in `metrics.py:get_signal_performance` with a t-stat/permutation test vs 50% + an
   `alive/reversed/dead` label (learn from `bench_runner.py:39-55`), giving the weekly loop an
   objective prune list — fixes the "0% win-rate signals never pruned" problem.
3. **Train/test split on signal grading (item 2, M, HIGH):** require a signal's edge to persist in
   recent predictions (learn from `bench_runner_strict.py:469-493`'s OOS split + `train_only` vs
   `confirmed_alive`) — directly exposes the win-rate-decaying-toward-50% problem.
4. **Confidence recalibration + bootstrap CIs (items 3-4, S/M, HIGH):** feed our existing
   calibration curve back into a recalibration map and wrap win-rate/Brier in bootstrap CIs (learn
   from `validation.py:bootstrap_sharpe_ci`) — fixes the near-constant uninformative ~0.6 confidence.
5. **Caveat:** their IC math is cross-sectional (panel of tickers/date); our predictions are sparse
   single-ticker events, so port the *methodology* (random-control null, OOS split, bootstrap,
   permutation) into our SQLite win-rate world — do not paste `compute_ic_series` verbatim.
