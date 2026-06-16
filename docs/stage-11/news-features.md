# Stage 11 · N1 — Structured news features (latest-news capability)

## Why
The `news` signal graded "dead" (~33% win). Root cause: scoring counted raw
headline volume and a flat sentiment average — reposts inflated volume and a
stale soft blip weighed the same as a hard catalyst. The latest-news pillar
needed *information*, not volume.

## What
- `mcp-market-data/news_features.py`: pure feature extractor producing a
  `NewsSignal` — deduped `unique_count`, **recency-weighted sentiment**
  (3-day half-life exponential decay), catalyst **event_tags**
  (earnings/guidance/ma/analyst/regulatory_legal/product/leadership/capital),
  and `has_positive_catalyst` / `has_negative_catalyst` flags.
- The `news` CLI command now emits a `signal` block alongside `items`.
- Both skills score NEWS_SCORE from `signal` (recency-weighted sentiment,
  deduped volume, negative-catalyst hard cap) and feed `event_tags` into the
  LLM_CONTEXT debate.
- 16 tests in `tests/test_news_features.py`.

## How
- **Recency weight**: `w = 0.5 ** (age_days / 3)` — a week-old item counts ~20%
  of a fresh one; future-dated clamps to age 0; unparseable dates fall back to
  unit weight.
- **Dedup**: word-prefix-containment with a 5-word minimum so wire reposts
  ("…, shares jump") collapse but short generic stubs don't swallow distinct
  stories.
- **Matching**: ASCII keywords match on word boundaries (`\bmiss\b` — never
  "mission"/"twins"/"heartbeat"); **Korean keywords match as substrings**, so KR
  Naver headlines (no Alpha Vantage sentiment) still yield event tags and
  catalyst flags (감자/유상증자/실적/소송 …).

## Validation
Structural/quality improvement — historical per-prediction headlines are not
stored, so this is **not** backfill win-rate validated (stated plainly).
Justification is information-quality: dedup removes double-counting, recency
weighting de-emphasises stale news, catalyst tagging separates material events
from noise. Whether it lifts the news signal's win rate will be measured forward
via weekly calibration. Live checks: US AAPL returned a populated sentiment
signal; KR 005930 deduped 8→7 headlines.

## Review loop (code-reviewer + codex + gemini)
- **Fixed (both, High) — substring catalyst false positives:** "miss"/"halt"/
  "wins"/"beat" could fire the −2 hard cap on "mission"/"health"/"twins"/
  "heartbeat". Now ASCII keywords use word-boundary regex and the lists prefer
  unambiguous phrases ("earnings miss", "trading halt"). Test asserts no false
  fire.
- **Fixed (codex, High) — skill drift:** removed an unvalidated "+1.0 hard
  catalyst" row that existed only in `/expect`; NEWS_SCORE is now identical
  across both skills (sentiment + volume + negative cap; tags feed LLM only).
- **Fixed (both, Medium) — dedup over-merge:** added the 5-word minimum prefix
  guard; test proves "Nvidia shares rise" no longer swallows a distinct longer
  headline.
- **Fixed (both, Medium) — Korean blind spot:** added Korean keywords to every
  event category and the catalyst lists, with substring matching for Hangul.
- **Fixed — test gaps:** word-boundary, Korean, short-prefix, future-date clamp,
  two-item bad-date, old-headline decay.
- **Noted — recency half-life is hardcoded (3d):** exposed as a module constant
  for future tuning.

## Code locations
- `mcp-market-data/news_features.py` — `NewsSignal`, `classify_event`,
  `dedup_headlines`, `_contains`, `summarize_news`, `RECENCY_HALF_LIFE_DAYS`,
  `DEDUP_MIN_PREFIX_WORDS`.
- `stock_cli.py` — `cmd_news` `signal` block.
- `.claude/skills/expect/SKILL.md` (Step 6), `.claude/skills/daily-briefing/SKILL.md` (NEWS_SCORE).
- `tests/test_news_features.py`.

## Retrospective
- The dead news signal was as much a *scoring* problem as a *data* problem:
  dedup + recency + catalyst structure are the information the old volume/avg
  scoring threw away.
- Honest about validation: this one can't be backfilled, so it ships as a
  quality improvement to be confirmed forward — not dressed up as a measured win.
