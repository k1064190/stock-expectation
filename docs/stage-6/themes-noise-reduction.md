# Stage 6 — Active Themes Noise Reduction

## Why

Live cron runs on both markets (PR #12-#13 KR, PR #17 US) exposed the
same noise pattern in the Active Themes block:

**KR (2026-05-13, 02:00 KST)**:
```
- 사상 최고치 [8종목, 15 헤드라인]
- 최고치 마감 [6종목, 9 헤드라인]
- 사상 최고치 마감 [5종목]
- 하루만에 변동성은 지속 [4종목]
- 변동성은 지속 사상최고 [4종목]
- 지속 사상최고 844 [4종목]
```

**US (2026-05-14, 02:00 KST)**:
```
- closer look stock [3종목, 3 헤드라인]: DIA, QQQ, SPY
- look stock slices [3종목, 3 헤드라인]: DIA, QQQ, SPY
- stock slices 500 [3종목, 3 헤드라인]: DIA, QQQ, SPY
- new inflation playbook [3종목, 3 헤드라인]: DIA, QQQ, SPY
```

Two failure modes:

1. **Shared-article echo** — General-market commentary ("Market Brief", KOSPI
   index articles) gets attached to multiple anchor tickers' news feeds. The
   *same article URL* appearing in 3 different ticker buckets makes its
   n-grams look like a 3-ticker cluster, but the underlying signal is one
   article echoed, not three independent observations.

2. **Sliding-window of one article** — Trigrams "하루만에 변동성은 지속" /
   "변동성은 지속 사상최고" / "지속 사상최고 844" are three different
   contiguous slices of *one* headline. Codex C1's subsequence dedup (PR
   #13) catches strict substring relations like ("ai",) ⊂ ("피지컬", "ai")
   but not sliding windows that don't share tokens at the edges.

The same article URL signature unifies both patterns: when a cluster is
backed by **a single underlying article**, every n-gram derived from it
should be one observation, not N.

## What

- `cluster_news` gains a `min_distinct_articles: int = 2` parameter.
- A parallel `ngram_articles: dict[ngram, set[article_id]]` tracks
  distinct article identifiers per n-gram. Identifier is
  `item["url"]` if present, else the headline string (so test fixtures
  with empty URLs still work as long as headlines differ).
- First-pass filter now requires BOTH:
  - `len(distinct_tickers) >= min_cluster_size` (existing)
  - `len(distinct_articles) >= min_distinct_articles` (new)
- Default 2 distinct articles rejects both noise patterns while letting
  the legitimate 5/13 KR case (피지컬 AI across 3+ distinct articles)
  through unchanged.

## Tests added

| Test | Pattern |
|---|---|
| `test_single_article_echoed_across_tickers_is_rejected` | 1 URL in 3 tickers → rejected; lowering gate to 1 lets it through (proves filter is what's rejecting) |
| `test_sliding_window_trigrams_from_one_article_rejected` | 1 URL, 4 tickers, 3 different trigram windows → all rejected |
| `test_distinct_articles_with_shared_ngram_survive` | 3 different URLs sharing "피지컬 ai" bigram → cluster surfaces (regression for the 5/13 KR pipeline) |

Existing `test_sample_headlines_capped_at_five_and_dedup` fixture also
updated: previous "AI 반도체 호재" echoed across 7 tickers is exactly
the new filter's target, so the test now uses 7 *distinct* headlines
sharing the bigram.

Full suite: **262 passed** (was 261; +1 due to net new tests offset by
the unchanged-but-semantically-different fixture).

## How

Design decisions:

1. **Default `min_distinct_articles=2`** — Two distinct articles is the
   minimum signal that says "multiple sources agree on this theme."
   Three would be stricter but might reject genuine narrative formation
   in early news cycles. Two is conservative without being permissive.
2. **URL preferred, headline string fallback** — Real news items always
   have URLs; the headline-string fallback keeps mocked tests working
   (with `url=""`) so we didn't have to retroactively populate URLs in
   every fixture. Distinct headlines = distinct articles in test mode.
3. **`headline_count` semantic unchanged** — Still counts ticker-bucket
   entries, not distinct articles. The new filter is a gate, not a
   metric replacement. Ranking by `headline_count` still works.
4. **Parallel dict, not bucket restructure** — Could have changed
   `ticker_to_headlines: dict[str, list[str]]` to
   `dict[str, list[tuple[str, str]]]` (headline + article_id pairs)
   but that ripples into `_pick_samples` and `headline_count` callers.
   A parallel `ngram_articles` dict is single-purpose and surgical.

## Code locations

- `scheduler/theme_clusterer.py:246-360` — `cluster_news` signature
  + `ngram_articles` parallel tracking + filter gate
- `scheduler/tests/test_theme_clusterer.py:200-280` — 3 new Stage 6
  tests + 1 fixture update

## Verification

```bash
uv run pytest scheduler/tests/test_theme_clusterer.py -v
# → 20 passed (17 existing + 3 new Stage 6)
uv run pytest -m "not network" -q
# → 262 passed
```

Re-running the US cron post-fix should drop the noisy cluster pattern.
KR cron similarly. (Live verification deferred to post-merge cron — the
test fixtures explicitly encode the exact pre/post live patterns from
both markets.)

## Review loop (per CLAUDE.md mandate)

세 reviewer 병렬 — `code-reviewer-pro` agent, `codex review`, `gemini`.

- **code-reviewer-pro**: 0 Critical, 0 Warning, 1 Suggestion (Day-1 edge case
  docstring) — **APPLIED**. `min_distinct_articles=2` 가 첫날 1-source 테마를
  보류한다는 trade-off 를 docstring 에 명시.
- **gemini**: 0 findings. "Approval: Proceed with merge." 의사결정 5가지 모두
  defensible 평가 (gate 값, URL fallback, parallel-dict, fixture 업데이트,
  headline_count 의미 유지).
- **codex**: 0 findings. "No discrete, actionable bug." 새 article-count gate
  가 targeted test 로 적절히 cover.

총 1건 적용, 0건 거부. 모든 reviewer 가 production-ready 판정.

## Retrospective

- The fixture update revealed a hidden assumption: pre-Stage 6, the
  "AI 반도체 호재" echo test was working because the OLD code accepted
  pure echo as a valid cluster. Stage 6 makes the test's *intent*
  ("multiple tickers share this theme") match its mechanism ("distinct
  articles").
- The parallel-dict approach is minimal-diff but adds one bookkeeping
  data structure. If a Stage 7 ever lifts this further (e.g., weighting
  by domain reputation), refactoring `ngram_index` to hold structured
  tuples might be cleaner.
- The 5/13 KR catalyst test passes unchanged — proves the filter doesn't
  break the production behavior we built Stage A/B around.
