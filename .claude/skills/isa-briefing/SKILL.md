---
name: isa-briefing
description: Monthly ISA contribution briefing for the long-term Korean ETF book. Values the ISA portfolio, checks drift vs the approved target allocation, optionally proposes a tilt (code-clamped to ±10%p), runs the sell-free contribution allocator, and composes a Korean briefing with per-ETF buy amounts, track record vs benchmarks, and the decision-log id. Triggers on keywords like isa, ISA 브리핑, 적립, 월 적립, 리밸런싱, ETF 적립, isa briefing, monthly contribution.
---

# ISA Monthly Briefing

장기 ISA ETF 적립 계좌의 월간 브리핑. 데이터와 실행은 전부 `bin/stock-cli isa ...`,
판단(틸트 여부)만 LLM — 틸트 클램프(±10%p)와 결정 로그는 코드가 강제한다.

## When to Use

- 매월 적립일 (자동 cron `scheduler/isa_briefing.py` 또는 수동 호출)
- 적립금 배분을 결정하고 실행 내역을 기록으로 남길 때
- 분기(1/4/7/10월) 리밸런스 밴드 점검

## Prerequisites

- `bin/stock-cli` 실행 가능 (uv-managed env)
- 승인된 목표 배분이 저장돼 있어야 함 (`isa init`, 최초 1회)
- KR 포트폴리오 이름 "ISA" (`portfolio create --market KR --name ISA`;
  거래 기록은 반드시 `--portfolio ISA`로)
- **이번 달 적립금은 항상 명시적 입력** — 프롬프트/인자로 주어진 금액만 사용하고,
  없으면 사용자에게 물어본다. 절대 임의 금액을 가정하지 않는다.

## Workflow

### 1. 현황 수집

```bash
bin/stock-cli isa status        # 목표 vs 현재 비중, 드리프트, 트랙레코드
bin/stock-cli isa rebalance     # 밴드(±5%p) 위반 + 무매도 교정 적립액
bin/stock-cli isa log --limit 5 # 최근 결정 로그
```

`status`가 `error`를 반환하면 (목표 없음 / ISA 포트폴리오 없음) 안내 메시지를
전달하고 중단한다.

### 2. 매크로 컨텍스트

```bash
bin/stock-cli macro-news --limit 10
```

리스크 레벨은 **컨텍스트일 뿐이다**. Stage-24의 RISK_OFF 스위치는 개별 종목의
신규 BULL 예측을 막는 게이트이며, **ISA 월 적립에는 적용되지 않는다** — 장기
DCA는 설계상 리스크오프 구간에도 계속한다. 리스크오프라면 이번 달을 건너뛰는
게 아니라 **틸트를 0으로 유지하는 근거**로 삼는다.

### 3. (선택) 동일 지수 내 더 싼 티커 점검

향후 매수분에 한해 같은 지수를 추종하는 더 싼 ETF로의 전환을 검토할 수 있다:

```bash
bin/stock-cli etf compare --query "미국 S&P500"
```

전환은 **앞으로의 매수 대상 변경**만 의미한다 — 기존 보유분 매도는 절대
권하지 않는다 (ISA 비과세/의무기간). 전환을 권하면 목표 재저장(`isa init`)이
필요하다고 안내한다.

### 4. 틸트 결정 (기본: 틸트 없음)

- 기본값은 **틸트 없음**이다. 틸트는 예외이며, 클래스당 2-3문장의 한국어 근거가
  있을 때만 제안한다.
- 제안 한도는 ±10%p — 그 이상 써도 CLI가 클램프한다. **CLI가 출력한 클램프
  결과가 최종이다.**

### 5. 적립 실행

```bash
bin/stock-cli isa allocate --amount <AMOUNT>                     # 틸트 없음
bin/stock-cli isa allocate --amount <AMOUNT> --tilt "overseas_equity=+5,bond=-5"
```

출력의 `per_etf` (종목별 매수액 + 예상 수량)와 `decision_id`를 브리핑에 그대로
인용한다. 미리보기만 필요하면 `--dry-run` (결정 로그 미기록).

### 6. 분기 리밸런스 섹션 (1/4/7/10월 실행 시)

`isa rebalance`의 밴드 위반과 `min_contribution_to_restore`(밴드를 복원하는
최소 추가 적립액)를 표로 보여준다. 교정 수단은 **추가 적립뿐** — 매도 리밸런스는
제시하지 않는다.

### 7. 한국어 브리핑 작성

순서대로:

1. **이번 달 적립 배분표** — 종목별 매수액 + 예상 수량 (`per_etf`)
2. **포트폴리오 현황** — 클래스별 비중 vs 목표, 드리프트 (`status`)
3. **트랙레코드** — 최근 스냅샷(`recent_snapshots`), 누적 수익률
   (`since_inception_return_pct`) vs 벤치마크 (S&P 500, KOSPI)
4. **매크로 코멘트** — 2-3문장, 틸트 결정과의 연결
5. **결정 로그 ID** — `decision_id` 명시

## Hard Rules

- **월 적립을 시장 내러티브로 건너뛰지 않는다.** 리스크오프 = 틸트 0, 절대
  "이번 달은 쉬자"가 아니다.
- 틸트는 코드에서 ±10%p로 클램프된다. 클램프된 결과에 이의를 달지 않는다.
- 리밸런스를 위한 **매도를 권하지 않는다** — 교정은 추가 적립으로만
  (ISA 비과세/의무기간).
- 모든 실행은 `isa allocate`/`isa rebalance`를 통해 결정 로그에 남는다.
  로그를 우회하는 수동 계산 지시를 하지 않는다.
- 적립금은 항상 명시적 입력이다. 임의 기본값 금지.
