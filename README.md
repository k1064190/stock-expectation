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
┌──────────────────────────────────────────────────────────┐
│                   Claude Code (대화형)                      │
│  "/expect ALL" / "오늘 브리핑" / "적중률 보여줘"              │
│                                                            │
│  .claude/skills/ (31 활성 + _archived/19 + 11 삭제)         │
│  ├── 핵심 4: expect, daily-briefing, stock-research,        │
│  │           prediction-review                              │
│  ├── 포트폴리오 5: portfolio-eval/-manager, position-sizer, │
│  │                 toss-sync, trader-memory-core            │
│  ├── 레짐+브레드스 6: macro-regime-detector, FTD,           │
│  │                    market-breadth/top-detector, ...      │
│  ├── 스크리너 5: vcp, canslim, finviz, base-breakout,       │
│  │               earnings-trade-analyzer                    │
│  ├── 캘린더+분석 5: earnings-/economic-calendar,            │
│  │                  theme-detector, technical-/stock-analysis │
│  ├── 한국 전용 1: korean-market-analysis                    │
│  └── 메타+ops 5: backtest-expert, data-quality-checker,     │
│                  signal-postmortem, retrospect, init        │
└───────────────────────┬──────────────────────────────────┘
                        │ Bash 호출
                        ▼
┌──────────────────────────────────────────────────────────┐
│              bin/stock-cli (uv run)                        │
│                                                            │
│  데이터:    price[-batch], fundamentals[-batch], search,    │
│             health, horizon-metrics[-batch]                 │
│  뉴스/공시: news (US: Finnhub→AV→FMP→yfinance / KR: Naver), │
│             disclosure (KR: Open DART)                      │
│  예측:      predict create/list/detail/cancel,              │
│             track-record, calibration                       │
│  포트폴리오: portfolio create/buy/sell/import/positions/    │
│              report/risk/vs-predictions/advice              │
│  옵션 모듈: memory (mem0+Qdrant, --extra memory),           │
│             graph (Neo4j, --extra graph)                    │
│                                                            │
│  → JSON 출력                                                │
└───────────┬──────────────────────┬───────────────────────┘
            │                      │
            ▼                      ▼
┌──────────────────────┐  ┌───────────────────────────────┐
│  mcp-market-data/    │  │  mcp-prediction-store/         │
│  providers/          │  │                                │
│                      │  │  models.py — Prediction        │
│  US 가격: FMP → yf   │  │    schema + DB CRUD            │
│  US 뉴스: Finnhub →  │  │  metrics.py — 적중률 / Brier / │
│    AV sentiment      │  │    캘리브레이션 / 시그널별 성과 │
│    merge → FMP → yf  │  └───────────┬───────────────────┘
│  KR 가격: PyKRX → yf │              │
│  KR 뉴스: Naver scrape│             │
│  KR 공시: Open DART  │              │
│  indicators.py:      │              │
│    horizon metrics   │              │
└─────────┬────────────┘              │
          │                           │
          ▼                           ▼
┌──────────────────────────────────────────────────────────┐
│            SQLite (data/predictions.db)                    │
│  predictions 테이블 — analysis_group_id로 멀티-호라이즌 묶음 │
│  data/portfolio.db (분리) — 포지션/체결 기록                 │
│  WAL 모드 — 동시 읽기/쓰기 지원                              │
└───────────┬──────────────────────┬───────────────────────┘
            ▲                      ▲
            │                      │
┌───────────┴──────────┐  ┌────────┴────────────────────┐
│  daily_briefing.py   │  │  outcome_tracker.py          │
│  (cron, 07/21/00시)  │  │  (cron, 매일 06:00)          │
│  codex exec 호출     │  │  가격 조회 → HIT/MISS 판정   │
│  → bin/stock-cli     │  │                              │
│  → Telegram 전송     │  │  weekly_calibration.py       │
│                      │  │  (cron, 일요일 22:00)        │
│                      │  │  주간 캘리브레이션 리포트    │
└──────────────────────┘  └─────────────────────────────┘

           옵션 레이어 (별도 --extra로 활성화)
┌──────────────────────┐  ┌─────────────────────────────┐
│  mcp-memory-store/   │  │  mcp-graph-store/            │
│  mem0 + Qdrant       │  │  Neo4j Community Edition     │
│  (sentence-trans)    │  │  (docker compose up -d neo4j)│
│                      │  │                              │
│  predictions/news/   │  │  Stock-LINKED_TO-Theme,      │
│  themes/outcomes/    │  │  Stock-MENTIONED_IN-News 등  │
│  transmission_chains │  │  idempotent MERGE 인제스션  │
└──────────────────────┘  └─────────────────────────────┘
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
bin/stock-cli news NVDA --market US --limit 5 --since-days 7
bin/stock-cli horizon-metrics-batch NVDA,AMD,AVGO --market US --days 400
bin/stock-cli predict create --ticker NVDA --market US --direction BULL \
  --confidence 0.65 --timeframe 1W --entry-price 215.20 \
  --target-price 226.00 --stop-price 206.60 \
  --reasoning "..." --signals technical,news,momentum \
  --analysis-group-id <UUID>
```

Claude가 SKILL.md를 읽고 필요한 `bin/stock-cli` 명령을 Bash로 실행한다.

### 예측 생성 과정

스킬에 따라 두 가지 흐름이 있다.

**`/expect` (결정론적 point-table)** — 본 시스템의 핵심:
1. WebSearch로 트렌딩 종목 10개 발굴 (US 5 + KR 5)
2. `bin/stock-cli horizon-metrics-batch` 1회 호출로 기술적 지표 일괄 수집 (MA20/50/200, RSI, 1W/1M/6M/1Y 수익률, 52w high/low 거리, max drawdown, vol_ratio, cycle_risk_flag)
3. `bin/stock-cli news` + `disclosure`로 뉴스/공시 수집 (US는 AV sentiment 병합)
4. **고정 포인트 테이블**로 `ALGO_SCORE` (max +8.0) + `NEWS_SCORE` (max +3.0) = `COMPOSITE` (-7..+11) 산출
5. half-open 범위로 `BUY` / `WATCH` / `HOLD` / `AVOID` / `SELL` 라벨 결정
6. 3-fact transmission chain(TECH/NEWS/RISK) 생성, 각 종목별 multi-horizon 예측(1W/1M/6M/1Y)을 공통 `analysis_group_id`로 DB 저장
7. `state/last-outcome-expect.json` 사이드카에 모든 컴포넌트 점수 기록 (주간 캘리브레이션이 소비)

**레거시 스킬 (`daily-briefing`, `stock-research` 등)** — 기존 가중 평균 흐름:
1. Claude가 `bin/stock-cli price <ticker>` 등을 실행해 JSON으로 시장 데이터 확보
2. 5개 시그널(기술적, 펀더멘털, 섹터, 모멘텀, 센티먼트)을 각각 0-100 점수로 평가
3. 가중 평균으로 composite score 산출 → 방향과 confidence 결정
4. `bin/stock-cli predict create ...`로 예측을 DB에 저장

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

# Anthropic API 모드 (scheduler --mode api 사용 시)
uv sync --extra api

# 스킬 스크립트 직접 실행 시 (jsonschema, pyyaml, scipy)
uv sync --extra skills

# Stage 7-A 메모리 레이어 (mem0 + Qdrant + sentence-transformers)
uv sync --extra memory

# Stage 7-B 그래프 레이어 (Neo4j 드라이버)
uv sync --extra graph

# 전부 한꺼번에
uv sync --extra dev --extra api --extra skills --extra memory --extra graph
```

`uv sync`는 `.python-version`을 보고 Python 3.11을 자동으로 가져오고, `pyproject.toml`의 의존성을 `.venv/`에 설치한다. memory/graph extras는 무거우므로 기본 설치에 포함하지 않고, 사용할 때만 추가한다. 미설치 상태에서 `memory`/`graph` 서브커맨드를 호출하면 "install with `uv sync --extra X`" 안내 메시지를 출력하고 종료한다.

### 3. 동작 확인

```bash
# CLI 헬스체크
./bin/stock-cli health

# 빠른 테스트 (네트워크 불필요)
uv run pytest -m "not network"   # 207 통과 기대

# 네트워크 테스트 (실제 API 호출)
uv run pytest -m network          # 23 통과 + 1 사전 PyKRX 실패 (무관)

# 전체
uv run pytest
```

### 4. 환경 변수

프로젝트 루트의 `.env` 파일에 설정. `python-dotenv`로 모든 엔트리 포인트(stock_cli, scheduler 등)에서 자동 로드된다. 기존 셸 export는 `override=False`로 보존.

`.env.example`가 저장소에 있으니 복사부터:
```bash
cp .env.example .env
$EDITOR .env
```

주요 키:
```bash
# 가격/펀더멘털 (선택)
FMP_API_KEY="your_key"                 # FMP 무료 250건/일, 없으면 yfinance fallback

# 뉴스/공시 (Stage 2, /expect의 NEWS_SCORE 컴포넌트가 사용)
FINNHUB_API_KEY="your_key"             # US 뉴스 primary, 무료 60 req/min
ALPHA_VANTAGE_API_KEY="your_key"       # US 뉴스 sentiment merge, 무료 25 req/day
                                       # ※ 오타 주의: ALPHA_VATAGE_API_KEY(X)
OPEN_DART_API_KEY="your_key"           # KR 공시 (감자/유상증자/관리종목 등), 무료
NAVER_CLIENT_ID="your_id"              # KR 뉴스 (네이버 검색 API), 무료 ~25,000 req/day
NAVER_CLIENT_SECRET="your_secret"      # 미설정 시 finance.naver.com 스크레이프로 폴백

# 자동화/알림
TELEGRAM_BOT_TOKEN="your_bot_token"    # Telegram 알림 (선택)
TELEGRAM_CHAT_ID="your_chat_id"
ANTHROPIC_API_KEY="your_key"           # scheduler --mode api 사용 시에만

# Stage 7-B 그래프 (Neo4j docker compose 사용 시)
NEO4J_PASSWORD="changeme123"           # compose.yml이 이 값 없으면 부팅 실패
NEO4J_URI="bolt://localhost:7687"
NEO4J_USER="neo4j"

# 포트폴리오 매니저 (Alpaca 브로커리지 연동 시, 선택)
APCA_API_KEY_ID="your_key"
APCA_API_SECRET_KEY="your_secret"
```

FMP 무료 티어 참고: 2025-08-31 이후 가입자는 레거시 엔드포인트(`/v3/earning_calendar`, `/v3/economic_calendar`)에 접근 불가. 해당 스킬은 web search로 자동 fallback.

## 사용법

### 터미널에서 직접 CLI 호출

```bash
# 가격 조회 (단일 / 배치)
./bin/stock-cli price NVDA --market US --days 30
./bin/stock-cli price 005930 --market KR --days 10
./bin/stock-cli price-batch AAPL,MSFT,NVDA --market US --days 30
./bin/stock-cli price-batch 005930,000660,035420 --market KR --days 30

# 펀더멘털 (단일 / 배치)
./bin/stock-cli fundamentals AAPL --market US
./bin/stock-cli fundamentals-batch AAPL,MSFT,NVDA --market US

# 종목 검색
./bin/stock-cli search "삼성" --market KR

# Multi-horizon 기술 지표 (MA/RSI/리턴/사이클/거래량 — /expect가 사용)
./bin/stock-cli horizon-metrics NVDA --market US --days 400
./bin/stock-cli horizon-metrics-batch NVDA,AMD,AVGO --market US --days 400

# 뉴스 (Stage 2)
./bin/stock-cli news NVDA --market US --limit 5 --since-days 7
./bin/stock-cli news 005930 --market KR --limit 5 --since-days 7

# KR 공시 (Stage 2, Open DART)
./bin/stock-cli disclosure 005930 --since-days 14

# 예측 CRUD
./bin/stock-cli predict create \
  --ticker NVDA --market US --direction BULL \
  --confidence 0.65 --timeframe 1W --entry-price 215.20 \
  --target-price 226.00 --stop-price 206.60 \
  --reasoning "RSI 65.86 healthy momentum; MA20>MA50>MA200; AV sent +0.28" \
  --signals technical,news,momentum \
  --analysis-group-id <UUID>
./bin/stock-cli predict list --status OPEN
./bin/stock-cli predict detail <id>
./bin/stock-cli predict cancel <id>

# 트랙 레코드 + 캘리브레이션
./bin/stock-cli track-record --days 30
./bin/stock-cli calibration

# 포트폴리오 (Stage 포트폴리오)
./bin/stock-cli portfolio create --market KR --name "Toss KR"
./bin/stock-cli portfolio buy 005930 --qty 10 --price 55000 --market KR
./bin/stock-cli portfolio sell 005930 --qty 5 --price 60000 --market KR --date 2026-04-01
./bin/stock-cli portfolio import trades.csv --market KR --dry-run
./bin/stock-cli portfolio positions --market KR
./bin/stock-cli portfolio report --market KR
./bin/stock-cli portfolio risk --market KR
./bin/stock-cli portfolio vs-predictions --market KR
./bin/stock-cli portfolio advice --market KR

# Stage 7-A 메모리 (mem0 + Qdrant, --extra memory 필요)
./bin/stock-cli memory stats
./bin/stock-cli memory search "AI infrastructure" --category predictions --limit 5
./bin/stock-cli memory purge <id> --yes

# Stage 7-B 그래프 (Neo4j Community, --extra graph 필요)
./bin/stock-cli graph init
./bin/stock-cli graph query "MATCH (n) RETURN count(n) AS total"
./bin/stock-cli graph similar-stocks NVDA --limit 5
./bin/stock-cli graph theme-winners "AI"

# 헬스체크
./bin/stock-cli health
```

모든 출력은 JSON. 파이프로 `jq`와 조합 가능:
```bash
./bin/stock-cli price NVDA --market US --days 5 | jq '.current_price'
./bin/stock-cli horizon-metrics-batch NVDA,AMD --market US | jq '.results.NVDA.vol_ratio'
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

Codex는 스킬 파일을 읽고 필요한 `bin/stock-cli` 명령을 Bash로 실행한다.

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

# 주간 캘리브레이션 (Stage 6) — predictions.db 스냅샷 + 시그널별 성과 + 과신 버킷 플래그
uv run python scheduler/weekly_calibration.py --dry-run
uv run python scheduler/weekly_calibration.py
# 출력: reports/weekly-calibration-YYYY-MM-DD.md
#       state/calibration-trend.json (12주 롤링)
```

### cron 등록

```bash
crontab scheduler/crontab.example
```

등록되는 스케줄:

| 시각 (KST) | 작업 | 설명 |
|------------|------|------|
| 07:00 월-금 | `daily_briefing.py --market KR --mode codex-cli` | 한국 시장 브리핑 (장 시작 전) |
| 21:00 월-금 | `daily_briefing.py --market US --mode codex-cli` | US 시장 브리핑 (프리마켓 전) |
| 00:00 화-토 | `daily_briefing.py --market US --mode codex-cli` | US 장중 브리핑 |
| 06:00 매일 | `outcome_tracker.py` | 오픈 예측 결과 판정 |
| 22:00 일요일 | `weekly_calibration.py` | 주간 캘리브레이션 + 트렌드 저장 |

### 자동화 모드 비교

| | Codex CLI 모드 (기본) | Claude Code 모드 | Anthropic API 모드 |
|--|----------------------|------------------|-------------------|
| 실행 방식 | `codex exec` CLI 호출 | `claude -p` CLI 호출 | `anthropic.Anthropic()` API 호출 |
| 데이터 조회 | Codex가 `bin/stock-cli` via Bash | Claude가 `bin/stock-cli` via Bash | 스크립트가 provider 직접 호출 |
| 예측 저장 | Codex가 `bin/stock-cli predict create` | Claude가 `bin/stock-cli predict create` | 스크립트가 JSON 파싱 후 저장 |
| 추가 비용 | ChatGPT/Codex CLI credit | Claude Code 구독 | ~$5-15/월 (Sonnet) |
| 필요 환경변수 | 없음 (`CODEX_MODEL` 선택) | 없음 | `ANTHROPIC_API_KEY` |
| 필요 의존성 | 기본 (`uv sync`) | 기본 (`uv sync`) | `uv sync --extra api` |
| 장점 | 스킬 파일 그대로 사용, cron 기본 경로 | Codex 장애 시 fallback | CLI 없는 서버에서 가능 |

## 프로젝트 구조

```
stock-expectation/
├── CLAUDE.md                          # Claude Code 프로젝트 설정 + 운영 규칙
├── README.md                          # 이 문서
├── pyproject.toml                     # uv 의존성 정의 (extras: dev/api/skills/memory/graph)
├── .python-version                    # Python 3.11
├── .env.example                       # 환경 변수 템플릿 (필요한 키 + 가입 링크)
├── compose.yml                        # Stage 7-B Neo4j Community Edition 서비스
├── uv.lock
├── stock_cli.py                       # CLI 엔트리 포인트 (모든 서브커맨드)
├── bin/
│   └── stock-cli                      # uv run 래퍼 (실행 가능)
│
├── .claude/skills/                    # Claude Code 자동 발견 (31 활성)
│   ├── expect/                        # /expect 결정론적 BUY/SELL 추천기 (핵심)
│   ├── daily-briefing/                # 일일 브리핑 + 종목 추천
│   ├── stock-research/                # 개별 종목 5-signal 분석
│   ├── prediction-review/             # 트랙 레코드 대시보드
│   ├── portfolio-eval/                # 포트폴리오 P&L + 리스크 + 어드바이스
│   ├── portfolio-manager/             # Alpaca 연동 포트폴리오 관리
│   ├── position-sizer/                # 리스크 기반 포지션 사이징
│   ├── toss-sync/                     # Toss 증권 자동 동기화
│   ├── trader-memory-core/            # 투자 논문 생애주기 추적
│   ├── macro-regime-detector/         # 매크로 레짐 분류
│   ├── market-breadth-analyzer/       # 시장 폭 (breadth) 점수
│   ├── market-top-detector/           # O'Neil distribution days 등
│   ├── ftd-detector/                  # Follow-Through Day 감지
│   ├── uptrend-analyzer/              # Monty Uptrend Ratio 대시보드
│   ├── sector-analyst/                # 섹터 로테이션 분석
│   ├── vcp-screener/                  # Minervini VCP 스크리닝
│   ├── canslim-screener/              # O'Neil CANSLIM 스크리닝
│   ├── finviz-screener/               # FinViz URL 빌더
│   ├── base-breakout-screener/        # 6M 베이스 브레이크아웃
│   ├── earnings-trade-analyzer/       # 어닝스 트레이드 5-factor 점수
│   ├── earnings-calendar/             # FMP 어닝스 캘린더 (mid-cap+)
│   ├── economic-calendar-fetcher/     # FMP 경제 캘린더
│   ├── theme-detector/                # 테마/섹터 라이프사이클
│   ├── technical-analyst/             # 주간 차트 기술적 분석
│   ├── stock-analysis/                # US 종합 펀더+테크니컬
│   ├── korean-market-analysis/        # 한국 시장 전문 (KOSPI/KOSDAQ)
│   ├── backtest-expert/               # 백테스트 검증 가이드
│   ├── data-quality-checker/          # 마켓 분석 문서 QA
│   ├── signal-postmortem/             # 시그널 사후 분석
│   ├── retrospect/                    # 세션 회고
│   ├── init/                          # CLAUDE.md 초기화
│   └── _archived/                     # 19 보관 + 11 삭제 — _archived/README.md 참조
│
├── mcp-market-data/                   # 시장 데이터 providers + 지표
│   ├── providers/
│   │   ├── base.py                    #   공통 인터페이스 + NewsItem/Disclosure + retry
│   │   ├── us.py                      #   가격 FMP→yf, 뉴스 Finnhub→AV→FMP→yf
│   │   └── kr.py                      #   가격 PyKRX→yf, 뉴스 Naver, 공시 DART
│   ├── indicators.py                  #   compute_horizon_metrics (MA/RSI/리턴/vol_ratio)
│   └── tests/                         #   test_indicators / test_news / test_news_live
│
├── mcp-prediction-store/              # 예측 저장 + 메트릭
│   ├── models.py                      #   Prediction 스키마 + DB CRUD
│   ├── metrics.py                     #   적중률, Brier, 캘리브레이션, 시그널별
│   └── tests/
│
├── mcp-memory-store/                  # Stage 7-A 메모리 레이어 (--extra memory)
│   ├── client.py                      #   MemoryStore (mem0 lazy import)
│   ├── schemas.py                     #   CategoryName / MemoryRecord / SearchHit
│   ├── ingestion.py                   #   ingest_predictions / transmission_chains
│   └── tests/
│
├── mcp-graph-store/                   # Stage 7-B 그래프 레이어 (--extra graph)
│   ├── driver.py                      #   GraphDriver (neo4j lazy import)
│   ├── cypher.py                      #   INIT_STATEMENTS + CANNED_QUERIES
│   ├── ingestion.py                   #   idempotent MERGE 인제스션
│   └── tests/
│
├── portfolio/                         # 포트폴리오 트래킹
│   ├── models.py / db.py / evaluator.py / importer.py
│   └── tests/
│
├── scheduler/                         # 자동화 스크립트
│   ├── daily_briefing.py              #   일일 브리핑 (codex-cli / claude-code / api 모드)
│   ├── outcome_tracker.py             #   HIT/MISS/EXPIRED 판정
│   ├── weekly_calibration.py          #   Stage 6 주간 캘리브레이션 (cron 일 22:00)
│   ├── telegram_sender.py             #   Telegram 전송 모듈
│   ├── crontab.example                #   cron 설정 템플릿
│   ├── prompts/                       #   API 모드용 프롬프트 템플릿
│   └── tests/
│
├── data/                              # SQLite DB + 캐시 (gitignore)
│   ├── predictions.db                 #   예측 + analysis_group_id 묶음
│   ├── portfolio.db                   #   포트폴리오 체결/포지션
│   └── dart_corp_codes.csv            #   Open DART 종목 코드 캐시
│
├── state/                             # 런타임 사이드카 (gitignore)
│   ├── last-outcome-expect.json       #   /expect 매 실행 결과 + 컴포넌트 점수
│   └── calibration-trend.json         #   주간 캘리브레이션 12주 롤링
│
├── reports/                           # 주간 캘리브레이션 마크다운 (gitignore)
│
├── docs/                              # 단계별 문서 (per-stage)
│   ├── HANDOFF.md                     # 머지된 작업의 단일 진입점 문서
│   └── stage-{1..7,4.1}/              # 각 단계 Why/What/How/Retrospective
│
├── references/                        # 외부 참고 레포 (gitignore)
└── claude-trading-skills/             # 원본 스킬 컬렉션 (참고용)
```

> 디렉터리 이름에 `mcp-`가 남아있는 건 과거 흔적이다. 지금은 MCP 서버가 아니라
> CLI에서 import하는 순수 provider/models 모듈이다.

## 데이터 소스

| 시장 | 종류 | Primary → Fallback | 비용 |
|------|------|--------------------|------|
| US | 가격 | FMP API → yfinance | FMP 무료 250건/일 |
| US | 펀더멘털 | FMP API → yfinance | 동일 |
| US | 뉴스 | Finnhub → Alpha Vantage sentiment merge → FMP → yfinance | Finnhub 60 req/min, AV 25 req/day |
| KR | 가격 | PyKRX (KRX 스크래핑) → FinanceDataReader → yfinance | 무료 |
| KR | 펀더멘털 | PyKRX → yfinance (.KS/.KQ) | 무료 (※ 벌크 호출 사전 버그 존재 — HANDOFF §11.E) |
| KR | 종목 검색 | yfinance + PyKRX | 무료 |
| KR | 뉴스 | Naver Finance 스크래핑 (table.type5 + iframe Referer) | 무료 |
| KR | 공시 | Open DART REST API (`/rcept/list.json`) | 무료, corp_code 1회 다운로드 캐시 |

가격/펀더멘털 provider는 `is_healthy()` 헬스체크와 3회 재시도(1s → 4s → 16s exponential backoff)를 지원한다. Primary가 실패하면 자동으로 fallback으로 전환된다. 뉴스/공시는 best-effort로 retry 미적용(429 시 30초 retry 체인이 `/expect`를 느리게 함).

US 뉴스의 AV sentiment 병합은 URL-match를 시도하지만 Finnhub가 자체 리다이렉트 URL을 반환하므로 실질적으로 항상 ticker-average 폴백으로 떨어진다 — DEBUG 로그에 `0 URL-matched, N ticker-avg fallback` 형식으로 가시화된다 (Stage 4.1 B4).

## 예측 스키마

```
prediction:
  id:                  UUID (자동 생성)
  created_at:          UTC ISO 타임스탬프
  ticker:              "NVDA" 또는 "005930"
  market:              "US" | "KR"
  direction:           "BULL" | "BEAR" | "NEUTRAL"
  confidence:          0.0-1.0 (예측 확신도)
  timeframe:           "1W" | "2W" | "1M" | "3M" | "6M" | "1Y"
                       (Stage 4: /expect가 multi-horizon 1W/1M/6M/1Y 로깅)
  entry_price:         예측 시점 주가
  target_price:        목표가 (선택)
  stop_price:          손절가 (선택)
  reasoning:           분석 근거
  signals_used:        ["technical", "news", "momentum", ...] 사용한 시그널 목록
  source:              "LIVE" (자동) | "INTERACTIVE" (수동) | "BACKTEST"
  status:              "OPEN" → "HIT" | "MISS" | "EXPIRED" | "CANCELLED"
  outcome_price:       결과 확정 시 주가
  outcome_date:        결과 확정 일자
  outcome_return:      수익률 (decimal)
  analysis_group_id:   같은 종목의 multi-horizon 예측을 묶는 UUID (Stage 4)
                       /expect 1회 실행이 같은 종목에 1W/1M/6M/1Y 예측을 만들면
                       모두 같은 group_id를 공유 — 그룹 단위 적중률 추적 가능
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

## Telegram 연동

Telegram 연동은 **두 가지 모드**로 작동한다.

### 1. Push 모드 (cron → Telegram)

`scheduler/daily_briefing.py`가 cron에 의해 실행되면, 결과를 `scheduler/telegram_sender.py`를 통해 Telegram 채팅방으로 자동 전송한다.

```
cron (07:00 KST) → daily_briefing.py → codex exec → 분석 생성
                                                    → telegram_sender.py → Telegram 채팅방
```

**설정:**
```bash
# .env에 추가
TELEGRAM_BOT_TOKEN="your_bot_token"
TELEGRAM_CHAT_ID="your_chat_id"
```

**전송되는 내용:**
- 매일 아침 한국 시장 브리핑 (07:00 KST)
- 매일 저녁 US 시장 브리핑 (21:00 KST)
- 예측 HIT/MISS 알림 (outcome_tracker 결과)
- 긴 메시지는 4096자 단위로 자동 분할

**한계:** 단방향. 알림만 받을 수 있고, Telegram에서 질문하거나 스킬을 실행할 수는 없다.

### 2. Interactive 모드 (Telegram ↔ Claude Code)

Claude Code의 Telegram MCP 플러그인을 사용하면 **양방향 대화**가 가능하다.

```
사용자 (Telegram) → "NVDA 분석해줘"
                       ↓
              Claude Code (실행 중)
              → stock-research 스킬 트리거
              → bin/stock-cli price NVDA
              → bin/stock-cli fundamentals NVDA
              → 5-signal 분석 생성
                       ↓
사용자 (Telegram) ← 분석 결과 수신
```

**사용 가능한 예시:**
- `"오늘 일일 브리핑"` → daily-briefing 스킬
- `"NVDA 분석해줘"` → stock-research 스킬
- `"VCP 스크리닝 해줘"` → vcp-screener 스킬
- `"market breadth 분석"` → market-breadth-analyzer 스킬
- `"포지션 사이즈 계산: 10만불 계좌, NVDA 130불 진입, 124불 손절"` → position-sizer 스킬
- 31개 활성 스킬 전부 Telegram에서 호출 가능

**한계:**
- Claude Code 세션이 실행 중이어야 함 (백그라운드 `claude` 프로세스 필요)
- 응답 시간은 스킬에 따라 10초~5분

## 포팅된 트레이딩 스킬

[claude-trading-skills](https://github.com/tradermonty/claude-trading-skills) 등 외부 컬렉션에서 가져온 스킬을
`bin/stock-cli`를 통해 US/KR 듀얼 마켓으로 적응시켰다. **Stage 3 정리** 후 31개 활성, 19개 archived,
11개 삭제 (자세한 내용은 `.claude/skills/_archived/README.md` 참조).

### 카테고리별 분류 (31개 활성)

| 카테고리 | 스킬 수 | 예시 |
|---------|--------|------|
| 핵심 흐름 | 4 | expect, daily-briefing, stock-research, prediction-review |
| 포트폴리오 | 5 | portfolio-eval, portfolio-manager, position-sizer, toss-sync, trader-memory-core |
| 레짐 + 브레드스 | 6 | macro-regime-detector, market-breadth-analyzer, FTD, market-top-detector, sector-analyst, uptrend-analyzer |
| 스크리너 | 5 | vcp-screener, canslim-screener, finviz-screener, base-breakout-screener, earnings-trade-analyzer |
| 캘린더 + 분석 | 5 | earnings-calendar, economic-calendar-fetcher, theme-detector, technical-analyst, stock-analysis |
| 한국 전용 | 1 | korean-market-analysis |
| 메타 + ops | 5 | backtest-expert, data-quality-checker, signal-postmortem, retrospect, init |

## 참고

이 시스템은 [claude-trading-skills](https://github.com/tradermonty/claude-trading-skills)의 스킬 패턴과 분석 프레임워크를 기반으로 한다.
원본 레포의 51개 스킬을 포팅하고, 한국 시장 지원(`bin/stock-cli` 듀얼 마켓), 예측 트랙 레코드(SQLite + 자기 개선 루프), Telegram 연동을 추가했다.
