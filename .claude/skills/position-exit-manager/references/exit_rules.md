# Exit Rules Reference

Math and rationale behind `portfolio exit-check` /
`portfolio.exit_manager.compute_exit_actions`.

## Rule precedence (first match wins)

The ladder is ordered **hardest-risk-off first**. The first rule whose condition
is met determines the action; later rules are not consulted.

1. **EXIT** — capital preservation. Any one of:
   - `price < linked stop_price`
   - latest prediction `status == MISS` (thesis invalidated)
   - `price < MA200` (structural trend break)
   - `price < ATR chandelier trailing stop`
2. **TRIM** — bank some profit. Any one of:
   - realized `R:R ≥ tp_rr` (default 2.0)
   - `overextension == EXTREME` AND `pnl_pct > +20%` (blow-off)
   - MA-stack break (`MA20 < MA50`) while still net profitable
3. **ADD** — pyramid into a clean, un-stretched uptrend (all of):
   - `overextension == NONE`
   - `RSI14 ∈ [45, 65]`
   - `MA20 > MA50 > MA200` intact
   - `pnl_pct ∈ [0, +20%]`
   - no contradicting BEAR prediction (a fresh BULL or none)
4. **WATCH** — signals mixed or required inputs missing.
5. **HOLD** — default: above MA200, no trailing-stop breach, R:R below the TP band.

Missing inputs never produce a silent HOLD. If the price is absent, or the
structural backbone (price + MA200) is unavailable after the EXIT/TRIM checks,
the position downgrades to **WATCH**.

## ATR (Average True Range)

True range per bar:

```
TR_t = max( high_t − low_t,
            |high_t − close_{t-1}|,
            |low_t  − close_{t-1}| )
```

ATR uses Wilder smoothing: seed with the simple mean of the first `period` TRs,
then `ATR_t = (ATR_{t-1} × (period−1) + TR_t) / period`. Default `period = 14`.
At least `period + 1` bars are required (the first bar only supplies a previous
close), otherwise ATR is `None` and the trailing-stop rule is skipped.

## Chandelier trailing stop

```
trailing_stop = swing_high − atr_mult × ATR
```

- `swing_high` is the highest **high** over a fixed ~22-bar lookback (about one
  trading month).
- `atr_mult` defaults to 3.0 (Chande's original chandelier exit value). A wider
  multiplier tolerates more noise; a tighter one exits sooner.

A close below this level is treated as a trend-following stop-out (EXIT).

### Approximation caveat — fixed 22-bar lookback

A textbook chandelier exit trails the **since-entry high-watermark**: the highest
high observed since the position was opened, ratcheting up and never down. That
requires each position's entry date plus full bar history per holding.

This skill instead uses a **fixed ~22-bar swing high**. Consequences:

- For positions held **longer than ~22 bars**, the swing high can sit *below* the
  true since-entry peak, so the trailing stop is lower (more lenient) than a real
  chandelier — it will not "remember" an old high that has since rolled off the
  window.
- For very **fresh** positions (held < 22 bars) the window reaches back before
  entry, so the swing high may reflect price action you did not participate in.

This is an intentional simplification: it needs only the recent bar window the
CLI already fetches, stays deterministic and offline-testable, and is good enough
for an *advisory* exit check. Treat the trailing stop as guidance, not a hard
broker order. If precise since-entry trailing is ever required, thread the
position's entry date through and replace `swing_high_22` with the max high since
entry.

## R:R take-profit band

```
effective_stop = max(linked stop_price, ATR trailing stop)
realized_R     = (price − avg_price) / (avg_price − effective_stop)
```

- We TRIM when `realized_R ≥ tp_rr` (default 2.0) — i.e. unrealized gain is at
  least twice the per-share risk implied by the effective stop. 2R is the
  conventional first-scale level: it locks in a favorable expectancy while
  leaving a runner for the rest of the move.
- `effective_stop` takes the **higher** of the linked stop and the ATR trailing
  stop, because once either has moved up, that is the real risk floor.
- When `effective_stop ≥ avg_price` (the stop has ratcheted to or above cost —
  a risk-free / "house money" position), the denominator is non-positive and the
  R:R rule deliberately does **not** fire. There is no fresh risk to take profit
  against, so the position is allowed to run until a structural EXIT trigger.

## Overextension input

`overextension_level` ("NONE" / "ELEVATED" / "EXTREME") comes from
`indicators.classify_overextension` (RSI14 and price-vs-MA20 distance). EXTREME
combined with a >+20% open gain is a blow-off TRIM signal; ELEVATED merely blocks
new ADDs.
