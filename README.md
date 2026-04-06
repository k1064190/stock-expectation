# Stock Expectation

US 주식과 한국 주식의 방향(상승/하락/횡보)을 예측하고, 그 예측의 적중률을 자동으로 추적하는 시스템.

하나의 CLI 도구(`bin/stock-cli`)와 Claude Code의 스킬 시스템을 결합해, 대화형 분석과 자동화 브리핑을 모두 지원한다.

## 이 시스템이 하는 일

1. **종목 추천 + 방향 예측**: 기술적 분석, 펀더멘털, 섹터 흐름, 모멘텀, 센티먼트 5개 시그널을 종합해 확률 가중 예측 생성
2. **예측 기록 관리**: 모든 예측을 SQLite에 저장, 실제 결과와 대조해 HIT/MISS/EXPIRED 자동 판정
3. **트랙 레코드**: 적중률, Brier score, 시그널별 성과, 캘리브레이션 리포트 제공
4. **자기 개선**: 과거 적중률을 다음 예측 프롬프트에 주입해, 과신/과소평가 패턴을 보정

## 시스템 구조

```
┌─────────────────────────────────────────────────┐
│              Claude Code (대화형)                  │
│  "NVDA 분석해줘" / "오늘 브리핑" / "적중률 보여줘"     │
│                                                   │
│  .claude/skills/daily-briefing     → 일일 브리핑    │
│  .claude/skills/stock-research     → 종목 심층 분석 │
│  .claude/skills/korean-market-analysis → 한국 전문 │
│  .claude/skills/prediction-review  → 트랙 레코드    │
└──────────────────────┬──────────────────────────┘
                       │ Bash 호출
                       ▼
┌─────────────────────────────────────────────────┐
│             bin/stock-cli (uv run)                │
│                                                   │
│  price, fundamentals, search, health              │
│  predict create / list / detail / cancel          │
│  track-record, calibration                        │
│                                                   │
│  → JSON 출력                                       │
└──────────┬──────────────────────┬────────────────┘
           │                      │
           ▼                      ▼
┌──────────────────┐  ┌──────────────────────┐
│ providers (py)   │  │ prediction store (py)│
│                  │  │                      │
│ US: yfinance/FMP │  │ Prediction schema    │
│ KR: PyKRX/FDR    │  │ Metrics computation  │
└────────┬─────────┘  └──────────┬───────────┘
         │                       │
         ▼                       ▼
┌─────────────────────────────────────────────────┐
│           SQLite (data/predictions.db)            │
│  predictions 테이블 — 모든 예측과 결과 저장          │
│  WAL 모드 — 동시 읽기/쓰기 지원                     │
└──────────┬──────────────────────┬────────────────┘
           ▲                      ▲
           │                      │
┌──────────┴─────────┐  ┌────────┴──────────┐
│ daily_briefing.py  │  │ outcome_tracker.py│
│ (cron, 07:00/21:00)│  │ (cron, 00:00)     │
│                    │  │                   │
│ claude -p 호출     │  │ 가격 조회          │
│ → bin/stock-cli    │  │ → HIT/MISS 판정   │
│ → Telegram 전송    │  │ → DB 업데이트      │
└────────────────────┘  └───────────────────┘
```

## 작동 원리

### CLI-first 설계

과거에는 MCP 서버 2개(market-data, prediction-store)를 띄워서 Claude Code가 MCP 도구로 호출했다. 지금은 **단일 CLI 바이너리(`bin/stock-cli`)**로 단순화했다.

**이유:**
- MCP 서버는 stdio JSON-RPC로 감싼 얇은 래퍼에 불과 — 실제 로직은 provider 함수들
- CLI는 Claude Code가 이미 잘 쓰는 Bash 도구로 호출 가능
- 에러 메시지가 그대로 보임 (MCP는 silent failure)
- 사용자도 터미널에서 직접 호출해서 디버깅/자동화 가능
- 백그라운드 Python 프로세스 불필요

**Claude Code 스킬이 CLI를 사용하는 방식:**
```bash
# 스킬 파일(.claude/skills/daily-briefing/SKILL.md)에 이런 예시가 있음
bin/stock-cli price NVDA --market US --days 30
bin/stock-cli predict create --ticker NVDA --market US --direction BULL \
  --confidence 0.72 --timeframe 1W --entry-price 125.50 \
  --reasoning "..." --signals technical,momentum
```

Claude가 SKILL.md를 읽고 필요한 `bin/stock-cli` 명령을 Bash로 실행한다.

### 예측 생성 과정

1. Claude가 `bin/stock-cli price <ticker>` 등을 실행해 JSON으로 시장 데이터 확보
2. 5개 시그널(기술적, 펀더멘털, 섹터, 모멘텀, 센티먼트)을 각각 0-100 점수로 평가
3. 가중 평균으로 composite score 산출 → 방향(BULL/BEAR/NEUTRAL)과 confidence(0.50-0.95) 결정
4. `bin/stock-cli predict create ...`로 예측을 DB에 저장
5. 저장된 예측에는 ticker, 진입가, 목표가, 손절가, 근거, 사용한 시그널 목록이 포함됨

### 결과 판정 규칙

`outcome_tracker.py`가 매일 밤 실행되며 다음 규칙으로 판정:

| 조건 | 판정 | 설명 |
|------|------|------|
| BULL 예측 + 현재가 >= 목표가 | **HIT** | 상승 맞춤 |
| BULL 예측 + 현재가 <= 손절가 | **MISS** | 손절 도달 |
| BEAR 예측 + 현재가 <= 목표가 | **HIT** | 하락 맞춤 |
| BEAR 예측 + 현재가 >= 손절가 | **MISS** | 역방향 이동 |
| 기간 만료 (HIT/MISS 미발생) | **EXPIRED** | 시간 초과 |

- 목표가/손절가 미설정 시 기본값: +3% HIT, -5% MISS
- **MISS가 HIT보다 우선** (보수적 판정)
- US 종목: 16:00 ET 종가 기준, 한국 종목: 15:30 KST 종가 기준
- 1W = 5거래일, 2W = 10거래일, 1M = 21거래일

### 자기 개선 루프

매 브리핑 생성 시 과거 트랙 레코드를 프롬프트에 주입:

```
### Your Track Record (Last 30 Days)
- Win rate: 62% (13W/8L)
- Calibration: 70% confidence 예측이 실제 68% 적중 (양호)
- Best signal: breadth (78%), Worst: momentum (51%)
- Recommendation: breadth 시그널 우선, momentum 단독 예측 축소
```

Claude는 이 정보를 보고 과신하던 시그널의 confidence를 낮추고, 잘 맞추던 시그널에 더 가중한다.

### 한국 시장 특수 처리

- 기본 예측 기간: **2주** (US는 1주). 유동성이 낮아 더 긴 시간 필요
- 손절폭: US 대비 **20% 더 넓게** (변동성 반영)
- 크로스마켓: NVDA/AMD 실적 → 삼성전자/SK하이닉스 1일 래그 반영
- 원/달러 환율: 수출기업에 미치는 영향 분석
- 재벌 디스카운트: 한국 P/E가 글로벌 대비 30-50% 낮은 구조적 현상 반영

## 설치

### 1. uv 설치 (없으면)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. 프로젝트 동기화

```bash
cd ~/projects/stock-expectation

# 기본 의존성 (CLI 동작에 필요한 것만)
uv sync

# 개발 의존성 포함 (pytest)
uv sync --extra dev

# Anthropic API 모드 쓸 때만
uv sync --extra api
```

`uv sync`는 `.python-version`을 보고 Python 3.11을 자동으로 가져오고, `pyproject.toml`의 의존성을 `.venv/`에 설치한다.

### 3. 동작 확인

```bash
# CLI 헬스체크
./bin/stock-cli health

# 테스트 (네트워크 불필요한 것만)
uv run pytest -m "not network"

# 전체 테스트
uv run pytest
```

### 4. 환경 변수 (선택)

```bash
# Telegram 알림 (선택)
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="your_chat_id"

# FMP API (선택, 없으면 yfinance fallback)
export FMP_API_KEY="your_key"

# Anthropic API (--mode api 사용 시에만)
export ANTHROPIC_API_KEY="your_key"
```

## 사용법

### 터미널에서 직접 CLI 호출

```bash
# 가격 조회
./bin/stock-cli price NVDA --market US --days 30
./bin/stock-cli price 005930 --market KR --days 10

# 펀더멘털
./bin/stock-cli fundamentals AAPL --market US

# 종목 검색
./bin/stock-cli search "삼성" --market KR

# 예측 생성
./bin/stock-cli predict create \
  --ticker NVDA --market US --direction BULL \
  --confidence 0.72 --timeframe 1W --entry-price 125.50 \
  --target-price 135.00 --stop-price 120.00 \
  --reasoning "Strong breakout with volume" \
  --signals technical,momentum

# 예측 조회
./bin/stock-cli predict list --status OPEN
./bin/stock-cli predict detail <id>
./bin/stock-cli predict cancel <id>

# 트랙 레코드
./bin/stock-cli track-record --days 30
./bin/stock-cli calibration

# 헬스체크
./bin/stock-cli health
```

모든 출력은 JSON. 파이프로 `jq`와 조합 가능:
```bash
./bin/stock-cli price NVDA --market US --days 5 | jq '.current_price'
./bin/stock-cli predict list --status OPEN | jq '.[] | {ticker, confidence, reasoning}'
```

### 대화형 (Claude Code에서)

이 프로젝트 폴더에서 Claude Code를 열면 `.claude/skills/`의 스킬들이 자동으로 로드된다.

```
> 오늘 일일 브리핑 해줘
> NVDA 분석해줘
> 삼성전자 어때?
> 한국 시장 분석해줘
> 내 예측 적중률 보여줘
```

Claude는 스킬 파일을 읽고 필요한 `bin/stock-cli` 명령을 Bash로 실행한다.

### 자동화 (cron)

```bash
# 수동 실행
uv run python scheduler/daily_briefing.py --market US
uv run python scheduler/daily_briefing.py --market KR
uv run python scheduler/daily_briefing.py --market ALL

# Anthropic API 모드 (ANTHROPIC_API_KEY 필요)
uv sync --extra api
uv run python scheduler/daily_briefing.py --market US --mode api

# 결과 판정 (LLM 불필요, 가격만 조회)
uv run python scheduler/outcome_tracker.py
```

### cron 등록

```bash
crontab scheduler/crontab.example
```

등록되는 스케줄:

| 시각 (KST) | 작업 | 설명 |
|------------|------|------|
| 07:00 | `daily_briefing.py --market KR` | 한국 시장 브리핑 (장 시작 전) |
| 21:00 | `daily_briefing.py --market US` | US 시장 브리핑 (프리마켓 전) |
| 00:00 | `outcome_tracker.py` | 오픈 예측 결과 판정 |

### 자동화 모드 비교

| | Claude Code 모드 (기본) | Anthropic API 모드 |
|--|----------------------|-------------------|
| 실행 방식 | `claude -p` CLI 호출 | `anthropic.Anthropic()` API 호출 |
| 데이터 조회 | Claude가 `bin/stock-cli` via Bash | 스크립트가 provider 직접 호출 |
| 예측 저장 | Claude가 `bin/stock-cli predict create` | 스크립트가 JSON 파싱 후 저장 |
| 추가 비용 | 없음 (Claude Code 구독 포함) | ~$5-15/월 (Sonnet) |
| 필요 환경변수 | 없음 | `ANTHROPIC_API_KEY` |
| 필요 의존성 | 기본 (`uv sync`) | `uv sync --extra api` |
| 장점 | 비용 없음, 스킬 파일 그대로 사용 | claude CLI 없는 서버에서 가능 |

## 프로젝트 구조

```
stock-expectation/
├── CLAUDE.md                          # Claude Code 프로젝트 설정
├── README.md                          # 이 문서
├── pyproject.toml                     # uv 의존성 정의
├── .python-version                    # Python 3.11
├── uv.lock                            # uv 잠금 파일
├── stock_cli.py                       # CLI 엔트리 포인트
├── bin/
│   └── stock-cli                      # uv run 래퍼 (실행 가능)
│
├── .claude/skills/                    # Claude Code 자동 발견 경로
│   ├── daily-briefing/SKILL.md        #   일일 브리핑 + 종목 추천
│   ├── stock-research/SKILL.md        #   개별 종목 5-signal 분석
│   ├── korean-market-analysis/SKILL.md#   한국 시장 전문 분석
│   └── prediction-review/SKILL.md     #   트랙 레코드 대시보드
│
├── mcp-market-data/                   # 시장 데이터 providers
│   ├── providers/
│   │   ├── base.py                    #   공통 인터페이스 + retry
│   │   ├── us.py                      #   US: FMP → yfinance fallback
│   │   └── kr.py                      #   KR: PyKRX → yfinance fallback
│   └── tests/
│
├── mcp-prediction-store/              # 예측 저장 + 메트릭
│   ├── models.py                      #   Prediction 스키마 + DB CRUD
│   ├── metrics.py                     #   적중률, Brier, 캘리브레이션
│   └── tests/
│
├── scheduler/                         # 자동화 스크립트
│   ├── daily_briefing.py              #   일일 브리핑 (claude-code / api 모드)
│   ├── outcome_tracker.py             #   HIT/MISS/EXPIRED 판정
│   ├── telegram_sender.py             #   Telegram 전송 모듈
│   ├── crontab.example                #   cron 설정 템플릿
│   ├── prompts/                       #   API 모드용 프롬프트 템플릿
│   └── tests/
│
├── data/                              # SQLite DB (gitignore)
│   └── predictions.db
│
└── claude-trading-skills/             # 참고용 레포 (직접 의존 아님)
```

> 디렉터리 이름에 `mcp-`가 남아있는 건 과거 흔적이다. 지금은 MCP 서버가 아니라
> CLI에서 import하는 순수 provider/models 모듈이다.

## 데이터 소스

| 시장 | Primary | Fallback | 비용 |
|------|---------|----------|------|
| US 가격 | FMP API | yfinance | FMP 무료 250건/일, yfinance 무료 |
| US 펀더멘털 | FMP API | yfinance | 동일 |
| KR 가격 | PyKRX (KRX 스크래핑) | FinanceDataReader | 무료 |
| KR 펀더멘털 | PyKRX | yfinance (.KS/.KQ) | 무료 |
| KR 종목 검색 | yfinance + PyKRX | — | 무료 |

모든 provider는 `is_healthy()` 헬스체크와 3회 재시도 (1s → 4s → 16s exponential backoff)를 지원한다. Primary가 실패하면 자동으로 fallback으로 전환된다.

## 예측 스키마

```
prediction:
  id:             UUID (자동 생성)
  created_at:     UTC ISO 타임스탬프
  ticker:         "NVDA" 또는 "005930"
  market:         "US" | "KR"
  direction:      "BULL" | "BEAR" | "NEUTRAL"
  confidence:     0.0-1.0 (예측 확신도)
  timeframe:      "1W" | "2W" | "1M" | "3M"
  entry_price:    예측 시점 주가
  target_price:   목표가 (선택)
  stop_price:     손절가 (선택)
  reasoning:      분석 근거
  signals_used:   ["technical", "breadth", ...] 사용한 시그널 목록
  source:         "LIVE" (자동) | "INTERACTIVE" (수동) | "BACKTEST"
  status:         "OPEN" → "HIT" | "MISS" | "EXPIRED" | "CANCELLED"
  outcome_price:  결과 확정 시 주가
  outcome_return: 수익률 (%)
```

## 트랙 레코드 지표

| 지표 | 설명 |
|------|------|
| Win Rate | HIT / (HIT + MISS). EXPIRED 제외 |
| Avg Return | 전체 마감 예측의 평균 수익률 |
| Current Streak | 연속 적중/실패 횟수 (+3 = 3연속 적중) |
| Brier Score | 예측 확률의 정확도. 0에 가까울수록 좋음. 0.1 이하 우수 |
| Calibration | 70% confidence 예측이 실제 70% 맞는지 검증 |
| Signal Attribution | 시그널별 적중률. 최소 10건 이상일 때만 표시 |

## 참고

이 시스템은 [claude-trading-skills](https://github.com/tradermonty/claude-trading-skills)의 스킬 패턴과 분석 프레임워크를 참고해 설계되었다.
claude-trading-skills는 US 시장 전용 50+ 스킬 모음이며, 이 프로젝트는 거기에 한국 시장 지원, 예측 트랙 레코드, 자기 개선 루프를 추가한 별도 시스템이다.
