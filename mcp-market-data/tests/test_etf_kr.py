"""Offline tests for the KR ETF universe layer (canned Naver payload).

Covers ``_parse_universe`` field mapping, asset-class/tax classification,
hedge/leverage flags, and None-NAV handling — no network.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etf_kr import EtfInfo, _parse_universe  # noqa: E402

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


def test_parse_universe_basic_fields():
    etfs = {e.code: e for e in _parse_universe(PAYLOAD)}
    kodex = etfs["069500"]
    assert kodex.name == "KODEX 200"
    assert kodex.aum_100m_krw == 269224
    # deviation_pct is rounded to 3 decimals per the EtfInfo interface.
    assert kodex.deviation_pct == round((123895 - 123899.0) / 123899.0 * 100, 3)


def test_classification():
    etfs = {e.code: e for e in _parse_universe(PAYLOAD)}
    assert etfs["069500"].asset_class == "domestic_equity"
    assert etfs["069500"].tax_type == "domestic_equity_type"
    assert etfs["360750"].asset_class == "overseas_equity"
    assert etfs["360750"].tax_type == "other_type"
    assert etfs["371460"].hedged is True
    assert etfs["122630"].leveraged_or_inverse is True
    assert etfs["122630"].tax_type == "other_type"


def test_missing_nav_gives_none_deviation():
    etfs = {e.code: e for e in _parse_universe(PAYLOAD)}
    assert etfs["122630"].nav is None
    assert etfs["122630"].deviation_pct is None


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

    rows = etf_kr._parse_universe(PAYLOAD)
    monkeypatch.setattr(etf_kr, "fetch_etf_universe", lambda **kw: rows)
    cache = tmp_path / "u.csv"
    got, source, notes = etf_kr.get_etf_universe(cache_path=cache)
    assert source == "live" and len(got) == 4 and cache.exists()


def test_universe_stale_cache_on_failure(tmp_path, monkeypatch):
    import etf_kr

    rows = etf_kr._parse_universe(PAYLOAD)
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
        etf_kr, "fetch_etf_universe",
        lambda **kw: (_ for _ in ()).throw(etf_kr.EtfDataUnavailable("down")))
    with _pytest.raises(etf_kr.EtfDataUnavailable):
        etf_kr.get_etf_universe(cache_path=tmp_path / "none.csv")
