# Paper-trading review — 2026-06-23

_Simulated long-only book trading the logged LIVE BULL predictions. Advisory — apply tuning via PR. See `paper_trading/` and `docs/superpowers/specs/2026-06-23-paper-trading-design.md`._

## US book (USD)

- Period: **2026-04-06 → 2026-06-22** (54 trading days)
- Final NAV: **$100,647** (initial $100,000)
- Cumulative return: **+0.65%**
- Benchmark (passive index) return: **+13.37%**
- Sharpe (annualized): **0.27**
- Max drawdown: **-6.94%**
- Realized round-trips: **46** | win rate **43%** | total P&L **$152**
- Open positions: **9**

### Realized P&L by exit reason

| Exit reason | n | win rate | total P&L |
|---|---|---|---|
| stop_hit | 21 | 0% | $-5,380 |
| target_hit | 13 | 100% | $4,651 |
| horizon_exit | 12 | 58% | $882 |

### Realized P&L by prediction confidence

| Confidence | n | win rate | avg return | total P&L |
|---|---|---|---|---|
| 0.60-0.70 | 46 | 43% | +1.0% | $152 |

### Recommendations (advisory)

- Lagging the passive benchmark by 12.7%. Fixed target exits cap upside in a trending tape — consider an ATR trailing stop (see `portfolio/exit_manager.py`) instead of fixed take-profits.
- Stop-outs (21) exceed target hits (13) — entries may be chased or stops set too tight; raise the BUY confidence floor or widen stops vs ATR.
- Realized win rate 43% is below a coin flip — the long signal is not converting to P&L; prioritize signal quality over position sizing.
- Transaction frictions consumed 408 (0.41% of initial capital) — a real drag the live system also pays.

## KR book (KRW)

- Period: **2026-04-06 → 2026-06-22** (52 trading days)
- Final NAV: **₩99,350,623** (initial ₩100,000,000)
- Cumulative return: **-0.65%**
- Benchmark (passive index) return: **+81.47%**
- Sharpe (annualized): **0.10**
- Max drawdown: **-11.29%**
- Realized round-trips: **62** | win rate **39%** | total P&L **₩-976,480**
- Open positions: **8**

### Realized P&L by exit reason

| Exit reason | n | win rate | total P&L |
|---|---|---|---|
| stop_hit | 36 | 0% | ₩-34,690,600 |
| target_hit | 24 | 100% | ₩34,991,997 |
| horizon_exit | 2 | 0% | ₩-1,277,877 |

### Realized P&L by prediction confidence

| Confidence | n | win rate | avg return | total P&L |
|---|---|---|---|---|
| 0.60-0.70 | 62 | 39% | +1.4% | ₩-976,480 |

### Recommendations (advisory)

- Lagging the passive benchmark by 82.1%. Fixed target exits cap upside in a trending tape — consider an ATR trailing stop (see `portfolio/exit_manager.py`) instead of fixed take-profits.
- Stop-outs (36) exceed target hits (24) — entries may be chased or stops set too tight; raise the BUY confidence floor or widen stops vs ATR.
- Realized win rate 39% is below a coin flip — the long signal is not converting to P&L; prioritize signal quality over position sizing.
- Transaction frictions consumed 2,016,938 (2.02% of initial capital) — a real drag the live system also pays.
