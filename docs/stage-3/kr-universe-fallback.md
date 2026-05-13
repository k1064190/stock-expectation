# Stage 3 — Static KR Universe Fallback + Review Findings Fix

## Why

PR #12 의 Stage A 가 dynamic candidate discovery 를 도입했지만, 2026-05-13
production cron 실행 결과 **PyKRX 의 bulk-by-ticker 엔드포인트가 KRX 응답
포맷 변경으로 모두 망가짐** 을 확인했다:

```
ERROR: PyKRX get_market_cap_by_ticker(20260513) failed:
  "None of [Index(['종가', '시가총액', '거래량', '거래대금'], dtype='object')] are in the [columns]"
```

`get_market_cap_by_ticker`, `get_market_fundamental_by_ticker`,
`get_market_ohlcv_by_ticker`, `get_market_ticker_list` 모두 동일 KeyError.
PyKRX 1.2.4 → 1.2.8 (latest) 둘 다 미패치. KRX upstream 변경에 PyKRX 가
대응할 때까지 universe enumeration 이 불가하다.

**결과적으로**: PR #12 의 anchors-only fallback 이 발동하여 5/13 catalyst
(307950 +55%, 066570 +34%, 005380 +30%) 가 여전히 prompt 에 surface 되지
못함. PR #12 의 약속이 PyKRX 결함으로 무효화됨.

추가로 PR #12 머지 후 Copilot 자동 리뷰 6건 + codex `@codex review` 4건
의 finding 이 도착했다. 7건이 즉시 surgical fix 가능.

## What

### Static CSV fallback
- **새 파일** `data/kr_universe.csv` — 166 curated tickers
  (110 KOSPI + 36 KOSDAQ + 20 ETF). 5/13 catalyst 종목 (307950, 066570,
  005380, 012330, 086280) 모두 포함.
- `scheduler/candidate_discovery.py`:
  - `_load_static_universe()` — CSV → `[(ticker, None, None)]` 리스트.
    cap/value 는 None (live bulk 만 알 수 있음, CSV 모드에선 미상).
  - `_load_static_universe_names()` — ticker → name 매핑. `_fill_names` 가
    PyKRX HTTP 호출 전 CSV 를 first-tier 로 사용 → 대부분의 candidate 가
    네트워크 호출 없이 한국어 라벨 획득.
  - `enumerate_kr_universe()` 재작성: PyKRX 시도 → 실패/빈 frame/이상한 컬럼
    → CSV fallback. 둘 다 실패하면 `[]` 반환 (anchors-only 모드).
  - `_fill_names()` 두-tier 로 재작성: CSV 우선, PyKRX backstop.
- `scheduler/tests/test_candidate_discovery.py`:
  - 기존 3개 PyKRX-failure 테스트 의 semantic 갱신 (이제 CSV 로 fallback).
  - 새 테스트 `test_enumerate_csv_missing_returns_empty` — CSV 부재 시
    여전히 anchors-only 안전망 동작 보장.

### Codex/Copilot review fixes (7 건)

| 출처 | 위치 | 변경 |
|---|---|---|
| Codex C1 [P1] | `theme_clusterer.py cluster_news` | dedup key 가 ticker set 이 아닌 **n-gram subsequence** — 같은 ticker set 의 *다른* 테마 (`피지컬 ai` vs `자율주행 로봇`) 가 둘 다 살아남음 |
| Codex C2 [P2] | `candidate_discovery.py enumerate_kr_universe` | `from pykrx import stock` 을 `try:` 안으로 이동 → ImportError 도 fallback path 로 |
| Codex C3 [P2] | `theme_clusterer.py cluster_news` | 같은 헤드라인에서 동일 n-gram 이 두 번 나와도 `headline_count` 는 1 만 증가 |
| Codex C4 [P2] | `daily_briefing.py` KR + US prompt | "Minimum confidence 0.55" → "0.60" (logging gate 와 일치) |
| Copilot #2 | `theme_clusterer.py cluster_news` | dead `seen_for_ticker` 변수 제거 (의도된 dedup 이 Codex C3 로 정착) |
| Copilot #3 | `daily_briefing.py:123` docstring | "3-gram" → "2/3-gram cross-ticker clustering (default ngram_sizes=(2,3))" |
| Copilot #6 | `docs/stage-2/theme-clusterer.md:105` | dry-run 의 headline_count=3 과 unit test fixture 의 headline_count=4 가 *별개* fixture 임을 명시 |

### 회귀 테스트 추가
- `test_distinct_themes_share_ticker_set_survive_both` — Codex C1 명시 보장
- `test_repeated_ngram_in_single_headline_counts_once` — Codex C3 명시 보장
- `test_enumerate_pykrx_failure_falls_back_to_static_csv` —
  5/13 catalyst tickers 모두 CSV 에 포함됨을 회귀로 강제
- `test_enumerate_csv_missing_returns_empty` — 양쪽 다 실패 시
  anchors-only fallback 안전성

### Refactor 거부 (별도 PR 으로 미룸)
- Copilot #1, #5: bare `from candidate_discovery import` / `sys.path` 변형
  → `from .candidate_discovery import` 로 가려면 cron 실행 방식
  (`python scheduler/daily_briefing.py` → `python -m scheduler.daily_briefing`)
  까지 바꿔야 함. 다음 정리 PR 영역.
- Copilot #4: `briefing_kr.md` 의 horizon JSON vs narrative 모순 →
  Doctor Cho 의 사전 horizon 확장 작업 의도 확인 필요.

## How

설계 결정:

1. **CSV 세 컬럼 명시** (`ticker, name, market_segment`) — `market_segment`
   는 **Stage 3 코드에서 미사용** 이지만 의도적으로 유지. 향후 KOSPI/KOSDAQ
   /ETF 별도 필터 (예: KOSDAQ 만 high-vol 스캔, ETF 만 leverage 제외) 가
   필요해질 때 CSV 재발행 없이 컬럼 활용 가능. 컬럼 1개 저장 cost 무시 가능,
   metadata 수동 유지보수에도 유용 (KOSDAQ 신규 IPO 추가 시 segment 명시).
   YAGNI 와 미래 확장성 trade-off 에서 후자 선택.
2. **CSV cap/value 는 None** — KRX 응답 포맷 변경 후 cap/value 를 신뢰
   가능한 소스로 얻을 길이 없음. 정확하지 않은 stale cap 보다 None 이 정직.
   `Candidate.market_cap` / `format_candidates_for_prompt` 가 None 처리 함.
3. **CSV ranking 없음** — score_and_filter 가 universe 전체에 대해
   `get_price_history_batch` 후 |return_5d| / vol_ratio 임계값으로 filtering.
   CSV 의 ticker 순서는 무관 (set union 이라 dedupe).
4. **CSV maintenance 정책** — 본 PR 의 166 tickers 는 KOSPI 100 + KOSDAQ 50 +
   ETF 16 의 best-effort 큐레이션. PyKRX 가 upstream 에서 패치되면 다시
   bulk path 가 활성화되며 CSV 는 fallback 으로만 사용. 분기 단위 수동 refresh
   권장 (스크립트 추가는 Stage 4 영역).
5. **Codex C1 (subsequence dedup)** — `tuple(c.tickers) == tuple(kept.tickers)`
   조건이 살아있고, 그 안에서 `_is_subseq(c.keywords, kept.keywords)` 가 True 일
   때만 drop. Substring 관계가 아닌 두 테마 (예: `자율주행 로봇` vs
   `피지컬 ai`) 는 같은 ticker set 위에 공존.
6. **Codex C3 (per-headline ngram dedup)** — n-gram 인덱싱 루프 안에
   `seen_in_headline: set` 추가. 한 헤드라인 안에서 같은 ngram 두 번 등장 시
   첫 번째만 bucket 에 push. headline_count 의 docstring 정의 ("number of
   headlines") 와 실제 동작 일치.
7. **CSV 위치 패치 가능** — 테스트가 `monkeypatch.setattr(cd, "STATIC_UNIVERSE_PATH", ...)`
   로 fallback-on-fallback 시나리오를 정확히 검증.

## Code locations

- `data/kr_universe.csv` (NEW, 167 lines incl header)
- `scheduler/candidate_discovery.py:96-227` — `_load_static_universe`,
  `_load_static_universe_names`, `enumerate_kr_universe` rewrite, import
  in try block (C2)
- `scheduler/candidate_discovery.py:434-475` — `_fill_names` two-tier
- `scheduler/theme_clusterer.py:275-355` — cluster_news with seen_in_headline
  dedup (C3) + subsequence-based dedup (C1) + `_is_subseq` helper +
  `seen_for_ticker` removal (Copilot #2)
- `scheduler/daily_briefing.py:123` — docstring 2/3-gram (Copilot #3)
- `scheduler/daily_briefing.py:376, 444` — "Minimum confidence 0.60" (C4)
- `scheduler/tests/test_candidate_discovery.py:153-203` — 4 CSV-fallback
  tests
- `scheduler/tests/test_theme_clusterer.py:130-180` — 2 new C1/C3 tests
- `docs/stage-2/theme-clusterer.md:107-113` — fixture clarification (Copilot #6)

## Verification

```bash
# 단위 (71 → 75 with 4 new tests)
uv run pytest scheduler/tests/ -v
# → 75 passed

# Live CSV-fallback smoke (PyKRX bulk broken in current environment)
uv run python -c "
from scheduler.candidate_discovery import discover_kr_candidates
for c in discover_kr_candidates(top_n_output=30):
    print(c.ticker, c.name, c.reason, f'{c.return_5d_pct:+.1f}%')
"
```

Smoke 결과 (실측, 2026-05-13 종가 기준):
```
005930 삼성전자          anchor    +0.0%
000660 SK하이닉스        anchor   +23.4%
069500 KODEX 200       anchor    +0.0%
307950 현대오토에버        momentum +58.5%  ◄◄ 5/13 catalyst
012330 현대모비스         momentum +50.1%   (보너스: 같은 그룹주)
036930 주성엔지니어링       momentum +31.7%
005380 현대차           momentum +29.1%  ◄◄ 5/13 catalyst
086280 현대글로비스        momentum +29.1%  (보너스: 그룹주)
277810 레인보우로보틱스      momentum +25.1%
066570 LG전자          momentum +23.6%  ◄◄ 5/13 catalyst
...
```

PR #12 의 약속이 실제로 surface — Stage A/B 의 architecture 가 valid 함을
재확인하는 evidence.

## Review loop (per CLAUDE.md mandate)

세 reviewer 병렬 — `code-reviewer-pro` agent, `codex review`, `gemini`.

### code-reviewer-pro

- **Critical**: 0건.
- **[Warning] CSV stale entry observability** — APPLIED. `score_and_filter`
  에 stale ticker logging 추가 (universe → bars_by_ticker 차집합을 INFO 로
  남김). 운영자가 cron 로그에서 010620/003410/091990 같은 delisted 종목을
  바로 식별 가능 → 분기 CSV refresh 의사결정에 직접 활용.
- **[Suggestion] `_is_subseq` 문서화** — APPLIED. docstring 에 4 example
  (("ai",) ⊂ ("피지컬", "ai") True, identical 은 False, ⊂ middle 가능 등)
  추가.
- **[Suggestion] `_fill_names` two-pass 주석** — APPLIED. 두 루프가 동일
  Candidate references 를 mutate 하는 점 inline 주석.
- **[Suggestion] CSV `market_segment` 의도 문서화** — APPLIED. docs/stage-3
  Design 결정 #1 을 보강 (YAGNI vs 향후 확장 trade-off 명시).

### gemini

- **No blockers/majors**. 4 검증 영역 (CSV format, `_is_subseq` 수학적 정확성,
  `_fill_names` 2-tier 효율성, 테스트 커버리지) 모두 OK.
- **[Minor] `_load_static_universe_names` docstring**: "Populated lazily on
  first call" 이라 했으나 실제로는 매 호출 disk read — APPLIED. docstring
  수정 (re-reads on every call, ~166 row parse cost negligible).
- **Confirmation**: Codex C1 (subsequence dedup) + C3 (per-headline dedup)
  + C4 (confidence floor 0.60) + C2 (pykrx import in try) 모두 senior-level
  defensive coding 으로 평가.

### codex review

- **[P1] mcp-prediction-store/models.py:138 — analysis_group_id migration
  + 6M/1Y CHECK constraint** — **OUT OF SCOPE**. Stage A/B 리뷰에서 이미
  세 차례 등장한 동일 finding. 본 Stage 3 staged diff 에 `models.py` 미포함
  (Doctor Cho 의 사전 unstaged 작업). Stage 3 심볼 (`kr_universe.csv`,
  `_load_static_universe`, `_is_subseq`, `enumerate_kr_universe`,
  `cluster_news`) 은 codex 출력에 0회 언급. — 다만 **이 finding 자체는 진짜
  버그** 이며 Doctor Cho 가 prediction-store 변경을 committing 할 때 반드시
  migration 추가 필요. 별도 PR 으로 처리 권장.

### Outcome

- 적용된 변경: 4건 (stale logging, `_is_subseq` example, `_fill_names` 주석,
  `_load_static_universe_names` docstring, market_segment 문서)
- 거부된 critical: 1건 (codex P1 — Stage 3 외부)
- Pending Doctor Cho 액션: codex P1 의 underlying migration 이슈는 별도
  prediction-store PR 로 처리

## Retrospective

- CSV maintenance debt: 166 tickers 중 010620 (HD현대미포), 003410 (쌍용C&E),
  091990 (셀트리온헬스케어) 3개가 데이터 없음 (delisted / merged?). Score
  필터가 자동 skip 하므로 무해하지만 cron 마다 FDR retry 로 +5-15s 추가.
  Stage 4 에서 CSV cleanup 스크립트 또는 매뉴얼 정리.
- PyKRX upstream patch 가 나오면 자연스럽게 live path 로 회귀 — CSV 코드
  경로는 사라지지 않고 안전망으로 유지.
- "captive portal 가설" 이 production cron 에서도 동일 실패 발견으로
  반증됨. 진단의 다음 단계까지 가본 가치가 있었음.
