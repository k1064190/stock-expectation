# Stage 11 · F1 — LIVE-vs-INTERACTIVE gap audit (no leakage found)

## Why
INTERACTIVE predictions won 66.7% vs LIVE 24.8% — a 42pt gap that, if caused by
look-ahead/leakage, would mean the INTERACTIVE track record (and anything derived
from it) is a mirage. Had to be settled before trusting any cross-source number.

## What — the audit
Compared the two sources on dates, timeframes, holding period, and monthly
win-rate (read-only DB queries):

| | INTERACTIVE | LIVE |
|---|---|---|
| n (closed) | 102 | 318 |
| created_at range | 2026-04-04 … **05-16** | 2026-04-04 … **06-11** |
| win by month | Apr 49/72, May 19/30 | May 55/141 (39%), **Jun 22/175 (12.6%)** |
| median hold (days) | 26 | **2** |
| min hold | 1 | 0 |

## Verdict: regime + selection, NOT leakage
1. **Time survivorship (dominant).** INTERACTIVE stopped on 5/16 — it never saw
   the June crash. LIVE is 55% June predictions, which won 12.6%. Remove the
   crash window and the gap collapses.
2. **Residual selection (smaller).** Same-month (May): INTERACTIVE 63% vs LIVE
   39%. INTERACTIVE are manual deep-dives (higher conviction, fewer names); LIVE
   are daily-briefing breadth. Different selection, not look-ahead.
3. **No backdating.** Every INTERACTIVE prediction resolves forward (min hold 1
   day); none are same-day/retroactive.
4. **The LIVE 2-day median hold** is the real tell: June LIVE longs hit their
   stops almost immediately — the overextension/regime failure already addressed
   by D and E1, not a data artifact.

## Action taken
- Confirmed the contamination risk (INTERACTIVE's inflated calibration leaking
  into LIVE confidence) is **already neutralised**: the recalibration map (A) is
  source-scoped, so a LIVE prediction only ever calibrates against LIVE history.
- The remaining hazard was the **blended headline track-record** the skills read,
  which hid the split inside a misleading ~35%. `track-record` now emits a
  `by_source` breakdown whenever sources are blended, so LIVE (real performance)
  and INTERACTIVE (manual sample) are never conflated again.

## Review loop
Audit + a small additive CLI change (`by_source` in `cmd_track_record`).
Unit tests assert the breakdown appears when blended and is absent when a
`--source` filter is given. No reviewer-flagged issues beyond the audit
reasoning, which is documented here for scrutiny. 403→ tests pass.

## Code locations
- `stock_cli.py` — `cmd_track_record` `by_source` breakdown.
- `tests/test_recalibration_cli.py` — breakdown tests.
- Source-scoping that already quarantines the gap:
  `stock_cli.py:_recalibrated_confidence` (source-scoped recal map, Stage A).

## Retrospective
- The scariest-looking number (42pt gap) turned out benign — but only a dated,
  per-source breakdown proved it. The fix is to make the honest decomposition the
  default view, so nobody quotes the blended figure as "the system's accuracy".
- Carry-forward: when LIVE accumulates a post-recalibration / post-gates sample,
  re-run this comparison on a *common* window to isolate the pure selection effect.
