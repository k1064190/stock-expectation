---
name: position-exit-manager
description: Decide when to exit, trim, add to, or hold each current holding. Layers an ATR chandelier trailing stop, R:R take-profit, and linked-prediction thesis invalidation on top of the portfolio. Use when the user asks to check positions, where to set stops, when to take profit, or whether to sell. Triggers on "포지션 점검", "손절", "익절", "트레일링 스탑", "언제 팔아", "exit check", "trim", "take profit", "when to sell", "stop loss", "trailing stop".
---

# Position Exit Manager

Advisory exit-decision layer for existing holdings. For each position it emits
one of five actions — **EXIT / TRIM / ADD / WATCH / HOLD** — by combining an ATR
chandelier trailing stop, an R:R take-profit band, MA-stack structure, and the
holding's latest linked prediction (thesis).

This skill is **advisory only**: it never sells, never edits predictions. It
tells you what to consider; you decide and record any trade via
`portfolio buy/sell`.

## When to Use

- "포지션 점검해줘", "지금 손절해야 돼?", "익절 타이밍이야?", "트레일링 스탑 어디?"
- "Should I sell X?", "where's my stop?", "is it time to take profit?"
- After a daily-briefing portfolio review, to get per-position exit guidance.
- Whenever a holding's prediction has resolved and you want a structural check.

## Prerequisites

- A portfolio must exist with positions (see `portfolio-eval` to set one up).
- Network access for live prices (US: yfinance, KR: pykrx). Provider failures
  degrade gracefully — the affected ticker downgrades to **WATCH**, never a
  silent HOLD.

## Workflow

### Step 1: Run the exit check

```bash
./bin/stock-cli portfolio exit-check --market {US|KR}
# Optional tuning:
./bin/stock-cli portfolio exit-check --market US --atr-mult 3.0 --tp-rr 2.0
```

The CLI fetches ~300 bars per holding, computes horizon metrics + ATR(14) + the
~22-bar swing high, looks up the holding's predictions, and prints JSON with one
entry per position.

### Step 2: Read the decision table

For each position the JSON carries `action`, `triggered_rules`, `atr`,
`trailing_stop`, `ma_status`, `overextension_level`, `pnl_pct`, and the
`linked_prediction` ({id, direction, status, target, stop, rr_progress}).

| Action | Meaning | Typical triggers |
| --- | --- | --- |
| **EXIT** | Hard risk-off — get out | price < linked stop, prediction MISS, close below MA200, or price < ATR chandelier trailing stop |
| **TRIM** | Take partial profit | realized R:R ≥ `tp-rr` (default 2.0), EXTREME overextension with >+20% gain, or MA20<MA50 stack break while still profitable |
| **ADD** | Pyramid into strength | overextension NONE, RSI 45–65, MA20>MA50>MA200 intact, gain in [0,+20%], no contradicting BEAR prediction |
| **WATCH** | Mixed / insufficient data | missing price or metrics, or signals that don't resolve cleanly |
| **HOLD** | Trend intact, nothing fired | above MA200, no trailing-stop breach, R:R below the TP band |

Precedence is **hardest-risk-off first**: EXIT > TRIM > ADD > WATCH > HOLD. The
first rule that matches wins, so an EXIT trigger always overrides a coincident
TRIM/ADD.

### Step 3: Synthesize a Korean-first review

Present per position, Korean-first:

1. **결론 (action)** with the Korean hint (`reason_ko_hint`) expanded into a full
   sentence — e.g. "EXIT: MA200 이탈로 추세 손상, 청산 검토".
2. **근거** — translate `triggered_rules` into plain Korean, citing the numbers
   (현재가, 트레일링 스탑, R:R 진행률, 손절선).
3. **수익률** (`pnl_pct`) and the linked thesis status if present.
4. **다음 행동** — concrete next step (전량 청산 / 일부 익절 / 추가매수 / 관망 / 보유).

For WATCH entries, state explicitly what data was missing so the user knows it
is a data gap, not a recommendation to do nothing.

## Notes on the math

- **ATR chandelier trailing stop** = `swing_high(~22 bars) − atr_mult × ATR(14)`.
  ATR uses Wilder true-range smoothing. `atr_mult` defaults to 3.0.
- **Effective stop** for the R:R take-profit = `max(linked stop, ATR trailing
  stop)`. Realized R = `(price − avg) / (avg − effective_stop)`. When the
  trailing stop has ratcheted above cost (a risk-free position) the R:R rule
  stops firing by design — you let the winner run.
- **Linking**: a holding links to its latest-by-`created_at` OPEN prediction.
  The MISS-invalidation EXIT also inspects the latest prediction overall.
  "No linked prediction" is a non-blocking skip — structural rules still run.

See `references/exit_rules.md` for the full derivation, the R:R band rationale,
and the fixed-lookback approximation caveat.

## Resources

- `references/exit_rules.md`: ATR/chandelier math, R:R band rationale, the
  fixed-22-bar swing-high approximation and its limits.
