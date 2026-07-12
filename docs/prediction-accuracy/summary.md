# prediction-accuracy — Stage Index

4-track initiative to raise prediction precision after the May→July win-rate
collapse (52.8% → 3.8%): gate hardening, signal re-weighting, news feature
persistence, learned-blend confidence, per-stock claude -p deep dives, and a
keyword-bucket audit loop.

## Stage 5 — Per-stock claude -p deep-dive fan-out

- [deep-dive-fanout](stage-5/deep-dive-fanout.md) — independent Bull/Bear/Judge
  `claude -p` dive per scanner pick (bounded fan-out, strict-JSON, fail-open),
  injected into the briefing prompt behind `--deep-dive` (default off); dive
  stored in `components.deep_dive`, never logs predictions itself.

## Stage 4a — Learned blend: offline model + validation harness

- [learned-blend-offline](stage-4a/learned-blend-offline.md) — hand-rolled L2
  logistic regression over components pillars + leak-free walk-forward CV vs
  raw/isotonic baselines; `stock-cli blend-eval`; weekly-report section. Real
  data verdict: blend loses (Brier 0.233 vs isotonic 0.123) → not wired live.

## Stage 3 — Raw NewsSignal persistence + per-tag performance

- [news-signal-persistence](stage-3/news-signal-persistence.md) — full NewsSignal
  (event tags, sentiment, catalysts) persisted under `components.news_signal` on
  every LIVE prediction (API augment + skill contract); new
  `stock-cli news-tag-performance` readout with min-N guard.

## Stage 2 — Dead-signal down-weight + low-edge-band tagging

- [signal-downweight-edge-band](stage-2/signal-downweight-edge-band.md) — /expect
  ALGO ceiling 8.0→7.0 (momentum/volume down-weighted per 30d calibration; BUY now
  needs news/LLM confirmation); LIVE BULL 0.60-0.70 raw-conf tagged
  `low_edge_band` and skipped by the paper book (logged for training).

## Stage 1 — Store-time overextension gate refresh

- [overextension-gate-refresh](stage-1/overextension-gate-refresh.md) — LIVE BULL
  `predict create` now recomputes missing `overextension`/`return_1m` from fresh
  bars so the R2 store gate cannot be bypassed by omitting `--components`;
  fail-open, `--no-gate-refresh` escape hatch, 14 tests.
