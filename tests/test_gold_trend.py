import pathlib
import sys
import textwrap

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scheduler import gold_trend as gt  # noqa: E402


def test_load_config_missing_file_returns_defaults(tmp_path):
    cfg = gt.load_config(tmp_path / "does_not_exist.yaml")
    assert cfg == gt.DEFAULT_CONFIG


def test_load_config_fills_missing_keys(tmp_path):
    p = tmp_path / "factors.yaml"
    p.write_text(
        textwrap.dedent(
            """
        central_bank:
          trailing_4q_tonnes: 700
        risk_off: true
    """
        )
    )
    cfg = gt.load_config(p)
    assert cfg["central_bank"]["trailing_4q_tonnes"] == 700
    # unspecified sub-key falls back to default
    assert (
        cfg["central_bank"]["baseline_tonnes"]
        == gt.DEFAULT_CONFIG["central_bank"]["baseline_tonnes"]
    )
    assert cfg["risk_off"] is True
    # untouched section falls back entirely
    assert cfg["scoring"]["weights"] == gt.DEFAULT_CONFIG["scoring"]["weights"]


def test_load_config_position_cost_defaults_none(tmp_path):
    cfg = gt.load_config(tmp_path / "nope.yaml")
    assert cfg["position"]["avg_cost_krw_per_g"] is None


def test_rsi_all_gains_is_100():
    closes = [float(i) for i in range(1, 40)]  # strictly increasing
    assert gt._rsi(closes) == 100.0


def test_sma_and_none_when_short():
    assert gt._sma([1, 2, 3, 4], 2) == 3.5
    assert gt._sma([1, 2], 5) is None


def test_pullback_subscore_bands():
    assert gt._pullback_subscore(-2) == 40  # near high
    assert gt._pullback_subscore(-12) == 100  # healthy zone
    assert gt._pullback_subscore(-27) == 70  # deep
    assert gt._pullback_subscore(-40) == 30  # broken


def test_pullback_subscore_boundaries():
    # strict > semantics: the boundary value itself falls into the *next* (lower) band
    assert gt._pullback_subscore(-5) == 100  # -5 > -5 is False -> not the >-5 band
    assert gt._pullback_subscore(-20) == 70  # -20 > -20 is False -> not the >-20 band
    assert gt._pullback_subscore(-35) == 30  # -35 > -35 is False -> falls to else


def test_momentum_subscore_bands():
    assert gt._momentum_subscore(25) == 100
    assert gt._momentum_subscore(40) == 80
    assert gt._momentum_subscore(50) == 60
    assert gt._momentum_subscore(65) == 40
    assert gt._momentum_subscore(80) == 15


def test_momentum_subscore_boundaries():
    # strict < semantics: the boundary value itself falls into the *next* band
    assert gt._momentum_subscore(30) == 80  # 30 < 30 is False
    assert gt._momentum_subscore(45) == 60  # 45 < 45 is False
    assert gt._momentum_subscore(60) == 40  # 60 < 60 is False
    assert gt._momentum_subscore(70) == 40  # 70 <= 70 is True
    assert gt._momentum_subscore(70.0001) == 15  # 70.0001 <= 70 is False


def test_trend_subscore_uptrend_rising():
    assert gt._trend_subscore(110, 100, 90, True) == 100
    assert gt._trend_subscore(110, 100, 90, False) == 60
    assert gt._trend_subscore(80, 100, 90, False) == 30
    # degraded (no ma200)
    assert gt._trend_subscore(110, 100, None, False) == 60
    assert gt._trend_subscore(90, 100, None, False) == 30


def test_trend_subscore_price_equals_ma200_boundary():
    assert gt._trend_subscore(100, 90, 100, True) == 100  # price >= ma200 and rising
    assert gt._trend_subscore(100, 90, 100, False) == 60  # price >= ma200, not rising


def test_compute_technical_healthy_pullback_in_uptrend():
    # Rising series then a mild pullback -> above ma200, healthy drawdown, mid RSI.
    closes = [100 + i for i in range(260)]  # 100..359 rising
    closes += [359 * 0.90]  # -10% pullback tick
    tech = gt.compute_technical(closes)
    assert tech["ma200"] is not None
    assert -20 < tech["drawdown_pct"] < -5
    # deterministic exact values: trend=100 (price>=ma200, rising), pullback=100
    # (drawdown -10 is >-20), momentum=100 (RSI well under 30 after a -10% tick)
    assert tech["trend"] == 100
    assert tech["pullback"] == 100
    assert tech["momentum"] == 100
    assert tech["score"] == 100.0
    assert tech["label"] == "양호"


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
    assert gt._fx_subscore(1200, 1000) == 30  # won very weak (+20%)
    assert gt._fx_subscore(1010, 1000) == 60  # near mean
    assert gt._fx_subscore(920, 1000) == 80  # won strong (-8%)


def test_compute_macro_seed_case():
    cfg = gt.load_config(pathlib.Path("does_not_exist"))  # DEFAULT_CONFIG
    m = gt.compute_macro(cfg, real_yield_pct=1.9, usdkrw=1544, usdkrw_ma200=1450)
    assert m["central_bank"] == 100  # 950 >= 900
    assert m["real_rate"] == 60  # between thresholds
    assert m["restrictive_flag"] is False
    assert m["dollar"] == 55  # 58 -> 55
    assert m["fx"] == 30  # 1544/1450 = +6.5% -> weak
    # 0.35*100 + 0.30*60 + 0.20*55 + 0.15*30 = 68.5
    assert round(m["score"], 1) == 68.5
    assert m["label"] == "높음"


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
    v = gt.decide_verdict(
        _tech(60, 47), _macro(70, restrictive_flag=True), gt.DEFAULT_CONFIG
    )
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


def test_dots_rounding():
    assert gt.dots(100) == "●●●●●"
    assert gt.dots(60) == "●●●○○"
    assert gt.dots(55) == "●●●○○"  # 2.75 -> 3
    assert gt.dots(30) == "●●○○○"  # 1.5 -> 2
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
        "verdict": {
            "verdict": "ACCUMULATE",
            "emoji": "🟢",
            "reasons": ["x"],
            "aggressive": False,
        },
        "technical": {"score": 61, "label": "양호", "rsi": 47},
        "macro": {"score": 68, "label": "높음"},
        "krx": {
            "price": 28740,
            "drawdown_pct": -24.1,
            "rsi": 47,
            "ma200_gap_pct": -3.0,
        },
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
        "verdict": {
            "verdict": "HOLD",
            "emoji": "🟡",
            "reasons": ["x"],
            "aggressive": False,
        },
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
