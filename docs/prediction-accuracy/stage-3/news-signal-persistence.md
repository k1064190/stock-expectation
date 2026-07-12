# Stage 3 — Raw NewsSignal persistence + per-tag performance

## Why

The scalar news score is graded near-dead (21-33% win), but the richer
`NewsSignal` fields it collapses from — event tags (earnings/ma/analyst/…),
recency-weighted sentiment, hard positive/negative catalysts — were computed at
prediction time and then thrown away. Without persisting them, nothing can ever
learn *which* news catalysts actually predict. Every day delayed loses labeled
training rows for the Stage 4 learned blend and beyond.

## What

- `NewsSignal.to_components_dict()` — compact JSON persisted under
  `components.news_signal` on every prediction.
- API mode: `_augment_news_signal` in `log_predictions` fetches per-ticker news
  and stores the signal (LIVE only, fail-open, never overwrites a
  model-supplied value). CLI-driven modes (claude-code/codex): the `/expect`
  skill and both briefing prompts now mandate `news_signal` in `--components`.
- `get_news_tag_performance` + `stock-cli news-tag-performance` — per-event-tag
  and per-catalyst hit rates over closed predictions, min-N guard (default 8).
- `get_component_contribution` now skips nested dict/list values so the new
  payload can't pollute pillar buckets.

## How

TDD (6 new tests first). The augment helper mirrors the existing
`_augment_gate_components` pattern; `_news_item_obj` bridges dict fixtures to
the attribute-access shape `summarize_news` expects.

## Code locations

- `mcp-market-data/news_features.py` — `NewsSignal.to_components_dict`
- `scheduler/daily_briefing.py` — `_augment_news_signal`, `_news_item_obj`,
  prompt contract lines (US/KR)
- `mcp-prediction-store/metrics.py` — `get_news_tag_performance`, nested skip
- `stock_cli.py` — `news-tag-performance` subcommand
- `.claude/skills/expect/SKILL.md` — components contract
- `tests/test_news_signal_persistence.py`, `scheduler/tests/test_daily_briefing.py`

## Verification

- `uv run pytest -m "not network"` — 904 passed (6 new)
- CLI smoke: `stock-cli news-tag-performance` on an empty DB returns the
  zero-state JSON shape

## Review loop

- **code-reviewer subagent**: ready to ship; 1 warning (`--min-count` lacks
  validation — dismissed: matches the existing `component-contribution`
  convention), 2 test-coverage suggestions (guard tests + multi-tag/both-
  catalyst tests — added).
- **Codex (gpt-5.6-sol, high)**: 2 medium + 1 low, all fixed — contract wording
  ("verbatim" vs drop raw_count) rewritten as an explicit six-key copy and the
  `daily-briefing` skill example updated to include `news_signal`; tag
  aggregation now type-checks (`list[str]`), dedups per row, and requires
  catalyst flags `is True`; `to_components_dict` nullifies non-finite floats
  (tested with `allow_nan=False`).
- **Antigravity (Gemini 3.1 Pro High)**: 2 low, both fixed — string
  `event_tags` guard (superseded by the stricter Codex fix) and UTC-anchored
  `asof_date` in `_augment_news_signal`.

## Retrospective

The measurement side (tag performance) will stay underpowered for weeks —
the readout deliberately refuses verdicts below min-N rather than reporting
noise.
