---
name: daily-briefing
description: Morning market briefing with stock picks for US and Korean markets. Generates a daily report covering macro environment, sector rotation, 10-12 actionable predictions (5-6 per market) with BUY/WATCH/HOLD/AVOID/SELL labels and Korean reasoning, plus an auto-Toss-synced portfolio review section recommending hold/add/trim/exit per current position. Each pick is logged as a formal prediction for track record tracking. Triggers on keywords like daily briefing, morning report, market overview, today's picks, what should I trade, 오늘 시장, 일일 브리핑.
---

# Daily Briefing

매크로 + 종목 추천 + 포트폴리오 점검을 통합한 일일 시장 브리핑.

## When to Use

- 장 시작 전 아침 루틴 (자동 cron 또는 수동 호출)
- 오늘 시장 전체를 빠르게 파악하고 매수/매도 결정에 활용
- 트랙 레코드 누적 — 모든 추천이 자동으로 DB에 기록됨

## Prerequisites

- `bin/stock-cli` 실행 가능 (uv-managed env)
- KR 데이터는 키 불필요 (PyKRX)
- 선택: `FMP_API_KEY` (US 데이터 강화), `FINNHUB_API_KEY` + `ALPHA_VANTAGE_API_KEY` (US 뉴스/sentiment)

## Workflow

모든 데이터 접근과 예측 등록은 `bin/stock-cli`를 통해 수행. 모든 명령은 JSON 반환 — 파싱 후 다음 단계 호출.

### 1. 시장 데이터 수집

**US 인덱스 + 섹터:**

> Cron 브리핑은 `scheduler/candidate_discovery.py` 의 `discover_us_candidates()`
> 로 후보를 동적 결정한다 (static S&P 500 + ETF + ADR universe in
> `data/us_universe.csv` → 5일 |return|≥15% OR vol_ratio≥2x 필터 → 3 ETF 앵커
> SPY/QQQ/DIA 병합). 결과 ticker 리스트가 prompt에 직접 주입되므로 LLM은 그
> 목록 안에서만 조회한다. 인터랙티브 사용 시에도 동일하게 호출하거나, 아래
> anchor + 주요 sector ETF 만 빠르게 본 뒤 화제 종목을 추가 조회한다:

```bash
bin/stock-cli price SPY --market US --days 10   # broad market (anchor)
bin/stock-cli price QQQ --market US --days 10   # NASDAQ 100 (anchor)
bin/stock-cli price DIA --market US --days 10   # Dow Jones 30 (anchor)
bin/stock-cli price IWM --market US --days 10   # 소형주 breadth
bin/stock-cli price XLK --market US --days 10   # tech sector
bin/stock-cli price XLF --market US --days 10   # financials
bin/stock-cli price XLE --market US --days 10   # energy
```

**KR 인덱스 + 주요 종목:**

> Cron 브리핑(`scheduler/daily_briefing.py`)은 `scheduler/candidate_discovery.py` 의
> `discover_kr_candidates()` 로 후보를 동적 결정한다 (시총 top-200 ∪ 거래대금
> top-50 → 5일 |return|≥15% OR 거래량비≥2x → 3 앵커 005930/000660/069500 병합).
> 결과 ticker 리스트가 prompt에 직접 주입되므로 LLM은 그 목록 안에서만 조회한다.
> 인터랙티브 사용 시에도 동일하게 호출하거나, 아래 anchor 3종목만 빠르게 본 뒤
> 화제 종목을 추가 조회한다:

```bash
bin/stock-cli price 005930 --market KR --days 10   # 삼성전자 (anchor)
bin/stock-cli price 000660 --market KR --days 10   # SK하이닉스 (anchor)
bin/stock-cli price 069500 --market KR --days 10   # KODEX 200 (anchor)
```

**Multi-horizon 기술 지표 (점수 산정에 사용):**
```bash
# 후보 종목 한 번에 (US 12-15개, KR 12-15개 정도)
bin/stock-cli horizon-metrics-batch SPY,QQQ,NVDA,AMD,MSFT,GOOGL,AAPL,META,AMZN,TSLA,AVGO,MU --market US --days 400
bin/stock-cli horizon-metrics-batch 005930,000660,035420,051910,006400,005380,373220,247540,034020,329180,012450,006800 --market KR --days 400
```

**뉴스 + sentiment** (각 finalist 종목별):
```bash
bin/stock-cli news NVDA --market US --limit 5 --since-days 7
bin/stock-cli news 005930 --market KR --limit 5 --since-days 7
bin/stock-cli disclosure 005930 --since-days 14   # KR only — 감자/유상증자/관리 종목 cap 체크
```

**기존 상태 점검:**
```bash
bin/stock-cli predict list --status OPEN --limit 30
bin/stock-cli track-record --days 30
bin/stock-cli calibration
bin/stock-cli regime --market US    # 하드 BULL 게이트 — Section 4 라벨 규칙 참조
bin/stock-cli regime --market KR
```

### 2. 포트폴리오 상태 수집 (필수 단계)

이 단계는 **항상** 실행한다. positions가 비어있는지 여부는 **출력으로 직접 확인**한 뒤에만 판단한다 (가정하지 말 것).

**2-1. Toss 동기화 (idempotent, 실패해도 briefing 계속):**

```bash
# Toss → 로컬 DB 동기화. TOSS_CLIENT_ID/TOSS_CLIENT_SECRET가 있으면 공식 Open API,
# 없으면 tossctl로 폴백. 실패 시 stderr에 경고만 남기고 0이 아닌 exit 반환 — 무시하고 계속.
bin/stock-cli portfolio sync 2>&1 | tail -5
```

위 명령이 실패해도 (예: 자격증명/네트워크 실패, tossctl 폴백 미인증) 기존 DB는 그대로 사용. briefing은 절대 중단하지 않는다.

**2-2. 포지션 + 평가 데이터 수집:**

```bash
bin/stock-cli portfolio positions --market US
bin/stock-cli portfolio positions --market KR
```

**판단 규칙 (엄격히 준수):**

- 반환 JSON의 `positions` 배열을 직접 파싱한다
- `len(positions) > 0` 이면 **반드시** 7-4의 "내 포트폴리오 점검" 섹션을 생성한다 — 보유가 있는데 누락하면 안 됨
- `len(positions) == 0` 일 때만 안내 한 줄로 대체
- "보유 종목 없음"을 출력하기 전에 반드시 위 명령 출력을 확인하라

**2-3. (positions 비어있지 않을 때만) 평가 데이터:**

```bash
bin/stock-cli portfolio report --market US
bin/stock-cli portfolio report --market KR
bin/stock-cli portfolio risk --market KR
bin/stock-cli portfolio risk --market US
```

### 3. 시장 환경 분석

다음 차원을 평가:

**매크로 레짐:**
- US: risk-on / risk-off (SPY 추세, 섹터 로테이션 패턴)
- KR: KOSPI breadth, 외국인 매매 방향, 원/달러 환율 영향
- 인덱스 vs IWM(소형주) 상대 강도 → 시장이 폭 넓은지

**섹터 로테이션:**
- 1W/1M 기준 leading/lagging 섹터
- US 반도체 ↔ KR 반도체 (cross-market 상관성)
- 방어주 vs 경기민감주 위치

**모멘텀 + breadth:**
- 인덱스 신고가 + 광범위 참여 여부
- KOSDAQ vs KOSPI 상대 강도

### 4. 종목 점수화 + 라벨 결정 (BUY/WATCH/HOLD/AVOID/SELL)

`/expect` 스킬과 **동일한** 결정론적 포인트 테이블 사용 (sync 유지 필수):

**RULE R1 — 하드 레짐 게이트 (BULL 라벨/horizon 로깅 전 적용).** Section 1의 `regime`
출력(`label`)을 해당 종목의 시장에 적용:
- **RISK_OFF**: 신규 BUY/BULL 로깅 금지. 라벨은 WATCH로 캡, BULL horizon은 NEUTRAL로 해소.
  "⚠️ REGIME RISK_OFF" 표기.
- **NEUTRAL**: BUY 기준선 8.0 → **9.0** 상향 + 로깅 confidence 한 단계 추가 트림(0.60 캡).
- **RISK_ON**: 변경 없음.

근거: 2026년 6월 조정장 백필에서, worse-of-SPY/QQQ(US)·KODEX 200(KR) 레짐이 NEUTRAL/RISK_OFF인
동안 발행된 BULL은 ~3%만 적중 — 게이트가 바로 그 구간을 억제/강화한다. 시장 단위 게이트이며,
개별 종목 과열은 RULE R2가 처리.

**RULE R2 — 개별 종목 과열 게이트 (포물선/blow-off 진입).** `horizon-metrics`의
`overextension_level` 사용:
- **EXTREME**: 신규 BUY/BULL 로깅 금지 — WATCH 캡, BULL horizon은 NEUTRAL 해소. "⚠️ OVEREXTENDED (EXTREME)".
- **ELEVATED**: BUY 기준선 +1.0 상향(COMPOSITE ≥ 9.0) + confidence 한 단계 트림(0.60 캡). "⚠️ OVEREXTENDED (ELEVATED)".
- **NONE**: 변경 없음.

근거: closed BULL 377건에서 진입 시 RSI14>75는 26% 적중(RSI<60은 47%), MA20 대비 +15% 초과는
30% 적중(3~8% 구간은 66%). blow-off 진입은 레짐 게이트(R1)가 못 보는 강한 역신호. R1(시장)과
R2(종목)은 함께 적용 — 둘 중 하나라도 BULL을 WATCH로 막으면 WATCH 유지.

**ALGO_SCORE (max +8.0)** — `horizon-metrics-batch` 결과로 산정:
- Trend: MA20>MA50>MA200 → +3.0 | MA20>MA50 only → +1.0 | full bear → -1.0 | else 0
- Momentum: RSI14 ∈ [50,70] → +1.5 | [30,50) → +0.5 | >70 → +0.5 | <30 → -0.5
- Return 1M: ≥+5% → +1.5 | 0~5% → +0.5 | -10%~0 → 0 | ≤-10% → -0.5
- Volume: `vol_ratio` > 1.3 → +1.0 | else 0
- Cycle: `pct_from_52w_high` ≥ -10% → +1.0 | `max_drawdown_1y` ≤ -25% → -1.0 | else 0

**NEWS_SCORE (max +3.0)** — `news` 출력의 `signal` 블록(중복 제거·최신 가중) + `disclosure`로:
- Sentiment: `signal.recency_weighted_sentiment` 사용 — >+0.15 → +2.0 | 0~+0.15 → +1.0 | -0.15~0 → -1.0 | <-0.15 → -2.0 | null → 0
- Headline volume: `signal.unique_count` ≥3 → +1.0 | else 0
- Hard cap: `signal.has_negative_catalyst` true → cap -2.0
- Hard cap: KR 공시 감자/유상증자/관리종목/거래정지/상장폐지 → cap -2.0
- `signal.event_tags`(earnings/guidance/ma/regulatory 등)는 Section 4의 LLM_CONTEXT 논쟁 입력으로 전달

**LLM_CONTEXT_SCORE (range -5.0 ~ +3.0)** — 매크로/내러티브 컨텍스트 (모멘텀 편향 보정):

**Macro detector invocation (amortised, mandatory once per market)**:
시장별 (US, KR) 한 번씩 호출하고 결과를 모든 후보 종목에 재사용:
- `market-top-detector` — 분배일 / defensive rotation / leadership breakdown
- `ftd-detector` — rally attempt / FTD confirmed 상태
- `macro-regime-detector` — Concentration / Broadening / Contraction 등
- `theme-detector` — 섹터 lifecycle phase

브리핑 전체 런타임 < 10분 (cron timeout 여유) 목표. 종목별로 detector 재호출 금지 (10-12배 latency).

Section 3 (시장 환경 분석) 결과 + 섹터 lifecycle + narrative themes 종합해서 종목별로 -5.0 ~ +3.0 부여. 앵커:
- **+3.0**: 강한 매크로 호재 + 섹터 early-stage breakout
- **+1.5**: 약한 호재 (favorable regime, mid-stage)
- **0**: 중립 (default — 특별한 매크로/내러티브 시그널 없을 때)
- **-1.5**: 약한 악재 (late-stage 섹터, 중립 매크로)
- **-3.0**: 매크로 톱 + 섹터 late + 단기 이벤트 (예: KOSPI 분배일, 외인 매도 가속, FX 1500↑, 어닝 직전)
- **-5.0**: 시스템 위기 (베어마켓 진입 + 펀더 악화)

**반드시 `llm_context_reasoning` 텍스트도 같이** — 매크로 시그널 1개 이상 인용 ("외인 -5조", "분배일 4건", "FTD 미확인" 등). Section 3에서 이미 분석한 내용을 종목별로 매핑.

**COMPOSITE = ALGO_SCORE + NEWS_SCORE + LLM_CONTEXT_SCORE** (범위 -12 ~ +14):
- ≥ 8.0 → **BUY**
- [6.0, 8.0) → **WATCH**
- [3.0, 6.0) → **HOLD**
- [0, 3.0) → **AVOID**
- < 0 → **SELL**

**임계값은 유지** — LLM_CONTEXT가 음수면 자연스럽게 BUY → WATCH/HOLD 다운그레이드, 양수면 약한 ALGO+NEWS 조합도 BUY 진입 가능.

각 시장에서 **5-6개 종목 선정**해 라벨 부여 (총 10-12개). 라벨이 BUY/WATCH만 있다면 좋지만, AVOID/SELL도 자연스럽게 포함 가능 (다양성 + 공부 자료).

### 5. 예측 등록 (각 종목별)

BUY/WATCH/HOLD 라벨 종목은 모두 DB에 등록 (AVOID/SELL은 정보 제공만, 등록 선택).

**종목별 `analysis_group_id` 부여**: 종목마다 UUID를 한 번 생성하고, 그 종목에서 등록하는
모든 예측 행에 동일한 `--analysis-group-id`로 전달한다(`/expect`와 동일 패턴). 그래야 한 분석에서
나온 행들을 묶어 추적/중복 식별이 가능하다. 종목이 바뀌면 새 UUID를 생성한다.

```bash
GROUP_ID=$(uv run python -c "import uuid; print(uuid.uuid4())")

bin/stock-cli predict create \
  --ticker 005930 --market KR --direction BULL \
  --confidence 0.62 --timeframe 1M \
  --entry-price 268500 --target-price 300000 --stop-price 248000 \
  --reasoning "RSI14=74.5 healthy momentum, MA20>MA50>MA200 stack, return_1m=+36.6%, AV sentiment N/A (KR), 반도체 사이클 후반 cycle_risk_flag=True, LLM_CONTEXT -1.5 (late-stage sector)" \
  --signals technical,momentum,llm_context \
  --components '{"algo":6.0,"news":0.0,"llm_context":-1.5,"overextension":"NONE","regime":"NEUTRAL"}' \
  --source LIVE \
  --recalibrate \
  --analysis-group-id "$GROUP_ID"
```

`--components`는 항상 해당 콜의 pillar별 기여도(algo/news/llm_context 점수 + overextension 레벨 +
regime 라벨)를 담아 전달 — `bin/stock-cli component-contribution`로 세 능력의 기여를 따로 측정하고
향후 blended confidence 학습에 사용.

**예측 품질 규칙:**
- Confidence 0.55-0.85, 4-signal-alignment 규칙 + calibration 캡 적용
- **재보정 적용**: raw confidence를 그대로 넘기고 `--recalibrate` 플래그를 추가하면 CLI가 isotonic recalibration 곡선으로 결정론적으로 매핑해 저장한다 (예: 과신 구간 0.62 → ~0.50). 직접 손으로 매핑하지 말 것 — 플래그가 일관되게 처리하며, 해당 source의 closed 예측이 30건 미만이면 안전하게 no-op. JSON 출력의 `raw_confidence`/`recalibration_applied`로 확인.
- **죽은 시그널 금지**: `cycle`, `valuation`, `mean_reversion`은 적중률 0%로 판정되어 signals_used에 기록하지 말 것 (calibration 오염). 해당 정성 신호는 `LLM_CONTEXT_SCORE`로 반영.
- 최소 2개 signal 명시 (signals_used). `llm_context` 시그널은 `LLM_CONTEXT_SCORE`가 0이 아닐 때만 포함 (주별 calibration이 이 시그널 단독 측정)
- KR 종목 기본 timeframe 1M (유동성 낮음 고려)
- US 종목 기본 timeframe 1W
- **리스크/엣지 게이트 (필수)**: `reward = |target − entry|`, `risk = |entry − stop|`. `reward/risk < 1.5`면 엣지가 얇으므로 **등록하지 말 것**(WATCH/HOLD로 정보만 제공). Target ≥ 2 × Stop 거리(2:1) 권장.
- **BEAR 상향 바**: 측정된 BEAR 적중률 ~6%(BULL ~61%) — 하락 예측은 신뢰 불가. BEAR는 (a) ≥3 signal 정렬, (b) 매크로/사이클 확인(`LLM_CONTEXT ≤ −2.0` 또는 `cycle_risk_flag=True`), (c) reward:risk ≥ 2.0 **모두** 충족할 때만 등록. 미충족 시 등록 생략. **`--source LIVE`에서는 store가 BEAR 행을 하드 거부(에러)하므로 cron 실행 시 BEAR는 절대 등록 시도하지 말 것.**
- 자동 cron이면 `--source LIVE`, 대화형이면 `--source INTERACTIVE`

### 6. 포트폴리오 액션 추천 (보유 종목 있을 때)

각 보유 포지션을 다음 기준으로 평가:

| 기준 | 추천 |
|---|---|
| 현재 ALGO/NEWS 점수 + 평단 대비 P&L + RSI/MA 상태 | |
| composite ≥ 6 & P&L > 0% & 추세 정상 | **보유 유지** |
| composite ≥ 8 & 평단 대비 +5% 이상 & 추가 진입 여력 | **추가 매수** (분할) |
| composite 3-6 & P&L > +20% & RSI > 75 | **부분 매도** (이익 실현) |
| composite < 3 & 손절가 근접 | **손절 검토** |
| composite < 0 OR 한도 caps 발동 (관리종목 등) | **전량 매도** |

각 추천에 1-2문장 한국어 사유 필수 (구체 숫자 인용).

### 7. 최종 출력 (한국어 우선)

```markdown
# 📊 Daily Market Briefing — [YYYY-MM-DD]

## 1. 요약 (Executive Summary)

3-5 bullet points 한국어:
- 오늘 시장의 핵심 테마
- 리스크 레벨 (낮음/보통/높음)
- 전반적 bias (강세/중립/약세)

## 2. 매크로 환경

### 미국
[SPY/QQQ/DIA 추세 + 섹터 리더십 + 주요 가격대 — 한국어 paragraph 3-5줄]

### 한국
[KOSPI/KOSDAQ 추세 + 외국인 흐름 + 원/달러 환율 영향 — 한국어 paragraph 3-5줄]

## 3. 오늘의 종목 추천

### 🇺🇸 미국 (5-6 종목)

#### 1. NVDA — *BUY* (composite 9.0)
- *점수*: ALGO 6.0 / NEWS 3.0
- *가격대*: 진입 215.20 → 목표 226 (+5%) / 손절 207 (-4%)
- *시간*: 1주
- *분석*: NVIDIA는 RSI14=65.86으로 건강한 모멘텀 구간이며, MA20>MA50>MA200 완전한 상승 정렬을 유지하고 있습니다. 지난 1개월 +17% 상승했지만 52주 고점 대비 -0.6%로 아직 추가 여력이 있고, Finnhub+AV 종합 sentiment +0.28로 뉴스 흐름도 우호적입니다. AI 인프라 수요 지속 + 사이클 리스크 낮은 편으로 *분할 매수 추천*.
- *로깅*: `predict_id=<uuid>`

#### 2. AAPL — *WATCH* (composite 7.5)
[동일 구조 — 점수 / 가격대 / 시간 / 분석 / 로깅]

[3-6번 종목 동일 형식]

### 🇰🇷 한국 (5-6 종목)

#### 1. 005930 삼성전자 — *WATCH* (composite 7.0)
- *점수*: ALGO 6.0 / NEWS 1.0
- *가격대*: 진입 285,500원 / 목표 320,000 (+12%) / 손절 263,000 (-8%)
- *시간*: 1개월
- *분석*: 삼성전자는 MA20>MA50>MA200 완전한 정렬에 RSI14=74.5로 약간 과열 구간이지만 거래대금 1위로 외국인 매수가 뒷받침되고 있습니다. 지난 1년 +382% 상승해 cycle_risk_flag=True로 단기 조정 가능성 있어 WATCH로 분류 — 285,000 이하 풀백 시 분할 진입 권장. KR 공시 cap 트리거 없음.
- *로깅*: `predict_id=<uuid>`

[2-6번 종목 동일 형식]

> **Markdown 주의**: Telegram MarkdownV1는 단일 `*` (별표 1개)만 bold로 인식. 이중 `**bold**`는 unclosed entity 에러 발생 → 출력 시 모든 강조는 단일 `*text*`만 사용.

## 4. 내 포트폴리오 점검

**판단 규칙**: 위 Step 2-2의 `positions` 배열을 직접 보고, 길이 > 0 이면 반드시 이 섹션을 채워라. "보유 종목 없음"은 positions가 진짜 빈 배열일 때만 출력.

(보유 종목 진짜 없을 때) → "보유 종목 없음 — `bin/stock-cli portfolio create` 후 매수 기록을 추가하시면 다음 브리핑부터 자동 점검됩니다." 한 줄.

(보유 종목 있을 때) →

### 🇺🇸 US 포지션

#### NVDA (10주, 평단 $185.00)
- 현재가 $215.20, P&L *+16.3%* ($+302)
- *추천*: 보유 유지
- *사유*: 추세 정상(MA stack), 모멘텀 건강(RSI 65), 1주 horizon 추가 BUY 예측 발생. 다만 +16% 익절 영역 들어가니 +25% 도달 시 부분 매도(30%) 권장.

#### TSLA (5주, 평단 $280.00)
- 현재가 $240.00, P&L *-14.3%* ($-200)
- *추천*: 손절 검토
- *사유*: composite=2.5 (AVOID 구간), MA20<MA50<MA200 풀 베어 정렬, 손절가 $238 근접. 한 번 더 -2% 빠지면 전량 매도 권장.

### 🇰🇷 KR 포지션

#### 005930 삼성전자 (50주, 평단 220,000원)
[동일 구조]

#### 종합
- US 포지션 평균 +N%
- KR 포지션 평균 +N%
- 섹터 집중 위험: [`portfolio risk` 결과 인용]
- 다음 액션 우선순위: [1순위 / 2순위]

## 5. 트랙 레코드 업데이트

[`track-record` + `calibration` 결과를 한국어로 정리]
- 30일 적중률: N%
- Brier score: N
- 시그널별 성과: technical N%, news N%, momentum N%
- 과신 버킷: [있으면 명시]

## 6. 오늘의 주요 이벤트

[`economic-calendar-fetcher` 또는 web search로 확인한 이벤트]
- US: [CPI 발표 / 어닝스 등]
- KR: [정책 발표 / 어닝스 등]
- 글로벌: [FOMC / 중국 등]

---

*모든 예측 ID는 `bin/stock-cli predict detail <id>`로 상세 조회 가능*
*다음 자동 브리핑: [평일 07:00 KR / 21:00 US]*
```

## 분석 톤 가이드

- **한국어 우선** — 매크로/분석/추천 사유는 모두 한국어 paragraph로 작성
- **구체 숫자 명시** — "강한 상승"보다 "RSI 65, 1개월 +17%" 식으로
- **명확한 액션** — "관심 가질 만함"이 아니라 "215.20 분할 매수, 207 손절"
- **리스크 동등 비중** — TECH 1줄, NEWS 1줄, RISK 1줄 (transmission chain 원칙)
- **과신 자제** — calibration에서 발견된 과신 버킷이 있다면 confidence 자동 캡

## 분량 가이드

전체 briefing: ~1500-2500자 (Telegram 한 메시지 4096자 한계 내, 2개 분할 예상). 종목별 분석은 5-6 줄 paragraph로 제한.
