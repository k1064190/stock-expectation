---
name: prediction-review
description: Review open predictions, check track record accuracy, and analyze prediction calibration. Shows which predictions are approaching targets or stops, displays win rate and Brier score, and suggests calibration adjustments. Triggers on keywords like prediction review, track record, how am I doing, accuracy, 예측 확인, 적중률, check predictions, my predictions, performance.
---

# Prediction Review

Review the current state of all predictions, analyze track record accuracy, and identify calibration improvements.

## When to Use

- To check how open predictions are performing
- To review overall accuracy and track record
- To identify if confidence calibration needs adjustment
- Weekly review of prediction performance

## Prerequisites

- `bin/stock-cli` must be executable (uses uv-managed environment)

## Workflow

All data access goes through `bin/stock-cli` via Bash.

### 1. Fetch Open Predictions

```bash
bin/stock-cli predict list --status OPEN --limit 50
```

Parse the JSON and group by market (US / KR).

### 2. Check Current Prices

For each open prediction, fetch the current price:

```bash
bin/stock-cli price <ticker> --market <market> --days 5
```

Calculate P&L vs entry price, and proximity to target and stop levels.

### 3. Flag Actionable Predictions

Categorize open predictions:

**Approaching Target (within 2%):**
```
⬆️ [TICKER] — Entry: $XX → Current: $XX (+X%) — Target: $XX (X% away)
   Consider: take partial profits or tighten stop
```

**Approaching Stop (within 2%):**
```
⬇️ [TICKER] — Entry: $XX → Current: $XX (-X%) — Stop: $XX (X% away)
   Consider: review thesis validity, may need to cut
```

**On Track:**
```
→ [TICKER] — Entry: $XX → Current: $XX (+/-X%) — Thesis intact
```

**Timeframe Expiring (within 2 days):**
```
⏰ [TICKER] — [X] trading days remaining — Current: $XX (+/-X%)
   Will expire as [HIT/MISS/EXPIRED] at current price
```

### 4. Track Record Dashboard

```bash
bin/stock-cli track-record --days 30
bin/stock-cli track-record --days 30 --market US
bin/stock-cli track-record --days 30 --market KR
bin/stock-cli calibration
```

Display:

```markdown
# Prediction Review — [DATE]

## Open Predictions ([count])

### US Market ([count])
[List with current P&L and status flags]

### Korean Market ([count])
[List with current P&L and status flags]

## Track Record (Last 30 Days)

| Metric | Overall | US | KR |
|--------|---------|-----|-----|
| Total Closed | [N] | [N] | [N] |
| Win Rate | [X%] | [X%] | [X%] |
| Average Return | [X%] | [X%] | [X%] |
| Current Streak | [+/-N] | | |
| Brier Score | [X.XXX] | | |

## Calibration Check

| Confidence Range | Predicted | Actual | Count | Status |
|-----------------|-----------|--------|-------|--------|
| 0.50-0.60 | X% | X% | N | [OK/OVER/UNDER] |
| 0.60-0.70 | X% | X% | N | [OK/OVER/UNDER] |
| 0.70-0.80 | X% | X% | N | [OK/OVER/UNDER] |
| 0.80-0.90 | X% | X% | N | [OK/OVER/UNDER] |
| 0.90-1.00 | X% | X% | N | [OK/OVER/UNDER] |

## Signal Performance

| Signal | Win Rate | Count |
|--------|----------|-------|
| [signal] | X% | N |

## Calibration Notes
[If overconfident: "Consider lowering confidence by X% for [signal type] predictions"]
[If underconfident: "Your [signal type] predictions are better than your confidence suggests"]
[If well-calibrated: "Calibration is good — predicted confidence matches actual accuracy"]
```

### 5. Suggest Actions

Based on the review, suggest:
- **Predictions to close early** (thesis invalidated or target nearly hit)
- **Confidence adjustments** (if calibration is off)

To cancel a prediction:
```bash
bin/stock-cli predict cancel <prediction-id>
```

Ask the user before cancelling:
> "Want me to cancel [TICKER]? The thesis appears invalidated because [reason]."

## Interpretation Guide

**Brier Score:**
- 0.00-0.10: Excellent calibration
- 0.10-0.20: Good calibration
- 0.20-0.30: Fair — room for improvement
- 0.30+: Poor — confidence levels need recalibration

**Calibration Status:**
- OK: Actual accuracy within 5% of predicted confidence
- OVER: Actual accuracy > predicted + 5% (underconfident — raise confidence)
- UNDER: Actual accuracy < predicted - 5% (overconfident — lower confidence)
