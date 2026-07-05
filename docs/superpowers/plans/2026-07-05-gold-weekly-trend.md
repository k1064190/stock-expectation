# Weekly Gold Trend Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a weekly, mostly-deterministic scheduler job that scores KRX gold trend + macro conditions and emits a Korean ACCUMULATE/HOLD/PAUSE verdict to Telegram + a report file.

**Architecture:** One self-contained scheduler script (`scheduler/gold_trend.py`) of small pure scoring/render functions plus thin network fetchers and an orchestrator `main()`. Slow structural macro inputs live in `data/gold_macro_factors.yaml`; fast inputs (gold price, FX, real yield) are fetched live each run. An optional single `claude -p` call writes a Korean summary paragraph; everything else is deterministic. Follows the existing `scheduler/weekly_calibration.py` pattern.

**Tech Stack:** Python 3.11, `uv`, `pykrx` (KRX gold `411060`), `yfinance` (`GC=F`, `KRW=X`), `httpx` (FRED CSV), `pyyaml`, `pytest`, `scheduler/telegram_sender.py`, `claude -p` subprocess.

## Global Constraints

- Run everything via `uv run` (e.g., `uv run pytest`, `uv run python scheduler/gold_trend.py`).
- Fast unit tests MUST pass under `uv run pytest -m "not network"`; live-fetch tests are marked `@pytest.mark.network`.
- All wall-clock/scheduling is KST (`Asia/Seoul`); cron file already pins `TZ=Asia/Seoul`.
- **Fail-open:** no single fetch failure aborts the run; degraded inputs are labelled inline in the output.
- **Never write to `data/predictions.db`** — gold uses its own `state/gold_trend.json`.
- Work on branch `feature/gold-weekly-trend`; commit after every task (never on `master`).
- Scorecard dot rendering = `round(score / 20)` clamped 0–5.
- Grams per troy ounce constant = `31.1035`.
- Macro score weights, real-rate thresholds, and central-bank baseline come from yaml; scoring band breakpoints are in code.
- KRX gold uses ACE KRX금현물 ETF `411060` as a **trend proxy**; the per-gram figure in the position line is an **approximation** (`spot USD/oz × USD/KRW ÷ 31.1035`), not the literal KRX 금현물 trade price — label it as such.

---

### Task 1: Config loader + macro factors file + dependency

**Files:**
- Create: `data/gold_macro_factors.yaml`
- Create: `scheduler/gold_trend.py` (module skeleton + `DEFAULT_CONFIG`, `load_config`)
- Modify: `pyproject.toml` (ensure `pyyaml` in base `[project].dependencies`)
- Test: `tests/test_gold_trend.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `DEFAULT_CONFIG: dict` — built-in defaults matching the yaml.
  - `load_config(path: pathlib.Path) -> dict` — returns a normalized config: keys `central_bank{trailing_4q_tonnes:float, baseline_tonnes:float}`, `dollar{reserve_share_pct:float}`, `real_rate{supportive_below_pct:float, restrictive_above_pct:float, assumed_pct:float}`, `scoring{weights:{central_bank,real_rate,dollar,fx}}`, `risk_off:bool`, `position{grams:float, avg_cost_krw_per_g:float|None}`, `last_reviewed:str`. Missing keys fall back to `DEFAULT_CONFIG`; a missing file returns `DEFAULT_CONFIG` unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gold_trend.py
import pathlib
import textwrap
import pytest
from scheduler import gold_trend as gt


def test_load_config_missing_file_returns_defaults(tmp_path):
    cfg = gt.load_config(tmp_path / "does_not_exist.yaml")
    assert cfg == gt.DEFAULT_CONFIG


def test_load_config_fills_missing_keys(tmp_path):
    p = tmp_path / "factors.yaml"
    p.write_text(textwrap.dedent("""
        central_bank:
          trailing_4q_tonnes: 700
        risk_off: true
    """))
    cfg = gt.load_config(p)
    assert cfg["central_bank"]["trailing_4q_tonnes"] == 700
    # unspecified sub-key falls back to default
    assert cfg["central_bank"]["baseline_tonnes"] == gt.DEFAULT_CONFIG["central_bank"]["baseline_tonnes"]
    assert cfg["risk_off"] is True
    # untouched section falls back entirely
    assert cfg["scoring"]["weights"] == gt.DEFAULT_CONFIG["scoring"]["weights"]


def test_load_config_position_cost_defaults_none(tmp_path):
    cfg = gt.load_config(tmp_path / "nope.yaml")
    assert cfg["position"]["avg_cost_krw_per_g"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_gold_trend.py -k load_config -v`
Expected: FAIL — `ModuleNotFoundError` / `AttributeError: module 'scheduler.gold_trend' has no attribute 'load_config'`.

- [ ] **Step 3: Write minimal implementation**

```python
# scheduler/gold_trend.py
"""Weekly gold trend analysis job.

Deterministic KRX-gold technical scoring + macro scorecard (live FX/real-yield +
yaml config for slow structural factors) + one optional LLM summary paragraph.
Emits an ACCUMULATE/HOLD/PAUSE verdict to Telegram and a report file.
"""
from __future__ import annotations

import copy
import pathlib
from typing import Optional

import yaml

GRAMS_PER_OZ = 31.1035

DEFAULT_CONFIG: dict = {
    "last_reviewed": "2026-07-04",
    "central_bank": {"trailing_4q_tonnes": 950.0, "baseline_tonnes": 500.0},
    "dollar": {"reserve_share_pct": 58.0},
    "real_rate": {
        "supportive_below_pct": 1.0,
        "restrictive_above_pct": 2.0,
        "assumed_pct": 1.9,
    },
    "scoring": {
        "weights": {"central_bank": 0.35, "real_rate": 0.30, "dollar": 0.20, "fx": 0.15}
    },
    "risk_off": False,
    "position": {"grams": 2.0, "avg_cost_krw_per_g": None},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Return base deep-merged with override (override wins on leaves)."""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: pathlib.Path) -> dict:
    """Load the macro-factors yaml, filling any missing keys from DEFAULT_CONFIG.

    A missing file yields DEFAULT_CONFIG unchanged.
    """
    if not path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    loaded = yaml.safe_load(path.read_text()) or {}
    return _deep_merge(DEFAULT_CONFIG, loaded)
```

- [ ] **Step 4: Create the seed config file**

```yaml
# data/gold_macro_factors.yaml
# Slow structural macro inputs for the weekly gold trend job. Refresh quarterly.
# Seeded 2026-07-04 from deep-research (WGC / IMF / Fed / J.P. Morgan, 2026-07-03).
last_reviewed: 2026-07-04

central_bank:
  trailing_4q_tonnes: 950      # ~1,000t/yr structural since 2022 (2025 = 863t)
  baseline_tonnes: 500         # prior-decade average

dollar:
  reserve_share_pct: 58.0      # IMF COFER; flat since 2022

real_rate:
  supportive_below_pct: 1.0
  restrictive_above_pct: 2.0
  assumed_pct: 1.9             # fallback when FRED fetch fails

scoring:
  weights: { central_bank: 0.35, real_rate: 0.30, dollar: 0.20, fx: 0.15 }

risk_off: false                # manual kill-switch -> forces PAUSE

position:                      # optional personal holding; null cost -> P&L line omitted
  grams: 2.0
  avg_cost_krw_per_g: null     # set to your actual KRW/g buy price to enable P&L
```

- [ ] **Step 5: Ensure pyyaml is a base dependency**

In `pyproject.toml`, confirm `pyyaml` appears in `[project].dependencies` (add it if only under the `skills` optional-extra). Then:

Run: `uv sync`
Expected: resolves without error; `pyyaml` installed in the base env.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_gold_trend.py -k load_config -v`
Expected: PASS (3 tests).

- [ ] **Step 7: Commit**

```bash
git add scheduler/gold_trend.py data/gold_macro_factors.yaml pyproject.toml uv.lock tests/test_gold_trend.py
git commit -m "feat(gold): config loader + seeded macro factors for weekly gold job"
```

---

### Task 2: Technical scoring

**Files:**
- Modify: `scheduler/gold_trend.py`
- Test: `tests/test_gold_trend.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  - `_rsi(closes: list[float], period: int = 14) -> float`
  - `_sma(closes: list[float], n: int) -> Optional[float]`
  - `_trend_subscore(price, ma50, ma200, ma200_rising) -> float`
  - `_pullback_subscore(drawdown_pct: float) -> float`
  - `_momentum_subscore(rsi: float) -> float`
  - `compute_technical(closes: list[float]) -> dict` with keys `price, ma50, ma200, ma200_rising, rsi, drawdown_pct, trend, pullback, momentum, score, label`. `label` in {"양호","보통","비권장"}. `closes` is chronological (oldest→newest) daily closes.

- [ ] **Step 1: Write the failing test**

```python
def test_rsi_all_gains_is_100():
    closes = [float(i) for i in range(1, 40)]  # strictly increasing
    assert gt._rsi(closes) == 100.0


def test_sma_and_none_when_short():
    assert gt._sma([1, 2, 3, 4], 2) == 3.5
    assert gt._sma([1, 2], 5) is None


def test_pullback_subscore_bands():
    assert gt._pullback_subscore(-2) == 40    # near high
    assert gt._pullback_subscore(-12) == 100  # healthy zone
    assert gt._pullback_subscore(-27) == 70   # deep
    assert gt._pullback_subscore(-40) == 30   # broken


def test_momentum_subscore_bands():
    assert gt._momentum_subscore(25) == 100
    assert gt._momentum_subscore(40) == 80
    assert gt._momentum_subscore(50) == 60
    assert gt._momentum_subscore(65) == 40
    assert gt._momentum_subscore(80) == 15


def test_trend_subscore_uptrend_rising():
    assert gt._trend_subscore(110, 100, 90, True) == 100
    assert gt._trend_subscore(110, 100, 90, False) == 60
    assert gt._trend_subscore(80, 100, 90, False) == 30
    # degraded (no ma200)
    assert gt._trend_subscore(110, 100, None, False) == 60
    assert gt._trend_subscore(90, 100, None, False) == 30


def test_compute_technical_healthy_pullback_in_uptrend():
    # Rising series then a mild pullback -> above ma200, healthy drawdown, mid RSI.
    closes = [100 + i for i in range(260)]          # 100..359 rising
    closes += [359 * 0.90]                            # -10% pullback tick
    tech = gt.compute_technical(closes)
    assert tech["ma200"] is not None
    assert -20 < tech["drawdown_pct"] < -5
    assert 0 <= tech["score"] <= 100
    assert tech["label"] in {"양호", "보통", "비권장"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_gold_trend.py -k "rsi or sma or subscore or compute_technical" -v`
Expected: FAIL — attributes not defined.

- [ ] **Step 3: Write minimal implementation**

```python
def _sma(closes: list[float], n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas][-period:]
    losses = [max(-d, 0.0) for d in deltas][-period:]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _trend_subscore(price, ma50, ma200, ma200_rising) -> float:
    if ma200 is not None:
        if price >= ma200 and ma200_rising:
            return 100.0
        if price >= ma200:
            return 60.0
        return 30.0
    # degraded: no 200-day history
    return 60.0 if (ma50 is not None and price >= ma50) else 30.0


def _pullback_subscore(drawdown_pct: float) -> float:
    d = drawdown_pct
    if d > -5:
        return 40.0
    if d > -20:
        return 100.0
    if d > -35:
        return 70.0
    return 30.0


def _momentum_subscore(rsi: float) -> float:
    if rsi < 30:
        return 100.0
    if rsi < 45:
        return 80.0
    if rsi < 60:
        return 60.0
    if rsi <= 70:
        return 40.0
    return 15.0


def compute_technical(closes: list[float]) -> dict:
    price = closes[-1]
    ma50 = _sma(closes, 50)
    ma200 = _sma(closes, 200)
    # 200-SMA now vs 20 sessions ago (needs >= 220 points)
    ma200_prev = _sma(closes[:-20], 200) if len(closes) >= 220 else None
    ma200_rising = bool(ma200 is not None and ma200_prev is not None and ma200 > ma200_prev)
    window = closes[-252:] if len(closes) >= 252 else closes
    high = max(window)
    drawdown_pct = (price / high - 1.0) * 100.0 if high else 0.0
    rsi = _rsi(closes)
    trend = _trend_subscore(price, ma50, ma200, ma200_rising)
    pullback = _pullback_subscore(drawdown_pct)
    momentum = _momentum_subscore(rsi)
    score = 0.40 * trend + 0.30 * pullback + 0.30 * momentum
    label = "양호" if score >= 60 else ("보통" if score >= 40 else "비권장")
    return {
        "price": price, "ma50": ma50, "ma200": ma200, "ma200_rising": ma200_rising,
        "rsi": rsi, "drawdown_pct": drawdown_pct,
        "trend": trend, "pullback": pullback, "momentum": momentum,
        "score": score, "label": label,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gold_trend.py -k "rsi or sma or subscore or compute_technical" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scheduler/gold_trend.py tests/test_gold_trend.py
git commit -m "feat(gold): deterministic technical scoring (trend/pullback/momentum)"
```

---

### Task 3: Macro scoring

**Files:**
- Modify: `scheduler/gold_trend.py`
- Test: `tests/test_gold_trend.py`

**Interfaces:**
- Consumes: `DEFAULT_CONFIG`/config shape from Task 1.
- Produces:
  - `_central_bank_subscore(trailing_4q: float, baseline: float) -> float`
  - `_real_rate_subscore(y: float, supportive_below: float, restrictive_above: float) -> tuple[float, bool]` returns `(score, restrictive_flag)`
  - `_dollar_subscore(reserve_share_pct: float) -> float`
  - `_fx_subscore(usdkrw: float, usdkrw_ma200: float) -> float`
  - `compute_macro(config: dict, real_yield_pct: float, usdkrw: float, usdkrw_ma200: float) -> dict` with keys `central_bank, real_rate, dollar, fx, restrictive_flag, score, label`. `label` in {"높음","중립","낮음"}.

- [ ] **Step 1: Write the failing test**

```python
def test_central_bank_subscore_bands():
    assert gt._central_bank_subscore(950, 500) == 100
    assert gt._central_bank_subscore(700, 500) == 70
    assert gt._central_bank_subscore(550, 500) == 50
    assert gt._central_bank_subscore(400, 500) == 30


def test_real_rate_subscore_and_flag():
    assert gt._real_rate_subscore(0.8, 1.0, 2.0) == (100, False)
    assert gt._real_rate_subscore(1.5, 1.0, 2.0) == (60, False)
    assert gt._real_rate_subscore(2.3, 1.0, 2.0) == (25, True)


def test_dollar_subscore_bands():
    assert gt._dollar_subscore(53) == 70
    assert gt._dollar_subscore(58) == 55
    assert gt._dollar_subscore(62) == 40


def test_fx_subscore_double_edged():
    assert gt._fx_subscore(1200, 1000) == 30   # won very weak (+20%)
    assert gt._fx_subscore(1010, 1000) == 60   # near mean
    assert gt._fx_subscore(920, 1000) == 80    # won strong (-8%)


def test_compute_macro_seed_case():
    cfg = gt.load_config(pathlib.Path("does_not_exist"))  # DEFAULT_CONFIG
    m = gt.compute_macro(cfg, real_yield_pct=1.9, usdkrw=1544, usdkrw_ma200=1450)
    assert m["central_bank"] == 100          # 950 >= 900
    assert m["real_rate"] == 60              # between thresholds
    assert m["restrictive_flag"] is False
    assert m["dollar"] == 55                 # 58 -> 55
    assert m["fx"] == 30                     # 1544/1450 = +6.5% -> weak
    # 0.35*100 + 0.30*60 + 0.20*55 + 0.15*30 = 68.5
    assert round(m["score"], 1) == 68.5
    assert m["label"] == "높음"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_gold_trend.py -k "central_bank or real_rate or dollar_subscore or fx_subscore or compute_macro" -v`
Expected: FAIL — attributes not defined.

- [ ] **Step 3: Write minimal implementation**

```python
def _central_bank_subscore(trailing_4q: float, baseline: float) -> float:
    if trailing_4q >= 900:
        return 100.0
    if trailing_4q >= 650:
        return 70.0
    if trailing_4q >= baseline:
        return 50.0
    return 30.0


def _real_rate_subscore(y: float, supportive_below: float, restrictive_above: float):
    if y <= supportive_below:
        return (100.0, False)
    if y >= restrictive_above:
        return (25.0, True)
    return (60.0, False)


def _dollar_subscore(reserve_share_pct: float) -> float:
    if reserve_share_pct < 55:
        return 70.0
    if reserve_share_pct <= 60:
        return 55.0
    return 40.0


def _fx_subscore(usdkrw: float, usdkrw_ma200: float) -> float:
    pct = (usdkrw / usdkrw_ma200 - 1.0) * 100.0
    if pct > 5:
        return 30.0
    if pct >= -5:
        return 60.0
    return 80.0


def compute_macro(config: dict, real_yield_pct: float, usdkrw: float, usdkrw_ma200: float) -> dict:
    cb = _central_bank_subscore(
        config["central_bank"]["trailing_4q_tonnes"],
        config["central_bank"]["baseline_tonnes"],
    )
    rr, restrictive = _real_rate_subscore(
        real_yield_pct,
        config["real_rate"]["supportive_below_pct"],
        config["real_rate"]["restrictive_above_pct"],
    )
    dl = _dollar_subscore(config["dollar"]["reserve_share_pct"])
    fx = _fx_subscore(usdkrw, usdkrw_ma200)
    w = config["scoring"]["weights"]
    score = w["central_bank"] * cb + w["real_rate"] * rr + w["dollar"] * dl + w["fx"] * fx
    label = "높음" if score >= 65 else ("중립" if score >= 45 else "낮음")
    return {
        "central_bank": cb, "real_rate": rr, "dollar": dl, "fx": fx,
        "restrictive_flag": restrictive, "score": score, "label": label,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gold_trend.py -k "central_bank or real_rate or dollar_subscore or fx_subscore or compute_macro" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scheduler/gold_trend.py tests/test_gold_trend.py
git commit -m "feat(gold): macro scorecard scoring (cb/real-rate/dollar/fx)"
```

---

### Task 4: Verdict decision

**Files:**
- Modify: `scheduler/gold_trend.py`
- Test: `tests/test_gold_trend.py`

**Interfaces:**
- Consumes: `compute_technical` output (Task 2), `compute_macro` output (Task 3), config (Task 1).
- Produces:
  - `decide_verdict(technical: dict, macro: dict, config: dict) -> dict` with keys `verdict` in {"ACCUMULATE","HOLD","PAUSE"}, `emoji` in {"🟢","🟡","🔴"}, `reasons: list[str]`, `aggressive: bool`.

- [ ] **Step 1: Write the failing test**

```python
def _tech(score=60, rsi=47):
    return {"score": score, "rsi": rsi}


def _macro(score=68, restrictive_flag=False):
    return {"score": score, "restrictive_flag": restrictive_flag}


def test_verdict_accumulate():
    v = gt.decide_verdict(_tech(60, 47), _macro(68), gt.DEFAULT_CONFIG)
    assert v["verdict"] == "ACCUMULATE"
    assert v["emoji"] == "🟢"
    assert v["aggressive"] is False


def test_verdict_accumulate_aggressive():
    v = gt.decide_verdict(_tech(78, 28), _macro(70), gt.DEFAULT_CONFIG)
    assert v["verdict"] == "ACCUMULATE"
    assert v["aggressive"] is True


def test_verdict_pause_on_overbought_rsi():
    v = gt.decide_verdict(_tech(80, 78), _macro(70), gt.DEFAULT_CONFIG)
    assert v["verdict"] == "PAUSE"
    assert any("RSI" in r for r in v["reasons"])


def test_verdict_pause_on_restrictive_real_rate():
    v = gt.decide_verdict(_tech(60, 47), _macro(70, restrictive_flag=True), gt.DEFAULT_CONFIG)
    assert v["verdict"] == "PAUSE"


def test_verdict_pause_on_risk_off_switch():
    cfg = gt.load_config(pathlib.Path("nope"))
    cfg["risk_off"] = True
    v = gt.decide_verdict(_tech(60, 47), _macro(70), cfg)
    assert v["verdict"] == "PAUSE"


def test_verdict_hold_when_mixed():
    v = gt.decide_verdict(_tech(45, 55), _macro(50), gt.DEFAULT_CONFIG)
    assert v["verdict"] == "HOLD"
    assert v["emoji"] == "🟡"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_gold_trend.py -k verdict -v`
Expected: FAIL — `decide_verdict` not defined.

- [ ] **Step 3: Write minimal implementation**

```python
def decide_verdict(technical: dict, macro: dict, config: dict) -> dict:
    reasons: list[str] = []
    if technical["rsi"] > 75:
        reasons.append("RSI 과열(>75)")
    if macro["restrictive_flag"]:
        reasons.append("실질금리 긴축 전환")
    if config.get("risk_off"):
        reasons.append("매크로 risk-off 스위치")
    if reasons:
        return {"verdict": "PAUSE", "emoji": "🔴", "reasons": reasons, "aggressive": False}

    if macro["score"] >= 55 and technical["score"] >= 50:
        aggressive = macro["score"] >= 65 and technical["score"] >= 75
        return {"verdict": "ACCUMULATE", "emoji": "🟢",
                "reasons": ["장기 편향 우호 + 진입 양호"], "aggressive": aggressive}

    return {"verdict": "HOLD", "emoji": "🟡", "reasons": ["편향/타이밍 혼조"], "aggressive": False}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gold_trend.py -k verdict -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scheduler/gold_trend.py tests/test_gold_trend.py
git commit -m "feat(gold): ACCUMULATE/HOLD/PAUSE verdict with hard flags"
```

---

### Task 5: Rendering — dots, scorecard, position line, report

**Files:**
- Modify: `scheduler/gold_trend.py`
- Test: `tests/test_gold_trend.py`

**Interfaces:**
- Consumes: technical (Task 2), macro (Task 3), verdict (Task 4), config (Task 1).
- Produces:
  - `dots(score: float) -> str` — 5-char `●`/`○` bar.
  - `position_line(config: dict, usd_gold_per_oz: float, usdkrw: float) -> Optional[str]` — `None` when `avg_cost_krw_per_g` is `None`.
  - `scorecard_lines(macro: dict, config: dict, real_yield_pct: float, real_yield_estimated: bool, usdkrw: float) -> list[str]`
  - `render_report(ctx: dict) -> str` — full Korean markdown. `ctx` keys: `date_kst:str, verdict:dict, technical:dict, macro:dict, krx{price,drawdown_pct,rsi,ma200_gap_pct}, decomp{usd_ret_3m,krw_ret_3m}|None, real_yield{pct,estimated}, usdkrw, scorecard:list[str], position_line:str|None, deltas:dict|None, summary:str|None, degraded:list[str], config:dict`.

- [ ] **Step 1: Write the failing test**

```python
def test_dots_rounding():
    assert gt.dots(100) == "●●●●●"
    assert gt.dots(60) == "●●●○○"
    assert gt.dots(55) == "●●●○○"   # 2.75 -> 3
    assert gt.dots(30) == "●●○○○"   # 1.5 -> 2
    assert gt.dots(0) == "○○○○○"


def test_position_line_omitted_when_no_cost():
    cfg = gt.load_config(pathlib.Path("nope"))  # avg_cost None
    assert gt.position_line(cfg, usd_gold_per_oz=4100, usdkrw=1544) is None


def test_position_line_computes_approx_pnl():
    cfg = gt.load_config(pathlib.Path("nope"))
    cfg["position"] = {"grams": 2.0, "avg_cost_krw_per_g": 150000}
    line = gt.position_line(cfg, usd_gold_per_oz=4100, usdkrw=1544)
    # krw/g = 4100 * 1544 / 31.1035 = 203,510 -> profit
    assert "2" in line and "%" in line and "근사" in line


def test_render_report_contains_verdict_and_scores():
    ctx = {
        "date_kst": "2026-07-05",
        "verdict": {"verdict": "ACCUMULATE", "emoji": "🟢", "reasons": ["x"], "aggressive": False},
        "technical": {"score": 61, "label": "양호", "rsi": 47},
        "macro": {"score": 68, "label": "높음"},
        "krx": {"price": 28740, "drawdown_pct": -24.1, "rsi": 47, "ma200_gap_pct": -3.0},
        "decomp": {"usd_ret_3m": -13.6, "krw_ret_3m": -9.0},
        "real_yield": {"pct": 1.9, "estimated": False},
        "usdkrw": 1544,
        "scorecard": ["  중앙은행 매수   ●●●●●  구조적 강세"],
        "position_line": None,
        "deltas": {"macro": 3, "technical": -4},
        "summary": None,
        "degraded": [],
        "config": gt.DEFAULT_CONFIG,
    }
    md = gt.render_report(ctx)
    assert "ACCUMULATE" in md
    assert "macro 68" in md
    assert "technical 61" in md
    assert "28,740" in md


def test_render_report_flags_degraded_and_missing_summary():
    ctx = {
        "date_kst": "2026-07-05",
        "verdict": {"verdict": "HOLD", "emoji": "🟡", "reasons": ["x"], "aggressive": False},
        "technical": {"score": 50, "label": "보통", "rsi": 55},
        "macro": {"score": 50, "label": "중립"},
        "krx": {"price": 28000, "drawdown_pct": -10.0, "rsi": 55, "ma200_gap_pct": 1.0},
        "decomp": None,
        "real_yield": {"pct": 1.9, "estimated": True},
        "usdkrw": 1500,
        "scorecard": ["x"],
        "position_line": None,
        "deltas": None,
        "summary": None,
        "degraded": ["USD 금(GC=F) 조회 실패 → 달러/원화 분해 생략"],
        "config": gt.DEFAULT_CONFIG,
    }
    md = gt.render_report(ctx)
    assert "estimated" in md.lower() or "추정" in md
    assert "요약 생략" in md
    assert "조회 실패" in md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_gold_trend.py -k "dots or position_line or render_report" -v`
Expected: FAIL — attributes not defined.

- [ ] **Step 3: Write minimal implementation**

```python
def dots(score: float) -> str:
    n = max(0, min(5, round(score / 20)))
    return "●" * n + "○" * (5 - n)


def position_line(config: dict, usd_gold_per_oz: float, usdkrw: float) -> Optional[str]:
    pos = config.get("position", {})
    cost = pos.get("avg_cost_krw_per_g")
    if cost is None:
        return None
    grams = pos.get("grams", 0.0)
    krw_per_g = usd_gold_per_oz * usdkrw / GRAMS_PER_OZ
    pnl_pct = (krw_per_g / cost - 1.0) * 100.0
    value = krw_per_g * grams
    return (f"내 포지션: {grams:g}g @ {cost:,.0f}원 · 현재 ≈{krw_per_g:,.0f}원/g · "
            f"평가 {pnl_pct:+.1f}% (≈{value:,.0f}원, spot×환 근사)")


def scorecard_lines(macro, config, real_yield_pct, real_yield_estimated, usdkrw) -> list[str]:
    est = " (추정)" if real_yield_estimated else ""
    cb_t = config["central_bank"]["trailing_4q_tonnes"]
    share = config["dollar"]["reserve_share_pct"]
    return [
        f"  중앙은행 매수   {dots(macro['central_bank'])}  구조적(연 ~{cb_t:.0f}t, {macro['central_bank']:.0f})",
        f"  실질금리 방아쇠 {dots(macro['real_rate'])}  DFII10 {real_yield_pct:.1f}%{est} ({macro['real_rate']:.0f})",
        f"  달러 신뢰도     {dots(macro['dollar'])}  준비자산 {share:.0f}% ({macro['dollar']:.0f})",
        f"  환(원/달러)     {dots(macro['fx'])}  {usdkrw:,.0f} ({macro['fx']:.0f})",
    ]


def _delta_str(deltas: Optional[dict], key: str) -> str:
    if not deltas or key not in deltas:
        return ""
    return f", 지난주 {deltas[key]:+.0f}"


def render_report(ctx: dict) -> str:
    v = ctx["verdict"]
    t = ctx["technical"]
    m = ctx["macro"]
    krx = ctx["krx"]
    lines = [
        f"[금 주간 분석] {ctx['date_kst']} (KST)",
        f"판정: {v['emoji']} {v['verdict']}"
        + ("  — 적극 분할" if v.get("aggressive") else "")
        + (f"  · {', '.join(v['reasons'])}" if v.get("reasons") else ""),
        "",
        f"장기 상승 편향: {m['label']} (macro {m['score']:.0f}/100{_delta_str(ctx.get('deltas'), 'macro')})",
        f"이번 주 진입:  {t['label']} (technical {t['score']:.0f}/100{_delta_str(ctx.get('deltas'), 'technical')})",
        "",
        f"▸ KRX 금현물(411060): {krx['price']:,.0f}원 | 고점대비 {krx['drawdown_pct']:+.1f}% | "
        f"RSI {krx['rsi']:.0f} | MA200 {krx['ma200_gap_pct']:+.1f}%",
    ]
    if ctx.get("decomp"):
        d = ctx["decomp"]
        lines.append(
            f"▸ 달러금 3M {d['usd_ret_3m']:+.1f}% vs KRW금 3M {d['krw_ret_3m']:+.1f}%"
        )
    lines.append("▸ 매크로 스코어카드")
    lines.extend(ctx["scorecard"])
    if ctx.get("real_yield", {}).get("estimated"):
        lines.append("  (실질금리 estimated — FRED 조회 실패, 설정값 사용)")
    if ctx.get("position_line"):
        lines.append(f"▸ {ctx['position_line']}")
    for d in ctx.get("degraded", []):
        lines.append(f"⚠ {d}")
    if ctx.get("summary"):
        lines.append(f"▸ 요약: {ctx['summary']}")
    else:
        lines.append("▸ 요약 생략: LLM 호출 실패 또는 비활성")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gold_trend.py -k "dots or position_line or render_report" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scheduler/gold_trend.py tests/test_gold_trend.py
git commit -m "feat(gold): report rendering (dots/scorecard/position/degraded)"
```

---

### Task 6: State roll + week-over-week deltas

**Files:**
- Modify: `scheduler/gold_trend.py`
- Test: `tests/test_gold_trend.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  - `load_state(path: pathlib.Path) -> list[dict]` — `[]` when missing/unparseable.
  - `roll_state(state: list[dict], entry: dict, cap: int = 12) -> list[dict]` — appends entry, keeps last `cap`.
  - `save_state(path: pathlib.Path, state: list[dict]) -> None`.
  - `compute_deltas(entry: dict, prev: Optional[dict]) -> Optional[dict]` — `{"macro": float, "technical": float}` deltas vs prev, `None` when no prev. Entry dicts carry at least `date, verdict, macro_score, technical_score`.

- [ ] **Step 1: Write the failing test**

```python
import json


def test_load_state_missing_returns_empty(tmp_path):
    assert gt.load_state(tmp_path / "none.json") == []


def test_roll_state_caps_at_12():
    state = [{"date": f"d{i}", "macro_score": i, "technical_score": i} for i in range(12)]
    entry = {"date": "d12", "macro_score": 99, "technical_score": 99}
    rolled = gt.roll_state(state, entry, cap=12)
    assert len(rolled) == 12
    assert rolled[-1]["date"] == "d12"
    assert rolled[0]["date"] == "d1"   # oldest dropped


def test_compute_deltas():
    prev = {"macro_score": 65, "technical_score": 65}
    entry = {"macro_score": 68, "technical_score": 61}
    d = gt.compute_deltas(entry, prev)
    assert d == {"macro": 3, "technical": -4}
    assert gt.compute_deltas(entry, None) is None


def test_save_and_load_roundtrip(tmp_path):
    p = tmp_path / "gold_trend.json"
    state = [{"date": "d1", "macro_score": 50, "technical_score": 50}]
    gt.save_state(p, state)
    assert gt.load_state(p) == state
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_gold_trend.py -k "state or deltas" -v`
Expected: FAIL — attributes not defined.

- [ ] **Step 3: Write minimal implementation**

```python
import json


def load_state(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (ValueError, OSError):
        return []


def roll_state(state: list[dict], entry: dict, cap: int = 12) -> list[dict]:
    return (list(state) + [entry])[-cap:]


def save_state(path: pathlib.Path, state: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def compute_deltas(entry: dict, prev: Optional[dict]) -> Optional[dict]:
    if not prev:
        return None
    return {
        "macro": round(entry["macro_score"] - prev["macro_score"]),
        "technical": round(entry["technical_score"] - prev["technical_score"]),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gold_trend.py -k "state or deltas" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scheduler/gold_trend.py tests/test_gold_trend.py
git commit -m "feat(gold): 12-week rolling state + week-over-week deltas"
```

---

### Task 7: Live fetchers + LLM summary (fail-open)

**Files:**
- Modify: `scheduler/gold_trend.py`
- Test: `tests/test_gold_trend.py`

**Interfaces:**
- Consumes: config (Task 1), `_sma` (Task 2).
- Produces (each fetcher never raises; returns a sentinel on failure):
  - `fetch_krx_gold_closes(days: int = 430) -> list[float]` — chronological closes for `411060` via `pykrx`; `[]` on failure.
  - `fetch_usd_gold() -> Optional[dict]` — `{"per_oz": float, "ret_3m": float, "ret_6m": float}` from `GC=F` (yfinance 1y); `None` on failure.
  - `fetch_usdkrw() -> Optional[dict]` — `{"last": float, "ma200": float}` from `KRW=X`; `None` on failure.
  - `fetch_real_yield(config: dict) -> tuple[float, bool]` — `(pct, estimated)` from FRED CSV `DFII10`; `(config["real_rate"]["assumed_pct"], True)` on failure.
  - `run_llm_summary(prompt: str, mode: str) -> Optional[str]` — `claude -p` subprocess; `None` on failure/timeout or when `mode == "none"`.
  - `build_llm_prompt(ctx: dict) -> str` — instructs a 2–3 sentence Korean summary grounded ONLY in the provided numbers.

- [ ] **Step 1: Write the failing test** (fail-open paths only; live paths are network-marked)

```python
def test_fetch_real_yield_falls_back_when_fetch_fails(monkeypatch):
    cfg = gt.load_config(pathlib.Path("nope"))

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(gt.httpx, "get", boom)
    pct, estimated = gt.fetch_real_yield(cfg)
    assert pct == cfg["real_rate"]["assumed_pct"]
    assert estimated is True


def test_run_llm_summary_none_mode_returns_none():
    assert gt.run_llm_summary("anything", mode="none") is None


def test_run_llm_summary_failopen_on_subprocess_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("no claude binary")

    monkeypatch.setattr(gt.subprocess, "run", boom)
    assert gt.run_llm_summary("prompt", mode="claude-code") is None


def test_build_llm_prompt_grounds_on_numbers():
    ctx = {"verdict": {"verdict": "ACCUMULATE"}, "macro": {"score": 68},
           "technical": {"score": 61}, "krx": {"drawdown_pct": -24.1, "rsi": 47}}
    prompt = gt.build_llm_prompt(ctx)
    assert "68" in prompt and "61" in prompt
    assert "한국어" in prompt


@pytest.mark.network
def test_fetch_krx_gold_live():
    closes = gt.fetch_krx_gold_closes(days=60)
    assert isinstance(closes, list) and len(closes) > 10


@pytest.mark.network
def test_fetch_usd_gold_and_fx_live():
    assert gt.fetch_usd_gold()["per_oz"] > 0
    assert gt.fetch_usdkrw()["last"] > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_gold_trend.py -k "fetch_real_yield or run_llm_summary or build_llm_prompt" -v`
Expected: FAIL — attributes not defined.

- [ ] **Step 3: Write minimal implementation**

```python
import subprocess
from datetime import datetime, timedelta

import httpx

FRED_DFII10_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFII10"


def fetch_krx_gold_closes(days: int = 430) -> list[float]:
    try:
        from pykrx import stock as krx_stock
        end = datetime.now()
        start = end - timedelta(days=days)
        df = krx_stock.get_market_ohlcv_by_date(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "411060"
        )
        if df is None or df.empty:
            return []
        closes = [float(c) for c in df["종가"].tolist() if c and float(c) > 0]
        return closes
    except Exception:
        return []


def _returns_from_series(closes: list[float]) -> tuple[float, float]:
    last = closes[-1]
    r3 = (last / closes[-63] - 1.0) * 100.0 if len(closes) > 63 else float("nan")
    r6 = (last / closes[-126] - 1.0) * 100.0 if len(closes) > 126 else float("nan")
    return r3, r6


def fetch_usd_gold() -> Optional[dict]:
    try:
        import yfinance as yf
        h = yf.Ticker("GC=F").history(period="1y")["Close"].dropna().tolist()
        if len(h) < 5:
            return None
        r3, r6 = _returns_from_series(h)
        return {"per_oz": float(h[-1]), "ret_3m": r3, "ret_6m": r6}
    except Exception:
        return None


def fetch_usdkrw() -> Optional[dict]:
    try:
        import yfinance as yf
        h = yf.Ticker("KRW=X").history(period="1y")["Close"].dropna().tolist()
        if len(h) < 5:
            return None
        ma200 = _sma(h, 200) or (sum(h) / len(h))
        return {"last": float(h[-1]), "ma200": float(ma200)}
    except Exception:
        return None


def fetch_real_yield(config: dict) -> tuple[float, bool]:
    try:
        resp = httpx.get(FRED_DFII10_CSV, timeout=15.0)
        resp.raise_for_status()
        rows = [r for r in resp.text.strip().splitlines()[1:] if r]
        for row in reversed(rows):
            parts = row.split(",")
            if len(parts) >= 2 and parts[-1] not in (".", ""):
                return (float(parts[-1]), False)
        raise ValueError("no numeric DFII10 rows")
    except Exception:
        return (float(config["real_rate"]["assumed_pct"]), True)


def build_llm_prompt(ctx: dict) -> str:
    return (
        "다음 금 분석 수치에만 근거해 한국어로 2~3문장 요약을 작성해. "
        "새로운 사실·숫자를 지어내지 말 것.\n"
        f"- 판정: {ctx['verdict']['verdict']}\n"
        f"- 장기 편향(macro): {ctx['macro']['score']:.0f}/100\n"
        f"- 진입(technical): {ctx['technical']['score']:.0f}/100\n"
        f"- 고점대비: {ctx['krx']['drawdown_pct']:+.1f}%, RSI {ctx['krx']['rsi']:.0f}\n"
        "요약:"
    )


def run_llm_summary(prompt: str, mode: str) -> Optional[str]:
    if mode == "none":
        return None
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            return None
        out = proc.stdout.strip()
        return out or None
    except Exception:
        return None
```

- [ ] **Step 4: Run fail-open tests to verify they pass**

Run: `uv run pytest tests/test_gold_trend.py -k "fetch_real_yield or run_llm_summary or build_llm_prompt" -v`
Expected: PASS.

- [ ] **Step 5: (Optional, requires network) run live fetch tests**

Run: `uv run pytest tests/test_gold_trend.py -k "live" -m network -v`
Expected: PASS when network + markets available (skip otherwise).

- [ ] **Step 6: Commit**

```bash
git add scheduler/gold_trend.py tests/test_gold_trend.py
git commit -m "feat(gold): fail-open live fetchers (KRX/GC=F/FX/FRED) + LLM summary"
```

---

### Task 8: Orchestration — build_context, main/CLI, cron, docs

**Files:**
- Modify: `scheduler/gold_trend.py`
- Modify: `scheduler/crontab.example`
- Modify: `CLAUDE.md`
- Test: `tests/test_gold_trend.py`

**Interfaces:**
- Consumes: every prior task.
- Produces:
  - `build_context(closes: list[float], usd_gold: Optional[dict], usdkrw: Optional[dict], real_yield: tuple[float,bool], config: dict, prev_entry: Optional[dict], date_kst: str) -> dict` — pure assembler returning the `ctx` dict consumed by `render_report`, plus a `state_entry` sub-dict.
  - `main(argv: Optional[list[str]] = None) -> int` — CLI: `--config PATH`, `--llm-mode {claude-code,none}` (default `claude-code`), `--no-telegram`, `--state PATH`, `--reports-dir PATH`.

- [ ] **Step 1: Write the failing test**

```python
def test_build_context_assembles_and_flags_degraded():
    closes = [100 + i for i in range(260)] + [359 * 0.90]
    cfg = gt.load_config(pathlib.Path("nope"))
    ctx = gt.build_context(
        closes=closes,
        usd_gold=None,                       # forces degraded + no decomp
        usdkrw={"last": 1544, "ma200": 1450},
        real_yield=(1.9, False),
        config=cfg,
        prev_entry={"macro_score": 65, "technical_score": 65},
        date_kst="2026-07-05",
    )
    assert ctx["decomp"] is None
    assert any("GC=F" in d or "USD" in d for d in ctx["degraded"])
    assert ctx["deltas"] is not None
    assert "state_entry" in ctx
    assert ctx["state_entry"]["date"] == "2026-07-05"


def test_main_writes_report_offline(tmp_path, monkeypatch):
    # Force deterministic offline run: KRX from stub, no USD/FX network, LLM off.
    closes = [100 + i for i in range(260)] + [359 * 0.90]
    monkeypatch.setattr(gt, "fetch_krx_gold_closes", lambda days=430: closes)
    monkeypatch.setattr(gt, "fetch_usd_gold", lambda: {"per_oz": 4100, "ret_3m": -13.6, "ret_6m": -5.2})
    monkeypatch.setattr(gt, "fetch_usdkrw", lambda: {"last": 1544, "ma200": 1450})
    monkeypatch.setattr(gt, "fetch_real_yield", lambda cfg: (1.9, False))
    reports = tmp_path / "reports"
    state = tmp_path / "state" / "gold_trend.json"
    rc = gt.main([
        "--config", "data/gold_macro_factors.yaml",
        "--llm-mode", "none", "--no-telegram",
        "--reports-dir", str(reports), "--state", str(state),
    ])
    assert rc == 0
    written = list(reports.glob("gold-trend-*.md"))
    assert len(written) == 1
    body = written[0].read_text()
    assert "[금 주간 분석]" in body
    assert state.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_gold_trend.py -k "build_context or main_writes_report" -v`
Expected: FAIL — `build_context` / `main` not defined.

- [ ] **Step 3: Write minimal implementation**

```python
import argparse


def build_context(closes, usd_gold, usdkrw, real_yield, config, prev_entry, date_kst) -> dict:
    degraded: list[str] = []
    technical = compute_technical(closes) if closes else {
        "score": 0, "label": "비권장", "rsi": 50, "price": 0,
        "drawdown_pct": 0, "ma200": None,
    }
    if not closes:
        degraded.append("KRX 금현물(411060) 조회 실패 → 기술점수 0 처리")

    ry_pct, ry_est = real_yield
    fx_last = usdkrw["last"] if usdkrw else 0.0
    fx_ma200 = usdkrw["ma200"] if usdkrw else 1.0
    if usdkrw is None:
        degraded.append("USD/KRW(KRW=X) 조회 실패 → 환 요인 중립 처리")
        fx_last, fx_ma200 = 1.0, 1.0

    macro = compute_macro(config, ry_pct, fx_last or 1.0, fx_ma200 or 1.0)
    verdict = decide_verdict(technical, macro, config)

    decomp = None
    if usd_gold and closes and len(closes) > 63:
        krw_r3 = (closes[-1] / closes[-63] - 1.0) * 100.0
        decomp = {"usd_ret_3m": usd_gold["ret_3m"], "krw_ret_3m": krw_r3}
    if usd_gold is None:
        degraded.append("USD 금(GC=F) 조회 실패 → 달러/원화 분해·포지션 근사 생략")

    ma200 = technical.get("ma200")
    ma200_gap = ((technical["price"] / ma200 - 1.0) * 100.0) if ma200 else 0.0
    krx = {
        "price": technical["price"], "drawdown_pct": technical["drawdown_pct"],
        "rsi": technical["rsi"], "ma200_gap_pct": ma200_gap,
    }

    pos_line = None
    if usd_gold and usdkrw:
        pos_line = position_line(config, usd_gold["per_oz"], usdkrw["last"])

    entry = {
        "date": date_kst, "verdict": verdict["verdict"],
        "macro_score": round(macro["score"], 1),
        "technical_score": round(technical["score"], 1),
        "drawdown_pct": round(technical["drawdown_pct"], 1),
    }
    deltas = compute_deltas(entry, prev_entry)

    return {
        "date_kst": date_kst, "verdict": verdict, "technical": technical, "macro": macro,
        "krx": krx, "decomp": decomp, "real_yield": {"pct": ry_pct, "estimated": ry_est},
        "usdkrw": fx_last,
        "scorecard": scorecard_lines(macro, config, ry_pct, ry_est, fx_last),
        "position_line": pos_line, "deltas": deltas, "summary": None,
        "degraded": degraded, "config": config, "state_entry": entry,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Weekly gold trend analysis")
    parser.add_argument("--config", default="data/gold_macro_factors.yaml")
    parser.add_argument("--llm-mode", choices=["claude-code", "none"], default="claude-code")
    parser.add_argument("--no-telegram", action="store_true")
    parser.add_argument("--state", default="state/gold_trend.json")
    parser.add_argument("--reports-dir", default="reports")
    args = parser.parse_args(argv)

    config = load_config(pathlib.Path(args.config))
    state_path = pathlib.Path(args.state)
    state = load_state(state_path)
    prev_entry = state[-1] if state else None
    date_kst = datetime.now().strftime("%Y-%m-%d")

    ctx = build_context(
        closes=fetch_krx_gold_closes(),
        usd_gold=fetch_usd_gold(),
        usdkrw=fetch_usdkrw(),
        real_yield=fetch_real_yield(config),
        config=config,
        prev_entry=prev_entry,
        date_kst=date_kst,
    )
    ctx["summary"] = run_llm_summary(build_llm_prompt(ctx), args.llm_mode)

    report = render_report(ctx)

    reports_dir = pathlib.Path(args.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / f"gold-trend-{date_kst}.md").write_text(report)

    save_state(state_path, roll_state(state, ctx["state_entry"]))

    if not args.no_telegram:
        try:
            from scheduler.telegram_sender import send_briefing
            send_briefing(report, title="금 주간 분석")
        except Exception as e:  # fail-open: report already on disk
            print(f"[gold_trend] telegram send failed (non-fatal): {e}")

    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gold_trend.py -k "build_context or main_writes_report" -v`
Expected: PASS.

- [ ] **Step 5: Run the full gold test module (not network)**

Run: `uv run pytest tests/test_gold_trend.py -m "not network" -v`
Expected: PASS (all deterministic tests green).

- [ ] **Step 6: Add the cron line**

In `scheduler/crontab.example`, after the weekly calibration block, add:

```
# Weekly gold trend analysis — Sunday 21:00 KST (before the 22:00 calibration).
# Deterministic KRX-gold technical + macro scorecard + one LLM summary paragraph.
# Emits ACCUMULATE/HOLD/PAUSE to Telegram + reports/gold-trend-YYYY-MM-DD.md and
# rolls state/gold_trend.json (12 weeks). Refresh data/gold_macro_factors.yaml quarterly.
0 21 * * 0 cd $PROJECT && uv run python scheduler/gold_trend.py >> $LOG_DIR/gold_trend.log 2>&1
```

- [ ] **Step 7: Document in CLAUDE.md**

Under the scheduler section of `CLAUDE.md`, add one line noting the new weekly job:

```markdown
### Weekly gold trend (no LLM cost beyond one summary call)

Pure-Python weekly job scoring KRX gold (`411060`) trend + a macro scorecard, emitting
an ACCUMULATE/HOLD/PAUSE verdict. Slow macro inputs live in `data/gold_macro_factors.yaml`
(refresh quarterly). Runs Sunday 21:00 KST.

    uv run python scheduler/gold_trend.py --llm-mode none --no-telegram   # dry run
```

- [ ] **Step 8: Verify the job runs end-to-end (offline dry run)**

Run: `uv run python scheduler/gold_trend.py --llm-mode none --no-telegram --reports-dir /tmp/gold_dryrun --state /tmp/gold_dryrun/state.json`
Expected: prints a `[금 주간 분석]` report; exit 0; a file appears under `/tmp/gold_dryrun/`. (Live fetches may degrade gracefully with ⚠ labels if markets are closed — that is acceptable.)

- [ ] **Step 9: Commit**

```bash
git add scheduler/gold_trend.py scheduler/crontab.example CLAUDE.md tests/test_gold_trend.py
git commit -m "feat(gold): orchestrator main + CLI + weekly cron + docs"
```

---

## Self-Review (completed by plan author)

**Spec coverage:**
- Deterministic technical scoring → Task 2. ✔
- Macro scorecard (4 factors, live + config) → Tasks 1, 3, 7. ✔
- Single LLM summary paragraph → Task 7 (`run_llm_summary`, `build_llm_prompt`), wired in Task 8. ✔
- Verdict ACCUMULATE/HOLD/PAUSE + hard flags → Task 4. ✔
- Optional 2g position P&L → Task 5 (`position_line`), wired Task 8. ✔ (uses approximate spot×FX per-gram — a deliberate resolution of the spec's per-gram gap; labelled "근사".)
- Telegram + report file + 12-week state → Tasks 6, 8. ✔ (no `predictions.db`. ✔)
- Fail-open on every fetch → Task 7 sentinels + Task 8 degraded labels. ✔
- Sunday 21:00 KST cron + pyyaml base dep + CLAUDE.md → Tasks 1, 8. ✔
- `pytest -m "not network"` green; live tests marked → Tasks 7, 8. ✔

**Deviations from spec (surfaced to Doctor Cho, pending nod):**
1. USD gold source `GLD → GC=F` (needs USD/oz for the per-gram derivation; also used for 3M/6M context returns).
2. KRX fetch uses `pykrx` directly (spec listed KR provider primary / pykrx fallback) — collapses to one self-contained path.
3. Position per-gram is an **approximation** (`spot USD/oz × USD/KRW ÷ 31.1035`), not the literal KRX 금현물 원/g; labelled "근사".

**Placeholder scan:** none — every step carries real code/commands.
**Type consistency:** `ctx` keys produced by `build_context` (Task 8) match those consumed by `render_report` (Task 5); `compute_technical`/`compute_macro`/`decide_verdict` signatures consistent across Tasks 2–4 and 8.
