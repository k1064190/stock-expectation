# Stage 1 — Dynamic KR Candidate Discovery

## Why

2026-05-13 cron daily briefing이 "피지컬 AI" 테마 폭등(현대오토에버 +55%,
LG전자 +34%, 현대차 +30% / 10일)을 완전히 놓치고 Samsung/SK하이닉스/현대차만
재추천했다. 추적 결과 두 가지 구조적 원인 중 첫 번째:

- `scheduler/daily_briefing.py` 가 KR 블루칩 6종목을 하드코딩 → 307950 같은
  mid-cap 모멘텀 폭등 종목은 LLM 시야 밖.
- `briefing_kr.md` 가 "Samsung/SK하이닉스 only" 프레이밍으로 LLM을 앵커링.

Stage 1 (= 플랜 Stage A) 은 첫 번째 원인을 제거한다 — 두 번째는 Stage 2(= 플랜 Stage B)
에서 다룬다.

## What

- 새 모듈 `scheduler/candidate_discovery.py`:
  - `Candidate` dataclass (ticker, name, market, cap, trading_value,
    return_5d_pct, vol_ratio_5d, news_count_7d, reason).
  - `ANCHORS_KR = (("005930","삼성전자"), ("000660","SK하이닉스"), ("069500","KODEX 200"))`
    — 6개 하드코딩에서 3개 reference 앵커로 축소.
  - `enumerate_kr_universe(top_n_cap=200, top_n_value=50)` —
    PyKRX `get_market_cap_by_ticker(today, market="ALL")` 1회 호출로
    시총·거래대금 동시 획득, 두 top-N 합집합 dedupe.
  - `score_and_filter(universe, provider, …)` —
    30일 OHLCV 일괄 조회, `|return_5d|≥15%` OR `vol_ratio_5d≥2x` 통과만
    유지. reason = "momentum" | "volume".
  - `discover_kr_candidates(top_n_output=20, …)` — enumerate → filter →
    anchor merge → 동적 후보를 `|return_5d|` 내림차순 정렬 → truncate.
    앵커가 동적 필터도 통과하면 dedupe하고 reason="anchor"로 relabel하되
    동적 메트릭(return/vol)은 유지.
  - `format_candidates_for_prompt(cands)` — LLM이 직접 읽을 한국어 마크다운
    블록. 시총 단위 `조`/`억` 자동 포맷.
- `scheduler/daily_briefing.py` 수정:
  - `fetch_kr_market_data()` 가 `discover_kr_candidates` + price-batch
    조회로 재작성 (API 모드용).
  - `build_claude_code_prompt("KR")` 가 `discover_kr_candidates` 결과를
    f-string에 `{candidate_block}` + `{ticker_csv}` 로 inline 주입
    (cron 기본 모드).
- `.claude/skills/daily-briefing/SKILL.md` 의 정적 ticker 리스트를
  "동적 결정됨, 3 앵커만 reference로" 안내로 교체.
- 단위 테스트 21개 추가: enumerate 4 + score_filter 7 + discover 5 +
  format 2 + defaults 1.

## How

설계 핵심 결정:

1. **유니버스 = 시총 top-N ∪ 거래대금 top-N** — 시총만 보면 mid-cap
   폭등을 놓치고 (현대오토에버는 시총 200위 밖일 수 있음), 거래대금만
   보면 메가캡 노이즈가 너무 많다. 합집합이 둘 다 잡는다.
2. **앵커는 3개로 축소** — Samsung / SK하이닉스 / KOSPI ETF 셋만.
   기존 6 종목 중 NAVER / LG화학 / 삼SDI / 현대차는 동적 필터 통과해야만
   등장. 이렇게 해야 cron이 "오늘 의미 있는 종목"만 보여준다.
3. **News count는 Stage 1에서 backfill하지 않음** — 200 종목 뉴스 fetch는
   timeout 위험이 있어 Stage 2 의 theme_clusterer가 top-30 후보에 대해서만
   가져와 채운다.
4. **PyKRX 실패 시 anchors-only fallback** — `enumerate_kr_universe`는
   exception을 `except`로 잡아 빈 리스트 반환. `discover_kr_candidates`는
   `include_anchors=True` 일 때 3 앵커는 항상 prompt에 등장. cron이 절대
   crash 하지 않는다.
5. **Name 조회는 survivor 집합에만** — `_fill_names`는 PyKRX
   `get_market_ticker_name` 을 순차 호출하므로 ~250 유니버스 전체 보다
   필터 통과한 ~20-30 종목에만 적용해 성능 절감.
6. **Provider 주입 가능** — `discover_kr_candidates(provider=…)` 로 unit
   test에서 fake provider 주입. PyKRX 호출도 `unittest.mock.patch` 로 격리.

## Code locations

- `scheduler/candidate_discovery.py` (NEW) — 340줄 module
- `scheduler/tests/test_candidate_discovery.py` (NEW) — 21 tests, 모두 mock
- `scheduler/daily_briefing.py:44-56` — import 추가
- `scheduler/daily_briefing.py:107-149` — `fetch_kr_market_data()` rewrite
- `scheduler/daily_briefing.py:386-410` — KR claude-code prompt rewrite
- `.claude/skills/daily-briefing/SKILL.md:46-56` — KR ticker 안내 교체

## Verification

```bash
# 1. 신규 단위 테스트
uv run pytest scheduler/tests/test_candidate_discovery.py -v
# → 21 passed

# 2. 기존 회귀
uv run pytest scheduler/tests/test_daily_briefing.py -v
# → 6 passed (no regression)

# 3. Prompt 빌더 dry-run with mocked candidates
# (스크립트는 verification section의 5/13 시나리오 fixture)
# → 307950, 066570, 005380 모두 prompt에 포함 확인
# → price-batch 명령에 ticker_csv 포함 확인
```

**Live PyKRX smoke test는 본 작업 머신의 captive portal로 막혀 실행 불가**
(Yonsei 네트워크가 PyKRX/FinanceDataReader의 KRX 호출을 가로채는 HTML 리다이렉트
응답을 반환). cron 실행 환경에는 정상 네트워크가 있어야 하며, 같은 PyKRX 벌크
엔드포인트를 사용하는 기존 `KoreanMarketProvider.get_fundamentals_batch` 가
정상 동작하는 환경이라면 본 모듈도 정상 동작한다.

## Review loop (per CLAUDE.md mandate)

세 reviewer 병렬 실행 — `code-reviewer-pro` agent, `codex review`, `gemini`.

### code-reviewer-pro

- **[Critical] Missing `sys.path` for scheduler dir** — **REJECTED with evidence**.
  Reviewer 주장: `daily_briefing.py` 가 `from candidate_discovery import ...` 를
  하는데 sys.path에 scheduler 디렉토리를 추가하지 않아 `ModuleNotFoundError`
  발생할 것. 검증: `uv run python scheduler/daily_briefing.py --help` 정상
  exit 0. Python 은 스크립트 실행 시 그 파일이 있는 디렉토리를 자동으로
  `sys.path[0]` 에 추가하므로 bare import 가 동작한다. 동일 패턴인
  `from telegram_sender import send_briefing` 가 PR #4 (commit `e7a00b5`)
  이래 cron 에서 정상 동작 중. 변경 없음.
- **[Suggestion 1] Prompt constraint may be too strict** — DEFERRED to Stage B.
  Plan 이 의도적으로 "이 목록 안에서만" 으로 시작하기로 결정했고, Stage B 의
  theme_clusterer 가 동적 후보 보완을 다룬다. Stage B 라이브 검증 후 재검토.
- **[Suggestion 2] Document `top_n_output < 3` edge case** — APPLIED.
  `discover_kr_candidates` docstring 에 "Callers should not set top_n_output
  below len(ANCHORS_KR) — anchors otherwise dropped from tail" 문장 추가.

### codex review

- **[P1] mcp-prediction-store/models.py:138 analysis_group_id migration** —
  **OUT OF SCOPE**. Codex 가 working tree 의 unstaged 변경 (Doctor Cho 의
  이전 세션 작업) 까지 본 결과로 발생. Stage A diff 는 `scheduler/*` +
  `.claude/skills/daily-briefing/SKILL.md` + `docs/stage-1/*` 에 한정.
  Stage A 심볼(`candidate_discovery`, `fetch_kr_market_data`,
  `discover_kr_candidates` 등) 은 codex 출력에 0회 언급.
- Stage A 코드 자체에 대해 codex 가 제기한 finding **없음**.

### gemini

- **[Major] PyKRX DataFrame already contains ticker names** — **REJECTED**.
  PyKRX 1.2.4 소스 (`stock_api.py:394` `get_market_cap_by_ticker` docstring)
  확인 결과 컬럼은 `종가, 시가총액, 거래량, 거래대금, 상장주식수` 만이고
  index 는 ticker 코드뿐. 별도 name 조회 호출이 필요하다. 기존
  `mcp-market-data/providers/kr.py:174` 의 `get_fundamentals_batch` 도 동일
  패턴 사용. 변경 없음.
- **[Minor 1] Rename `trading_value` → `turnover`** — DECLINED. 내부 데이터
  클래스 필드이며 prompt 출력은 이미 "거래대금" 으로 한국어 label. 명명
  자체로 인한 혼동 가능성 낮음.
- **[Minor 2] Add absolute minimum trading_value floor for vol_ratio noise** —
  ALREADY MITIGATED. `_vol_ratio` 가 `prior_20 == 0` 일 때 1.0 반환하므로
  신규 상장 거래 정지 케이스는 자동으로 필터 통과 못함. 더 strict 한 floor
  는 옵션이지만 cron 의 false positive 가 실측되기 전엔 노이즈 줄이는
  대가로 진짜 신호도 잃을 가능성이 있어 보류.

### Outcome

- 적용된 변경: 1건 (docstring note for `top_n_output < 3`).
- 거부된 critical/major: 2건 (sys.path, PyKRX name 컬럼) — 둘 다 검증 기반
  반박 가능, 변경 없음.
- 거부된 minor: 2건 (이름 변경, 거래대금 floor).
- Stage A 코드에 대한 reviewer 합의 = 큰 결함 없음.

## Retrospective

- 계획대로 잘 진행: 21개 단위 테스트가 5/13 시나리오를 명시적으로 fixture로
  잡아내 회귀 방지를 보장. `score_and_filter` 의 fixture (`_bars_with_return`)
  가 직관적이라 미래 임계값 튜닝 시 재사용 좋다.
- 발견된 함정: PyKRX 1.2.4 의 bulk-by-ticker 호출들이 일부 환경에서 KRX
  endpoint 응답 형식 mismatch로 KeyError 던짐 (`종가`/`시가총액` 칼럼 lookup
  실패). 본 모듈은 이를 `except`로 잡아 anchors-only로 안전하게 fallback하지만,
  cron 환경에서도 같은 에러가 떨어지면 동적 발굴이 무력화된다. Stage 3 or 4에서
  정적 fallback 유니버스 CSV 또는 KRX 직접 endpoint 폴백 고려 가치 있음.
- 다음 단계로 carry-forward: Stage B 의 `theme_clusterer.fetch_news_for_candidates`
  가 `Candidate.news_count_7d` 를 backfill하고 점수 재계산을 트리거할 수 있도록
  Candidate dataclass는 mutable로 유지 (frozen 아님).
