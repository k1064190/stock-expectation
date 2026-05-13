# Stage 2 — News-Cluster Theme Auto-Extraction (KR)

## Why

Stage 1 동적 후보 발굴만으로는 LLM이 "왜 이 종목들이 한꺼번에 움직이나?"
라는 narrative를 못 만든다. 5/13 사례에서도 현대오토에버/LG전자/현대차가
별개 종목으로 prompt에 나열되면 LLM은 각각 따로 분석할 가능성이 크다 —
실제 카탈리스트인 "KB 자산운용 RISE 현대차고정피지컬AI ETF 상장" + "광주
자율주행 200대 실증" 같은 *공유 narrative* 가 시야 밖.

Stage 2 (= 플랜 Stage B) 는 후보 종목들의 최근 7일 뉴스 헤드라인을 병렬로
긁어 cross-ticker n-gram 클러스터링으로 *이름 있는 테마*를 추출해 prompt
의 새 "Active Themes" 섹션에 삽입한다. `briefing_kr.md` 의 반도체 한정
앵커 문구도 일반화해 LLM 이 동적 후보 안에서 자유롭게 thesis를 만들 수
있게 한다.

## What

- 새 모듈 `scheduler/theme_clusterer.py`:
  - `ThemeCluster` dataclass (keywords, tickers, sample_headlines, headline_count).
  - `fetch_news_for_candidates(cands, days=7, max_workers=8, provider=None)` —
    `KoreanMarketProvider.get_news` 를 ThreadPoolExecutor(8) 로 병렬화.
    Provider exception 은 per-ticker swallow.
  - `cluster_news(news_by_ticker, min_cluster_size=3, ngram_sizes=(2,3), max_themes=8)`
    — 헤드라인 lowercase + `[A-Za-z가-힣0-9]+` 토큰화 + 인라인 stopword set 제거
    + bigram/trigram count → ≥3 종목 공유한 n-gram만 유지 → 동일 ticker set 안
    superset 우선 (`("ai",)` vs `("피지컬","ai")` → 후자만 유지).
  - `backfill_news_counts(cands, news_by_ticker)` — Stage 1 의
    `Candidate.news_count_7d=0` 을 실제 count로 교체.
  - `format_themes_for_prompt(clusters)` — 한국어 markdown 블록. 빈 clusters
    리스트도 "≥3 종목 공유 테마 없음" 한 줄로 graceful 처리.
- `scheduler/daily_briefing.py` 변경:
  - `fetch_kr_market_data()` (API 모드) 에 `fetch_news_for_candidates` +
    `cluster_news` + `backfill_news_counts` + `format_themes_for_prompt`
    호출 추가. 각 종목 라인에 `news7d=N` 부착.
  - `build_claude_code_prompt("KR")` (cron 모드) 에 동일 plumbing 추가 +
    `{themes_block}` 자리표시자 inline 주입.
  - Cross-market rule 일반화: `NVDA/SMH → Samsung/SK Hynix` 에서
    "Active Themes 블록 우선" 으로 전환.
- `scheduler/prompts/briefing_kr.md:75` 도 동일 일반화 (API 모드 템플릿).
- 15개 unit test 추가 (5/13 fixture, min_cluster_size, stopword,
  superset preference, fetch failure resilience, dict/dataclass
  normalisation, sort order, empty-input, sample dedup, backfill,
  format empty/non-empty).

## How

설계 결정:

1. **3-gram 우선, unigram 제외** — 단일 토큰 ("ai" 만) 은 너무 많은 무관
   종목에 등장해 거짓 클러스터를 만든다. 2-gram/3-gram 만 사용해 *의미를
   가진 조합* 만 surface. 4-gram 은 너무 specific 해서 3 종목 동시 등장
   드물어 default off.
2. **Superset preference** — 같은 ticker set 을 가진 multiple n-gram 중
   가장 긴 것만 유지. `("ai",)` 과 `("피지컬", "ai")` 이 동일 3 종목을
   가지면 후자만 keep — LLM 이 보기 쉬운 키워드만 남는다.
3. **Pure-stdlib 클러스터링** — sklearn / sentence-transformers / KoNLPy
   금지. 정규식 토큰화 + dict 카운트 + 정렬만 사용. 의도적으로 dumb 한
   설계 — 이게 부족하면 Stage 4 (mem0 semantic search) 가 교체.
4. **stopword 인라인 set** — 외부 사전 파일 없음. `종목, 기업, 시장,
   코스피, 코스닥` 등 KR 헤드라인 공통 noise 와 영어 particle 만. 너무
   넓게 잡으면 진짜 테마 ("AI") 까지 잃기에 보수적으로.
5. **Min length 2** — 단일 음절 한글 토큰 (예: "셀") 제외. 5/13
   카탈리스트의 핵심 토큰 (피지컬, ai, 자율주행, etf, 현대차) 은 모두 ≥2
   character 이므로 손실 없음. 테스트 fixture 작성 시 이 함정 발견 후
   docs 화 (`test_clusters_sorted_by_ticker_count_then_headlines` 코멘트).
6. **Per-ticker exception swallow** — Naver rate-limit 으로 1-2 종목
   fetch 실패해도 나머지 cluster 는 동작. provider 가 이미 per-call
   swallow 패턴이라 thread pool wrapper 가 그 패턴을 그대로 따른다.
7. **News count backfill 은 Stage 1 의 Candidate 를 mutate** — Stage 1
   에서 `Candidate` 를 frozen=False 로 유지한 이유. `news_count_7d` 가
   향후 score re-rank 의 입력이 될 수 있도록.

## Code locations

- `scheduler/theme_clusterer.py` (NEW) — ~270 줄
- `scheduler/tests/test_theme_clusterer.py` (NEW) — 15 tests
- `scheduler/daily_briefing.py:58-64` — import 추가
- `scheduler/daily_briefing.py:111-159` — `fetch_kr_market_data()` 에
  news fetch + cluster + format + per-ticker `news7d=N` 추가
- `scheduler/daily_briefing.py:401-450` — KR `build_claude_code_prompt`
  에 `{themes_block}` 주입 + cross-market rule 일반화
- `scheduler/prompts/briefing_kr.md:75-80` — API 모드 템플릿의
  cross-market rule 일반화

## Verification

```bash
# 1. 신규 단위 테스트
uv run pytest scheduler/tests/test_theme_clusterer.py -v
# → 15 passed

# 2. 전체 회귀 (Stage A + B + 기존)
uv run pytest scheduler/tests/ -v
# → 68 passed

# 3. End-to-end dry-run (mocked candidates + news, 5/13 시나리오)
# verify checks: Active Themes section appears, '피지컬 ai' cluster surfaces
# with members 005380/066570/307950, no Samsung-only anchoring
```

end-to-end dry-run 결과 확인 (1 종목당 1-2 mock 헤드라인 fixture):
```
## Active Themes (지난 7일 뉴스 클러스터링)
- 피지컬 ai [3종목, 3 헤드라인]: 005380, 066570, 307950
  예시: "현대차, 피지컬 AI 로보틱스 뉴욕 공개"
```

> 참고: 본 dry-run fixture 는 단위 테스트
> `test_513_physical_ai_cluster_surfaces_across_three_tickers` 와 *별개* —
> 단위 테스트 fixture 에는 307950 이 2 헤드라인을 가지므로 거기서는
> `headline_count == 4` 가 정답이다 (확장된 fixture). 두 숫자는 같은
> catalyst 의 다른 realization.

라이브 PyKRX/Naver smoke 는 captive portal 로 불가 — cron 실행 환경 (정상
네트워크) 에서 동일 패턴이 동작. 기존 `KoreanMarketProvider.get_news` 가
PR #5 이래 cron 에서 정상 사용 중.

## Review loop (per CLAUDE.md mandate)

세 reviewer 병렬 실행 — `code-reviewer-pro` agent, `codex review`, `gemini`.

### code-reviewer-pro

- **Critical/Warning**: 0건.
- **[Suggestion] Trailing newline 일관성** — APPLIED. `format_themes_for_prompt`
  empty-themes 분기가 마지막 `\n` 을 포함했는데 non-empty 분기는 포함 안 함.
  두 분기 모두 trailing `\n` 없도록 수정.

### gemini

- **[Major] Naver rate-limit / retry 없음** — DEFERRED. Valid concern이지만
  `KoreanMarketProvider.get_news` 의 책임이라 Stage B 가 wrapper에 retry 를
  넣으면 provider 레이어 책임 침범. provider 가 이미 per-call swallow 패턴
  유지 + 우리 ThreadPool wrapper 가 per-ticker exception swallow → 한 종목
  실패해도 cluster 동작. retry 도입은 별도 PR 영역.
- **[Major] Anchor 보장** — **REJECTED with evidence**. Reviewer가 "filter가
  너무 tight 한 날 anchor가 누락될 가능성" 우려를 표명했으나 Stage A 의
  `discover_kr_candidates(include_anchors=True)` (production default) 가
  filter 결과와 무관하게 ANCHORS_KR 3개를 *unconditionally* prepend 함.
  `test_discover_includes_anchors_even_when_filter_empty` 가 정확히 이
  케이스 (universe=[]) 를 보장. 변경 없음.
- **[Minor] Price-action stopwords (상승/하락/강세 등)** — APPLIED.
  `_KO_STOPWORDS` 에 14개 추가: 상승/하락/강세/약세/돌파/급등/급락/반등
  /상한가/하한가/신고가/신저가/매수/매도. 모멘텀 종목들이 공통으로 "상승"
  헤드라인을 가지면 spurious cluster 가 생기는 걸 방지.
- **[Minor] Fuzzy ticker-set overlap** — DEFERRED. 현재 exact-set 기반
  superset dedup 으로 충분. Jaccard similarity 도입은 Stage 4 (mem0
  semantic) 와 자연스럽게 합쳐질 영역.
- **[Gap] backfill 후 re-rank 없음** — NOT A GAP. 플랜의 Stage B
  "backfill 후 score 재계산" 은 단순히 `news_count_7d` 필드 채움만 의도
  했고 실제 score 함수는 변경 없음 (Stage A 결정론적 정렬 유지). 향후
  점수 재계산을 도입할 때 별도 테스트 추가.

### codex review

- **[P1] mcp-prediction-store/models.py:138 — analysis_group_id migration**
  — **OUT OF SCOPE**. Stage A 리뷰와 동일한 finding. 이 파일은 Stage B
  diff에 포함되지 않은 Doctor Cho의 사전 unstaged 작업. Stage B 심볼
  (theme_clusterer / cluster_news / fetch_news_for_candidates /
  format_themes) 은 codex 출력에 0회 언급.
- **[P2] daily_briefing.py:451 — KR multi-horizon logging** — APPLIED.
  Reviewer 가 정확한 모순을 짚었다: KR prompt 가 4 horizon 분석을 요청
  하면서도 logging 지시는 "for each pick" (1개) 만 요구. US prompt 의
  "for each horizon ≥ 0.60 confidence per pick" 패턴으로 통일.

### Outcome

- 적용된 변경: 3건
  - format_themes empty-branch trailing `\n` 제거
  - 14 price-action stopwords 추가
  - KR prompt multi-horizon logging instruction 수정
- 거부된 major: 1건 (anchor 보장 — 명시적 테스트로 반박)
- 보류 (별도 PR/Stage): 2건 (Naver retry, fuzzy overlap)
- Out of scope: 1건 (codex [P1] — Stage B diff 외부)

## Retrospective

- 5/13 fixture 가 핵심 자산: Stage B 의 cluster_news 가 실제 catalysts
  (KB ETF, 광주 자율주행) 키워드를 추출하는지 명시적으로 보장. 향후 임계값
  튜닝/스톱워드 추가 시 회귀 방지.
- 1 음절 한글 토큰 제외 결정은 의도적이지만 trade-off: "셀" "주" "원"
  같은 짧은 명사 기반 narrative (예: "K-셀") 은 cluster 화 안 됨. 5/13
  케이스에는 문제 없지만 미래 시나리오 등장 시 ngram length 조정 필요할
  수 있음.
- Stage B 의 wiring (fetch_kr_market_data + build_claude_code_prompt 두
  call site) 가 중복인 것 약간 거슬리지만, 두 mode 가 분리된 prompt 흐름을
  가져 인라인 호출이 가장 명확. shared helper 도입은 단일 use-case 추상화
  로 과잉. CLAUDE.md "no abstractions for single-use code" 정신 따라 그대로
  둠.
