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
    ma200_rising = bool(
        ma200 is not None and ma200_prev is not None and ma200 > ma200_prev
    )
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
        "price": price,
        "ma50": ma50,
        "ma200": ma200,
        "ma200_rising": ma200_rising,
        "rsi": rsi,
        "drawdown_pct": drawdown_pct,
        "trend": trend,
        "pullback": pullback,
        "momentum": momentum,
        "score": score,
        "label": label,
    }


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


def compute_macro(
    config: dict, real_yield_pct: float, usdkrw: float, usdkrw_ma200: float
) -> dict:
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
    score = (
        w["central_bank"] * cb + w["real_rate"] * rr + w["dollar"] * dl + w["fx"] * fx
    )
    label = "높음" if score >= 65 else ("중립" if score >= 45 else "낮음")
    return {
        "central_bank": cb,
        "real_rate": rr,
        "dollar": dl,
        "fx": fx,
        "restrictive_flag": restrictive,
        "score": score,
        "label": label,
    }
