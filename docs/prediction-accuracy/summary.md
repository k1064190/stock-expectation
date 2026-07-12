# prediction-accuracy — Stage Index

4-track initiative to raise prediction precision after the May→July win-rate
collapse (52.8% → 3.8%): gate hardening, signal re-weighting, news feature
persistence, learned-blend confidence, per-stock claude -p deep dives, and a
keyword-bucket audit loop.

## Stage 1 — Store-time overextension gate refresh

- [overextension-gate-refresh](stage-1/overextension-gate-refresh.md) — LIVE BULL
  `predict create` now recomputes missing `overextension`/`return_1m` from fresh
  bars so the R2 store gate cannot be bypassed by omitting `--components`;
  fail-open, `--no-gate-refresh` escape hatch, 14 tests.
