---
name: catalyst-event-gate
description: "Unified forward catalyst timeline + deterministic R3 event-risk gate. Merges the FMP earnings calendar (US-listed) and economic calendar (FOMC/CPI/NFP) into one timeline, then turns imminent binary events into label caps and confidence trims so /expect and daily-briefing don't issue fresh BUY calls right into an earnings print or a Fed decision. Triggers: catalyst, event risk, earnings risk, event gate, upcoming earnings, FOMC, CPI, NFP, macro event, 이벤트 리스크, 실적 발표 임박, 캐털리스트."
---

# Catalyst Event Gate — Forward Timeline + R3 Event-Risk Gate

Two products from two FMP forward calendars:

1. **Merged timeline** — every forward earnings report (per ticker) and every
   high-impact macro release (FOMC / CPI / NFP) in one normalized, sorted view.
2. **R3 gate** — a deterministic verdict that caps the label or trims confidence
   when a binary event is too close to safely hold a directional thesis through.

It is a **risk gate, not a signal**: R3 never raises the BUY bar and never
issues a BUY — it only caps a would-be BUY to WATCH or shaves confidence.

## Why this exists

A strong technical + news + macro setup can still be a bad *entry* if the next
trading day is an earnings print or a Fed decision: the outcome is a coin-flip
gap, not a thesis. R1 (regime) and R2 (overextension) cannot see dated binary
events. R3 closes that gap.

## CLI

```bash
# Merged timeline (earnings only unless --include-macro)
bin/stock-cli catalyst timeline NVDA,AMD --market US --days 14
bin/stock-cli catalyst timeline NVDA,AMD --market US --days 14 --include-macro
bin/stock-cli catalyst timeline 005930   --market KR --days 14 --include-macro

# R3 gate verdict (per-ticker cap/trim + market macro_trim)
bin/stock-cli catalyst gate NVDA,AMD --market US
bin/stock-cli catalyst gate 005930   --market KR   # macro_trim only, no earnings cap
```

All output is JSON. Requires `FMP_API_KEY`. **No key or any fetch error → the
command still exits 0** with `gate_unavailable: true` and neutral (zero) caps —
it never breaks the cron (FAIL-OPEN).

## Merged timeline structure

```jsonc
{
  "asof": "2026-06-17",
  "market": "US",
  "by_ticker": {
    "NVDA": [
      {
        "ticker": "NVDA", "market": "US", "kind": "earnings",
        "name": "NVIDIA Corp", "event_date": "2026-06-18",
        "timing": "AMC", "impact": "High",
        "trading_days_until": 1, "source": "fmp:earning_calendar"
      }
    ],
    "AMD": []
  },
  "market_wide": [          // only present with --include-macro
    {
      "ticker": null, "market": "GLOBAL", "kind": "macro",
      "name": "FOMC Interest Rate Decision", "event_date": "2026-06-18",
      "timing": null, "impact": "High",
      "trading_days_until": 1, "source": "fmp:economic_calendar"
    }
  ]
}
```

- `by_ticker[SYM]` — that ticker's forward earnings events, nearest-first.
- `market_wide` — high-impact macro events (GLOBAL — they transmit across
  markets via FX / index futures), nearest-first.
- `trading_days_until` — business-day (Mon-Fri) distance; **0 = today**.
  Holidays are *not* excluded (pure stdlib), which only ever makes an event look
  ~1 day farther away — the safe direction for a risk gate.

## R3 gate rule table

`bin/stock-cli catalyst gate` returns:

```jsonc
{
  "asof": "2026-06-17", "market": "US",
  "by_ticker": {
    "NVDA": {"cap_label": "WATCH", "confidence_trim": 0.0,
             "next_earnings_date": "2026-06-18", "trading_days_until": 1}
  },
  "macro_trim": 0.05,
  "macro_events": [{"name": "FOMC ...", "event_date": "2026-06-18",
                    "trading_days_until": 1, "impact": "High"}],
  "gate_unavailable": false, "notes": []
}
```

### Earnings (per-ticker, US only)

| Trading days to next report | R3 action | Field |
|---|---|---|
| `td <= 2` (`EARNINGS_WATCH_DAYS`) | **cap label → WATCH** (binary risk) | `cap_label="WATCH"` |
| `2 < td <= 5` (`EARNINGS_TRIM_DAYS`) | trim confidence **0.05** | `confidence_trim=0.05` |
| `td > 5` | none | both zero |

### Macro (market-wide, US + KR)

| Condition | R3 action | Field |
|---|---|---|
| Any High-impact FOMC / CPI / NFP within `td <= 2` (`MACRO_TRIM_DAYS`) | trim **0.05** on **every** pick in that market | `macro_trim=0.05` |
| else | none | `macro_trim=0.0` |

Thresholds live in `mcp-market-data/events.py` as module constants.

## KR vs US coverage matrix

| | Earnings (per-ticker) | Macro (market-wide) |
|---|---|---|
| **US** | ✅ FMP `/earning_calendar` (US-listed) | ✅ FMP `/economic_calendar` |
| **KR** | ❌ no forward KR EPS feed on FMP | ✅ **consumes the US macro stream** (transmits via FX / SOXL) |

**KR is macro-only.** FMP's `/earning_calendar` is US-listed only and there is
no forward KR EPS feed, so KR tickers never receive a per-ticker earnings cap —
their `cap_label`/`next_earnings_date` are always null. KR still receives the
US `macro_trim`, because a Fed decision or US CPI moves KOSPI/KOSDAQ through the
won and the semiconductor complex (SOXL/SOXX). The earnings fetch is skipped
entirely for KR to protect the FMP quota.

## R1 + R2 + R3 stacking

R3 composes with the existing /expect gates (see `expect/SKILL.md` Step 7):

- **WATCH cap wins.** A WATCH from R1 (RISK_OFF), R2 (EXTREME overextension), or
  R3 (earnings `td <= 2`) caps the label at WATCH — no BULL logged, full stop.
- **Confidence caps/trims take the minimum / stack down.** R1/R2 caps
  (0.60), the R3 earnings trim (−0.05), and the R3 macro trim (−0.05) all pull
  confidence *down*; the effective confidence is the lowest survivor. A pick can
  eat both R3 trims (own earnings near + macro near) at once.
- **R3 never raises the BUY bar.** Only R1 NEUTRAL (+1.0) and R2 ELEVATED (+1.0)
  raise the COMPOSITE bar. R3 caps or trims; it does not move the threshold.

## Quota note

`catalyst gate` and `catalyst timeline` each fetch the earnings + macro windows
**once per call** (not once per ticker). Pass all candidate tickers in a single
comma-separated call. FMP free tier is 250 calls/day.

## Implementation

- Core + fetchers: `mcp-market-data/events.py`
  (`CatalystEvent`, `EventGate`, `trading_days_between`, `build_timeline`,
  `evaluate_gate`).
- CLI: `stock_cli.py` (`cmd_catalyst_timeline`, `cmd_catalyst_gate`,
  `catalyst` subparser).
- Tests (offline, stubbed fetchers): `mcp-market-data/tests/test_events.py`.
