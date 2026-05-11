---
name: stock-help
description: Show all available Stock Expectation commands — Claude Code skills, bin/stock-cli subcommands, and common workflows. Use when the user asks "what commands are available", "how do I use this", "show me skills", or 명령어 알려줘 / 도움말 / 어떤 스킬 있어 / 사용법 / 뭐 할 수 있어 / stock help.
---

# Stock Expectation — Command Reference

이 프로젝트가 제공하는 모든 명령을 한눈에 정리한 도움말. 더 자세한 워크플로는 `README.md`, `cron_setting.md`, `docs/HANDOFF.md` 참조.

## 가장 자주 쓰는 5개

| 명령 | 용도 |
|---|---|
| `/expect <ticker>` 또는 `/expect ALL` | **결정론적 BUY/SELL 추천** — 점수 테이블 + multi-horizon 예측 자동 등록 |
| `/daily-briefing` | 일일 시장 브리핑 (자동 cron도 매일 07:00 KR, 21:00 US) |
| `/prediction-review` | 내 예측 적중률 / 시그널별 성과 / 캘리브레이션 |
| `/portfolio-eval` | 포트폴리오 P&L, 리스크, 어드바이스 |
| `./bin/stock-cli price <ticker> --market US` | 터미널에서 빠른 가격 조회 (LLM 우회) |

---

## Claude Code 스킬 (대화형, `/명령` 또는 자연어로 호출)

### 1. 핵심 예측 흐름 (4)

- **`/expect`** — 결정론적 추천기. 모드:
  - `/expect NVDA` 또는 `/expect 삼성전자` — 단일 종목 깊은 분석
  - `/expect NVDA,AMD,AVGO` — 배치 (≤5개)
  - `/expect US` / `/expect KR` — 시장별 자동 발굴 5개
  - `/expect ALL` 또는 `/expect` — US 5 + KR 5 풀 스캔
- **`/daily-briefing`** — 시장 매크로 + 섹터 + 3-5개 종목 추천 (자동 cron에서도 호출)
- **`/stock-research`** — 5-signal 가중 분석 (`/expect`보다 narrative 중심)
- **`/prediction-review`** — 적중률 / Brier / 시그널 성과 / 과신 버킷

### 2. 포트폴리오 (5)

- **`/portfolio-eval`** — 4축 분석 (P&L / 리스크 / 예측 일치도 / 어드바이스)
- **`/portfolio-manager`** — Alpaca 브로커리지 연동 분석
- **`/position-sizer`** — 리스크 기반 매수 수량 계산 (Kelly, ATR 등)
- **`/toss-sync`** — Toss 증권 자동 동기화
- **`/trader-memory-core`** — 투자 thesis 생애주기 추적 (등록 → 진입 → 마감 → postmortem)

### 3. 매크로 / 시장 환경 (6)

- **`/macro-regime-detector`** — Concentration / Broadening / Contraction 레짐 분류
- **`/market-breadth-analyzer`** — 시장 폭 0-100 점수
- **`/market-top-detector`** — 천장 신호 (distribution days 등)
- **`/ftd-detector`** — Follow-Through Day (바닥 확인)
- **`/uptrend-analyzer`** — 5-component breadth dashboard
- **`/sector-analyst`** — 섹터 로테이션 사이클 위치

### 4. 스크리너 (5) — 다양한 종목 발굴

- **`/vcp-screener`** — Minervini VCP (Stage 2 momentum)
- **`/canslim-screener`** — O'Neil CANSLIM (성장주)
- **`/base-breakout-screener`** — 6M 베이스 + 거래량 200% 폭발
- **`/earnings-trade-analyzer`** — 어닝스 직후 5-factor 점수
- **`/finviz-screener`** — "P/E < 20, ROE > 15%" 같은 자연어 → FinViz URL 빌더

### 5. 캘린더 + 분석 (5)

- **`/earnings-calendar`** — FMP 어닝스 캘린더 (mid-cap+)
- **`/economic-calendar-fetcher`** — 중앙은행, 고용, CPI 등 매크로 이벤트
- **`/theme-detector`** — 테마/섹터 라이프사이클
- **`/technical-analyst`** — 주간 차트 기술적 분석 (이미지 업로드 가능)
- **`/stock-analysis`** — US 종합 펀더+테크니컬

### 6. 한국 시장 (1)

- **`/korean-market-analysis`** — KOSPI/KOSDAQ, 재벌, 환율 영향, 반도체 공급망 등 한국 특화

### 7. 메타 / 운영 (5)

- **`/backtest-expert`** — 백테스트 설계 가이드
- **`/data-quality-checker`** — 마켓 분석 문서 QA
- **`/signal-postmortem`** — 시그널 사후 분석
- **`/retrospect`** — 세션 회고 문서 생성
- **`/init`** — CLAUDE.md 초기화

### 8. 메타 (이 스킬)

- **`/stock-help`** — 이 문서

> **Archived 19 + Deleted 11**: Stage 3 정리 후 보관/삭제됨. 복원 방법은 `.claude/skills/_archived/README.md` 참조.

---

## `bin/stock-cli` — 터미널 직접 호출 (LLM 우회)

JSON으로만 출력. `jq`와 조합 가능.

### 시장 데이터

```bash
./bin/stock-cli price NVDA --market US --days 30
./bin/stock-cli price-batch AAPL,MSFT,NVDA --market US --days 30
./bin/stock-cli fundamentals AAPL --market US
./bin/stock-cli fundamentals-batch AAPL,MSFT --market US
./bin/stock-cli search "삼성" --market KR
./bin/stock-cli health   # provider 헬스체크
```

### Multi-horizon 지표 (/expect가 사용)

```bash
./bin/stock-cli horizon-metrics NVDA --market US --days 400
./bin/stock-cli horizon-metrics-batch NVDA,AMD,AVGO --market US --days 400
```

### 뉴스 + 공시

```bash
./bin/stock-cli news NVDA --market US --limit 5 --since-days 7
./bin/stock-cli news 005930 --market KR --limit 5 --since-days 7
./bin/stock-cli disclosure 005930 --since-days 14   # KR, Open DART
```

### 예측 관리

```bash
./bin/stock-cli predict create \
  --ticker NVDA --market US --direction BULL \
  --confidence 0.65 --timeframe 1W --entry-price 215.20 \
  --target-price 226.00 --stop-price 206.60 \
  --reasoning "..." --signals technical,news,momentum

./bin/stock-cli predict list --status OPEN
./bin/stock-cli predict detail <id>
./bin/stock-cli predict cancel <id>
./bin/stock-cli track-record --days 30
./bin/stock-cli calibration
```

### 포트폴리오

```bash
./bin/stock-cli portfolio create --market KR --name "Toss KR"
./bin/stock-cli portfolio buy 005930 --qty 10 --price 55000 --market KR
./bin/stock-cli portfolio sell 005930 --qty 5 --price 60000 --market KR --date 2026-04-01
./bin/stock-cli portfolio import trades.csv --market KR --dry-run
./bin/stock-cli portfolio positions --market KR
./bin/stock-cli portfolio report --market KR
./bin/stock-cli portfolio risk --market KR
./bin/stock-cli portfolio vs-predictions --market KR
./bin/stock-cli portfolio advice --market KR
```

### 옵션 모듈 (별도 `--extra` 설치 필요)

```bash
# Stage 7-A 메모리 (uv sync --extra memory)
./bin/stock-cli memory stats
./bin/stock-cli memory search "AI infrastructure" --category predictions --limit 5
./bin/stock-cli memory purge <id> --yes

# Stage 7-B 그래프 (uv sync --extra graph + docker compose up -d neo4j)
./bin/stock-cli graph init
./bin/stock-cli graph query "MATCH (n) RETURN count(n)"
./bin/stock-cli graph similar-stocks NVDA --limit 5
./bin/stock-cli graph theme-winners "AI"
```

---

## 자동화 (cron) — 본인이 손 안 대도 도는 것들

| 시각 (KST) | 작업 | 무엇이 일어남 |
|---|---|---|
| 평일 07:00 | KR daily_briefing | Telegram에 KR 시장 요약 + 1-3 예측 등록 |
| 평일 21:00 | US daily_briefing | Telegram에 US 시장 요약 + 1-3 예측 등록 |
| 매일 00:00 | outcome_tracker | OPEN 예측 평가 → HIT/MISS/EXPIRED, Telegram 알림 |
| 일요일 22:00 | weekly_calibration | `reports/weekly-calibration-*.md` 생성, 12주 트렌드 갱신 |

설정 상세: `cron_setting.md`. 변경: `crontab -e`. 끄기: `crontab -r`.

수동 실행:
```bash
uv run python scheduler/daily_briefing.py --market KR
uv run python scheduler/outcome_tracker.py
uv run python scheduler/weekly_calibration.py
```

---

## 시나리오별 권장 흐름

### A. 매일 5분 워크플로
1. 자동 cron이 07:00/21:00 Telegram briefing 발송 → 읽기
2. 출근/퇴근길에 `/prediction-review`로 적중률 확인
3. 관심 종목 떠오르면 `/expect <ticker>`

### B. 새로운 종목 발굴 (다양성 ↑)
1. `/vcp-screener` 또는 `/canslim-screener`로 후보 30~50개
2. 좁혀서 5개 → `/expect TICKER1,TICKER2,...`로 점수화
3. BUY 라벨 받으면 `/position-sizer`로 매수 수량 계산

### C. 매수 직전 매크로 점검
1. `/macro-regime-detector` — 어느 레짐?
2. `/market-top-detector` — 천장 신호 있는가?
3. `/ftd-detector` — 바닥 확인됐는가?
4. 시장 환경 OK → 매수 진행

### D. 주간 회고 (일요일 저녁)
1. 자동 weekly_calibration이 22:00에 리포트 생성
2. `reports/weekly-calibration-YYYY-MM-DD.md` 읽기
3. `/prediction-review`로 어떤 시그널이 잘 작동했는지 확인
4. 다음 주 가중치 조정

### E. 포트폴리오 관리
1. `/toss-sync`로 토스 증권 동기화 (또는 수동 `portfolio buy/sell`)
2. `/portfolio-eval`로 4축 분석
3. 추천 받으면 `/expect`로 추가 매수 후보 검증

---

## 학습 자료 (더 깊이 알고 싶을 때)

| 파일 | 내용 |
|---|---|
| `README.md` | 시스템 전체 개요 + 설치 + 시나리오 |
| `cron_setting.md` | 자동화 스케줄 설정 / 로그 / 트러블슈팅 |
| `docs/HANDOFF.md` | 스테이지별 변경 이력 + 결정 포인트 |
| `.claude/skills/expect/SKILL.md` | `/expect` 점수 테이블 (ALGO_SCORE/NEWS_SCORE) 전체 |
| `docs/stage-{1..7,4.1}/*.md` | 단계별 설계 결정 + 회고 |

---

## 도움이 되지 않을 때

- 특정 ticker 정보가 안 나옴 → `bin/stock-cli health`로 provider 상태 확인
- skill이 트리거 안 됨 → `/명령어` 직접 호출 또는 description의 키워드 사용
- 자동 cron이 안 도는 것 같음 → `tail ~/logs/stock-expectation/*.log`
- 새 환경에서 처음 셋업 → `README.md` "설치" 섹션 참조 (`uv sync` → `.env.example` 복사 → 키 입력)

질문 형식 자유. "어떻게 ~ 하지?" / "~ 명령어 뭐였더라?" / "내가 마지막 한 거 보여줘" 같은 자연어 모두 OK.
