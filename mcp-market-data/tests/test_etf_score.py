"""Offline tests for deterministic ETF candidate scoring (etf_score).

Covers cost/liquidity min-max normalization within the candidate set, the
missing-fee degradation path, and the deterministic tie-break chain.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etf_kr import EtfInfo  # noqa: E402
from etf_score import score_candidates  # noqa: E402


def _mk(code, name, aum, value, dev):
    return EtfInfo(
        code=code,
        name=name,
        price=10000.0,
        nav=10000.0,
        deviation_pct=dev,
        aum_100m_krw=aum,
        value_million_krw=value,
        ret_3m_pct=10.0,
        tab_code=4,
        asset_class="overseas_equity",
        tax_type="other_type",
        hedged=False,
        leveraged_or_inverse=False,
    )


ROWS = [
    _mk("AAAAA1", "TIGER 미국S&P500", aum=202101, value=2677236, dev=0.05),
    _mk("BBBBB2", "KODEX 미국S&P500", aum=100000, value=900000, dev=0.10),
    _mk("CCCCC3", "ACE 미국S&P500", aum=50000, value=400000, dev=0.30),
]
DETAILS = {
    "AAAAA1": {"fund_pay_pct": 0.07, "base_index": "S&P 500", "notes": []},
    "BBBBB2": {"fund_pay_pct": 0.0099, "base_index": "S&P 500", "notes": []},
    "CCCCC3": {
        "fund_pay_pct": None,
        "base_index": "S&P 500",
        "notes": ["fund fee unavailable"],
    },
}


def test_best_balances_cost_and_liquidity():
    out = score_candidates(ROWS, DETAILS)
    scored = {s["code"]: s for s in out["scored"]}
    # BBBBB2: cheapest fee (cost 100) + mid liquidity; AAAAA1: fee 0.07
    # (cost 0) + top liquidity (100)
    assert scored["BBBBB2"]["cost_score"] == 100.0
    assert scored["AAAAA1"]["liquidity_score"] == 100.0
    assert out["best"] == "BBBBB2"  # 0.5*100 + 0.5*mid > 0.5*0 + 0.5*100


def test_missing_fee_excluded_with_note():
    out = score_candidates(ROWS, DETAILS)
    c = next(s for s in out["scored"] if s["code"] == "CCCCC3")
    assert c["cost_score"] is None
    assert c["composite"] == c["liquidity_score"]  # renormalized to liquidity only
    assert any("CCCCC3" in n and "fee unavailable" in n for n in out["notes"])


def test_deterministic_tiebreak_and_empty():
    out = score_candidates([], {})
    assert out["best"] is None and out["scored"] == []
    twin_a = _mk("TWINA1", "X", aum=100, value=100, dev=0.1)
    twin_b = _mk("TWINB2", "Y", aum=100, value=100, dev=0.1)
    d = {
        "TWINA1": {"fund_pay_pct": 0.1, "base_index": "I", "notes": []},
        "TWINB2": {"fund_pay_pct": 0.1, "base_index": "I", "notes": []},
    }
    out = score_candidates([twin_a, twin_b], d)
    assert out["best"] == "TWINA1"  # identical scores → code ascending


def test_single_candidate_degenerate():
    row = _mk("SINGLE", "혼자", aum=100, value=100, dev=0.1)
    d = {"SINGLE": {"fund_pay_pct": 0.1, "base_index": "I", "notes": []}}
    out = score_candidates([row], d)
    s = out["scored"][0]
    assert s["cost_score"] == 100.0 and s["liquidity_score"] == 100.0
    assert any("single candidate" in n for n in out["notes"])
    assert out["best"] == "SINGLE"


def test_missing_deviation_redistributes_liquidity_weights():
    rows = [
        _mk("DDDDD1", "A", aum=200, value=200, dev=None),
        _mk("EEEEE2", "B", aum=100, value=100, dev=0.1),
    ]
    d = {
        "DDDDD1": {"fund_pay_pct": 0.1, "base_index": "I", "notes": []},
        "EEEEE2": {"fund_pay_pct": 0.1, "base_index": "I", "notes": []},
    }
    out = score_candidates(rows, d)
    top = {s["code"]: s for s in out["scored"]}
    # DDDDD1: AUM 100, value 100, deviation missing → (0.5*100+0.3*100)/0.8
    assert top["DDDDD1"]["liquidity_score"] == 100.0
    assert any("DDDDD1" in n and "deviation" in n for n in out["notes"])


def test_indistinguishable_candidates_note():
    """Multi-candidate set with ALL stats equal must say the scores carry no
    signal (promised by the scoring-rules doc)."""
    twin_a = _mk("TWINA1", "X", aum=100, value=100, dev=0.1)
    twin_b = _mk("TWINB2", "Y", aum=100, value=100, dev=0.1)
    d = {
        "TWINA1": {"fund_pay_pct": 0.1, "base_index": "I", "notes": []},
        "TWINB2": {"fund_pay_pct": 0.1, "base_index": "I", "notes": []},
    }
    out = score_candidates([twin_a, twin_b], d)
    assert any("indistinguishable" in n for n in out["notes"])
    # A set with any differing dimension must NOT carry the note.
    out2 = score_candidates(ROWS, DETAILS)
    assert not any("indistinguishable" in n for n in out2["notes"])
