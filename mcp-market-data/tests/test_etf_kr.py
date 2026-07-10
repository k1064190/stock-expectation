"""Tests for the KR ETF universe layer (canned Naver payload).

Covers ``_parse_universe`` field mapping, asset-class/tax classification,
hedge/leverage flags, None-NAV handling, malformed-row skipping, detail
parsing, and the CSV cache — all offline. One ``@pytest.mark.network`` smoke
test hits the live endpoint.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etf_kr import _parse_universe  # noqa: E402

PAYLOAD = {
    "result": {
        "etfItemList": [
            {
                "itemcode": "069500",
                "itemname": "KODEX 200",
                "nowVal": 123895,
                "nav": 123899.0,
                "threeMonthEarnRate": 35.4985,
                "quant": 12813727,
                "amonut": 1563670,
                "marketSum": 269224,
                "etfTabCode": 1,
            },
            {
                "itemcode": "360750",
                "itemname": "TIGER 미국S&P500",
                "nowVal": 28252,
                "nav": 28276.0,
                "threeMonthEarnRate": 12.8732,
                "quant": 94872086,
                "amonut": 2677236,
                "marketSum": 202101,
                "etfTabCode": 4,
            },
            {
                "itemcode": "371460",
                "itemname": "TIGER 차이나전기차SOLACTIVE(H)",
                "nowVal": 10000,
                "nav": 10010.0,
                "threeMonthEarnRate": 1.0,
                "quant": 10,
                "amonut": 5,
                "marketSum": 30,
                "etfTabCode": 4,
            },
            {
                "itemcode": "122630",
                "itemname": "KODEX 레버리지",
                "nowVal": 20000,
                "nav": None,
                "threeMonthEarnRate": 60.0,
                "quant": 100,
                "amonut": 50,
                "marketSum": 20000,
                "etfTabCode": 3,
            },
        ]
    }
}


def _rows(payload=PAYLOAD):
    rows, _notes = _parse_universe(payload)
    return rows


def test_parse_universe_basic_fields():
    etfs = {e.code: e for e in _rows()}
    kodex = etfs["069500"]
    assert kodex.name == "KODEX 200"
    assert kodex.aum_100m_krw == 269224
    # deviation_pct is rounded to 3 decimals per the EtfInfo interface.
    assert kodex.deviation_pct == round((123895 - 123899.0) / 123899.0 * 100, 3)


def test_parse_universe_clean_payload_has_no_notes():
    _rows_, notes = _parse_universe(PAYLOAD)
    assert len(_rows_) == 4 and notes == []


def test_classification():
    etfs = {e.code: e for e in _rows()}
    assert etfs["069500"].asset_class == "domestic_equity"
    assert etfs["069500"].tax_type == "domestic_equity_type"
    assert etfs["360750"].asset_class == "overseas_equity"
    assert etfs["360750"].tax_type == "other_type"
    assert etfs["371460"].hedged is True
    assert etfs["122630"].leveraged_or_inverse is True
    assert etfs["122630"].tax_type == "other_type"


def test_missing_nav_gives_none_deviation():
    etfs = {e.code: e for e in _rows()}
    assert etfs["122630"].nav is None
    assert etfs["122630"].deviation_pct is None


def test_empty_string_ret_3m_gives_none():
    """Recently listed ETFs return "" for threeMonthEarnRate — must not crash."""
    payload = {
        "result": {
            "etfItemList": [
                {
                    "itemcode": "500001",
                    "itemname": "신규상장 ETF",
                    "nowVal": 10000,
                    "nav": 10000.0,
                    "threeMonthEarnRate": "",
                    "amonut": 1,
                    "marketSum": 100,
                    "etfTabCode": 1,
                }
            ]
        }
    }
    rows, notes = _parse_universe(payload)
    assert rows[0].ret_3m_pct is None and notes == []


def test_malformed_rows_skipped_with_note():
    """One bad row (garbage numeric / missing or invalid itemcode) must be
    skipped with a visible note, never crash the whole universe."""
    payload = {
        "result": {
            "etfItemList": [
                PAYLOAD["result"]["etfItemList"][0],
                {
                    "itemcode": "360750",
                    "itemname": "bad",
                    "nowVal": "N/A",
                    "etfTabCode": 4,
                },
                {"itemcode": "", "itemname": "no code", "nowVal": 1, "etfTabCode": 1},
                {
                    "itemcode": "ABC123X",
                    "itemname": "weird code",
                    "nowVal": 1,
                    "etfTabCode": 1,
                },
            ]
        }
    }
    rows, notes = _parse_universe(payload)
    assert [r.code for r in rows] == ["069500"]
    assert notes == ["skipped 3 malformed universe rows"]


def test_alphanumeric_krx_code_accepted():
    """Post-2024 KRX short codes are alphanumeric (e.g. 0193T0) — they are
    real listings, not malformed rows."""
    payload = {
        "result": {
            "etfItemList": [
                {
                    "itemcode": "0193T0",
                    "itemname": "KODEX SK하이닉스단일종목레버리지",
                    "nowVal": 23095,
                    "nav": 23067.0,
                    "threeMonthEarnRate": None,
                    "amonut": 3108407,
                    "marketSum": 53575,
                    "etfTabCode": 2,
                }
            ]
        }
    }
    rows, notes = _parse_universe(payload)
    assert [r.code for r in rows] == ["0193T0"] and notes == []
    assert rows[0].ret_3m_pct is None
    assert rows[0].leveraged_or_inverse is True


def test_synthetic_hedge_suffix_detected():
    payload = {
        "result": {
            "etfItemList": [
                {
                    "itemcode": "449180",
                    "itemname": "KODEX 미국나스닥100(합성 H)",
                    "nowVal": 10000,
                    "nav": 10000.0,
                    "threeMonthEarnRate": 1.0,
                    "amonut": 1,
                    "marketSum": 100,
                    "etfTabCode": 4,
                }
            ]
        }
    }
    rows, _notes = _parse_universe(payload)
    assert rows[0].hedged is True


def test_extended_leverage_tokens():
    def item(code, name):
        return {
            "itemcode": code,
            "itemname": name,
            "nowVal": 10000,
            "nav": 10000.0,
            "threeMonthEarnRate": 1.0,
            "amonut": 1,
            "marketSum": 100,
            "etfTabCode": 4,
        }

    payload = {
        "result": {
            "etfItemList": [
                item("500002", "가상 미국반도체 3배 인버스"),
                item("360140", "KODEX 200롱코스닥150숏선물"),
                item("500003", "가상 곱버스 ETF"),
            ]
        }
    }
    rows, _notes = _parse_universe(payload)
    assert all(r.leveraged_or_inverse for r in rows)
    assert all(r.tax_type == "other_type" for r in rows)


DETAIL_PAYLOAD = {
    "totalInfos": [
        {"code": "nav", "key": "NAV", "value": "28,276.20"},
        {"code": "fundPay", "key": "펀드보수", "value": "0.007%"},
        {"code": "etfBaseIdx", "key": "기초지수", "value": "S&P 500"},
    ]
}


def test_parse_detail_extracts_fee_and_index():
    from etf_kr import _parse_detail

    d = _parse_detail(DETAIL_PAYLOAD)
    assert d["fund_pay_pct"] == 0.007
    assert d["base_index"] == "S&P 500"
    assert d["notes"] == []


def test_parse_detail_missing_fields_noted():
    from etf_kr import _parse_detail

    d = _parse_detail({"totalInfos": []})
    assert d["fund_pay_pct"] is None and d["base_index"] is None
    assert any("unavailable" in n for n in d["notes"])


def test_universe_cache_roundtrip(tmp_path, monkeypatch):
    import etf_kr

    rows = _rows()
    monkeypatch.setattr(etf_kr, "fetch_etf_universe", lambda **kw: (rows, []))
    cache = tmp_path / "u.csv"
    got, source, notes = etf_kr.get_etf_universe(cache_path=cache)
    assert source == "live" and cache.exists() and notes == []
    # Full field fidelity through the CSV round-trip (dataclass equality).
    assert etf_kr._load_cache(cache) == rows
    assert got == rows


def test_universe_stale_cache_on_failure(tmp_path, monkeypatch):
    import etf_kr

    rows = _rows()
    cache = tmp_path / "u.csv"
    etf_kr._save_cache(rows, cache)

    def boom(**kw):
        raise etf_kr.EtfDataUnavailable("down")

    monkeypatch.setattr(etf_kr, "fetch_etf_universe", boom)
    got, source, notes = etf_kr.get_etf_universe(cache_path=cache)
    assert source == "cache-stale" and len(got) == 4
    assert any("stale" in n for n in notes)


def test_universe_both_down_raises(tmp_path, monkeypatch):
    import etf_kr
    import pytest as _pytest

    monkeypatch.setattr(
        etf_kr,
        "fetch_etf_universe",
        lambda **kw: (_ for _ in ()).throw(etf_kr.EtfDataUnavailable("down")),
    )
    with _pytest.raises(etf_kr.EtfDataUnavailable):
        etf_kr.get_etf_universe(cache_path=tmp_path / "none.csv")


def test_universe_cache_write_failure_still_returns_live(tmp_path, monkeypatch):
    """A read-only data/ dir must not kill a successful live fetch — the rows
    are returned with a visible cache-write note."""
    import etf_kr

    rows = _rows()
    monkeypatch.setattr(etf_kr, "fetch_etf_universe", lambda **kw: (rows, []))

    def boom(_rows_, _path):
        raise OSError("read-only file system")

    monkeypatch.setattr(etf_kr, "_save_cache", boom)
    got, source, notes = etf_kr.get_etf_universe(cache_path=tmp_path / "u.csv")
    assert source == "live" and got == rows
    assert any("cache write failed" in n for n in notes)


@pytest.mark.network
def test_live_universe_smoke():
    """Live smoke: the Naver universe endpoint still serves cp949 JSON with
    hundreds of classified ETFs (deselect with -m "not network")."""
    from etf_kr import fetch_etf_universe

    rows, notes = fetch_etf_universe()
    assert len(rows) > 500
    codes = {r.code for r in rows}
    assert "069500" in codes  # KODEX 200
    assert all(len(r.code) == 6 for r in rows)
