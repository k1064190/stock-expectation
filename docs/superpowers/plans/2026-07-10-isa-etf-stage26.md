# ISA ETF Stage 26 — KR ETF Data Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A KR-listed ETF universe + metadata layer (`mcp-market-data/etf_kr.py`) exposed as `stock-cli etf list / info`, with classification (asset class, tax type, hedge, leverage) and fail-open caching.

**Architecture:** Naver Finance is the verified data source (probed 2026-07-10): `finance.naver.com/api/sise/etfItemList.nhn` returns all 1,141 KR ETFs as cp949 JSON (code, name, price, NAV, 3m return, volume, traded value, marketSum, etfTabCode); `m.stock.naver.com/api/stock/{code}/integration` returns per-ETF 펀드보수(`fundPay`) and 기초지수(`etfBaseIdx`) inside `totalInfos`. pykrx ETF endpoints are currently broken (KeyError '시장' — same breakage class as the stage-3 ticker-list incident) and are NOT used. Universe fetches are cached to `data/etf_universe_kr.csv`; on fetch failure the stale cache is served with a visible note (macro-news fail-open philosophy).

**Tech Stack:** Python 3.11, httpx (already a dependency), stdlib csv/dataclasses, pytest.

## Global Constraints

- Run everything via `uv run`; tests must pass with `uv run pytest -m "not network"` (network-hitting tests carry `@pytest.mark.network`).
- All CLI output is JSON (match existing `stock_cli.py` conventions).
- Fail open with a VISIBLE note — never silent zeros (project-wide rule since the R3 incident).
- No coupling to `mcp-prediction-store` / predictions.db.
- Naver universe endpoint body is **cp949-encoded** — decode `r.content.decode("cp949")`, never `r.json()`.
- Match existing provider code style in `mcp-market-data/` (module-level constants, docstrings documenting args/returns).

---

### Task 1: `etf_kr.py` — universe fetch, parse, classify

**Files:**
- Create: `mcp-market-data/etf_kr.py`
- Test: `mcp-market-data/tests/test_etf_kr.py`

**Interfaces:**
- Produces: `EtfInfo` dataclass and `fetch_etf_universe() -> list[EtfInfo]`; pure helper `_parse_universe(payload: dict) -> list[EtfInfo]`; `class EtfDataUnavailable(Exception)`. Fields of `EtfInfo`: `code: str`, `name: str`, `price: float`, `nav: float | None`, `deviation_pct: float | None` (=(price-nav)/nav*100, rounded 3), `aum_100m_krw: int` (marketSum, 억원), `value_million_krw: int` (amonut), `ret_3m_pct: float | None`, `tab_code: int`, `asset_class: str`, `tax_type: str`, `hedged: bool`, `leveraged_or_inverse: bool`.

- [ ] **Step 1: Write failing tests** (canned payload, no network)

```python
# mcp-market-data/tests/test_etf_kr.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from etf_kr import EtfInfo, _parse_universe  # noqa: E402

PAYLOAD = {
    "result": {
        "etfItemList": [
            {"itemcode": "069500", "itemname": "KODEX 200", "nowVal": 123895,
             "nav": 123899.0, "threeMonthEarnRate": 35.4985, "quant": 12813727,
             "amonut": 1563670, "marketSum": 269224, "etfTabCode": 1},
            {"itemcode": "360750", "itemname": "TIGER 미국S&P500", "nowVal": 28252,
             "nav": 28276.0, "threeMonthEarnRate": 12.8732, "quant": 94872086,
             "amonut": 2677236, "marketSum": 202101, "etfTabCode": 4},
            {"itemcode": "371460", "itemname": "TIGER 차이나전기차SOLACTIVE(H)", "nowVal": 10000,
             "nav": 10010.0, "threeMonthEarnRate": 1.0, "quant": 10,
             "amonut": 5, "marketSum": 30, "etfTabCode": 4},
            {"itemcode": "122630", "itemname": "KODEX 레버리지", "nowVal": 20000,
             "nav": None, "threeMonthEarnRate": 60.0, "quant": 100,
             "amonut": 50, "marketSum": 20000, "etfTabCode": 3},
        ]
    }
}


def test_parse_universe_basic_fields():
    etfs = {e.code: e for e in _parse_universe(PAYLOAD)}
    kodex = etfs["069500"]
    assert kodex.name == "KODEX 200"
    assert kodex.aum_100m_krw == 269224
    assert abs(kodex.deviation_pct - (123895 - 123899.0) / 123899.0 * 100) < 1e-6


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
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest mcp-market-data/tests/test_etf_kr.py -q` → FAIL (`ModuleNotFoundError: etf_kr`)

- [ ] **Step 3: Implement `etf_kr.py`**

```python
"""KR-listed ETF universe + metadata layer (Naver Finance source).

Verified 2026-07-10: finance.naver.com etfItemList returns all KR ETFs as
cp949 JSON. pykrx ETF endpoints are broken (KeyError '시장') and are not used.
Fail-open philosophy: fetch errors raise EtfDataUnavailable; callers serve the
stale CSV cache with a visible note instead of silent zeros.
"""
from __future__ import annotations

from dataclasses import dataclass
import json

import httpx

UNIVERSE_URL = "https://finance.naver.com/api/sise/etfItemList.nhn"
_HEADERS = {"User-Agent": "Mozilla/5.0"}

# etfTabCode → asset class (Naver ETF tab taxonomy).
TAB_ASSET_CLASS = {
    1: "domestic_equity",        # 국내 시장지수
    2: "domestic_sector",        # 국내 업종/테마
    3: "domestic_derivative",    # 국내 파생 (레버리지/인버스)
    4: "overseas_equity",        # 해외 주식
    5: "commodity",              # 원자재
    6: "bond",                   # 채권
    7: "other",                  # 기타 (리츠/혼합 등)
}
# 국내주식형(매매차익 비과세)은 국내 주식 지수/업종 현물형만. 파생/해외/채권/
# 원자재는 기타형(보유기간 과세) — ISA 절세 효과가 큰 쪽.
_DOMESTIC_EQUITY_TABS = {1, 2}
_LEVERAGE_TOKENS = ("레버리지", "인버스", "2X", "2x")


class EtfDataUnavailable(Exception):
    """Raised when the universe source cannot be fetched or parsed."""


@dataclass
class EtfInfo:
    code: str
    name: str
    price: float
    nav: float | None
    deviation_pct: float | None
    aum_100m_krw: int
    value_million_krw: int
    ret_3m_pct: float | None
    tab_code: int
    asset_class: str
    tax_type: str
    hedged: bool
    leveraged_or_inverse: bool


def _parse_universe(payload: dict) -> list[EtfInfo]:
    """Parse the etfItemList payload into classified EtfInfo rows.

    Args: payload — decoded JSON dict from UNIVERSE_URL.
    Returns: one EtfInfo per listed ETF (unfiltered).
    """
    out: list[EtfInfo] = []
    for it in payload.get("result", {}).get("etfItemList", []):
        code = str(it.get("itemcode", "")).zfill(6)
        name = it.get("itemname", "")
        price = float(it.get("nowVal") or 0)
        nav = it.get("nav")
        nav = float(nav) if nav not in (None, "", 0) else None
        deviation = round((price - nav) / nav * 100, 3) if nav else None
        tab = int(it.get("etfTabCode") or 7)
        lev = any(tok in name for tok in _LEVERAGE_TOKENS) or tab == 3
        tax = "domestic_equity_type" if (tab in _DOMESTIC_EQUITY_TABS and not lev) else "other_type"
        ret_3m = it.get("threeMonthEarnRate")
        out.append(EtfInfo(
            code=code, name=name, price=price, nav=nav, deviation_pct=deviation,
            aum_100m_krw=int(it.get("marketSum") or 0),
            value_million_krw=int(it.get("amonut") or 0),
            ret_3m_pct=float(ret_3m) if ret_3m is not None else None,
            tab_code=tab, asset_class=TAB_ASSET_CLASS.get(tab, "other"),
            tax_type=tax, hedged=name.rstrip().endswith("(H)"),
            leveraged_or_inverse=lev,
        ))
    return out


def fetch_etf_universe(timeout: float = 10.0) -> list[EtfInfo]:
    """Fetch and parse the full KR ETF universe from Naver.

    Returns: list[EtfInfo]. Raises EtfDataUnavailable on any fetch/parse error
    (body is cp949 — decoded explicitly).
    """
    try:
        r = httpx.get(UNIVERSE_URL, headers=_HEADERS, timeout=timeout)
        r.raise_for_status()
        payload = json.loads(r.content.decode("cp949"))
    except Exception as e:  # noqa: BLE001 — single fail-open choke point
        raise EtfDataUnavailable(f"etf universe fetch failed: {e}") from e
    rows = _parse_universe(payload)
    if not rows:
        raise EtfDataUnavailable("etf universe fetch returned no rows")
    return rows
```

- [ ] **Step 4: Run tests** — `uv run pytest mcp-market-data/tests/test_etf_kr.py -q` → 3 passed
- [ ] **Step 5: Commit** — `git add mcp-market-data/etf_kr.py mcp-market-data/tests/test_etf_kr.py && git commit -m "feat(etf): KR ETF universe fetch + classification (Naver source)"`

---

### Task 2: per-ETF detail (펀드보수, 기초지수)

**Files:**
- Modify: `mcp-market-data/etf_kr.py` (append)
- Test: `mcp-market-data/tests/test_etf_kr.py` (append)

**Interfaces:**
- Produces: `fetch_etf_detail(code: str) -> dict` with keys `fund_pay_pct: float | None`, `base_index: str | None`, `notes: list[str]`; pure helper `_parse_detail(payload: dict) -> dict`.

- [ ] **Step 1: Failing tests**

```python
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
```

- [ ] **Step 2: Run** → FAIL (`ImportError: _parse_detail`)
- [ ] **Step 3: Implement** (append to `etf_kr.py`)

```python
DETAIL_URL = "https://m.stock.naver.com/api/stock/{code}/integration"


def _parse_detail(payload: dict) -> dict:
    """Extract 펀드보수/기초지수 from the integration payload's totalInfos."""
    infos = {i.get("code"): i.get("value") for i in payload.get("totalInfos", [])}
    notes: list[str] = []
    fee = infos.get("fundPay")
    fee_pct = None
    if fee:
        try:
            fee_pct = float(str(fee).replace("%", "").replace(",", ""))
        except ValueError:
            notes.append(f"fundPay unparseable: {fee!r}")
    base_index = infos.get("etfBaseIdx") or None
    if fee_pct is None:
        notes.append("fund fee unavailable")
    if base_index is None:
        notes.append("base index unavailable")
    return {"fund_pay_pct": fee_pct, "base_index": base_index, "notes": notes}


def fetch_etf_detail(code: str, timeout: float = 10.0) -> dict:
    """Fetch per-ETF detail (fee, base index). Fail-open: on error returns
    Nones with an explanatory note instead of raising — detail is enrichment,
    not a hard dependency."""
    try:
        r = httpx.get(DETAIL_URL.format(code=code), headers=_HEADERS,
                      timeout=timeout, follow_redirects=True)
        r.raise_for_status()
        return _parse_detail(r.json())
    except Exception as e:  # noqa: BLE001
        return {"fund_pay_pct": None, "base_index": None,
                "notes": [f"etf detail fetch failed for {code}: {e}"]}
```

- [ ] **Step 4: Run** → 5 passed · **Step 5: Commit** `feat(etf): per-ETF detail — 펀드보수/기초지수 (fail-open enrichment)`

---

### Task 3: universe CSV cache with stale fallback

**Files:**
- Modify: `mcp-market-data/etf_kr.py` (append)
- Test: `mcp-market-data/tests/test_etf_kr.py` (append)

**Interfaces:**
- Produces: `get_etf_universe(cache_path: Path | None = None, refresh: bool = False) -> tuple[list[EtfInfo], str, list[str]]` returning `(rows, source, notes)` where source ∈ `"live" | "cache-stale"`. Default cache path `data/etf_universe_kr.csv`. Live fetches always rewrite the cache; on `EtfDataUnavailable` the cache is loaded and `notes` carries a visible stale warning; if neither works, re-raises `EtfDataUnavailable`.

- [ ] **Step 1: Failing tests** (tmp_path cache; monkeypatched `fetch_etf_universe`)

```python
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
    import etf_kr, pytest as _pytest
    monkeypatch.setattr(
        etf_kr, "fetch_etf_universe",
        lambda **kw: (_ for _ in ()).throw(etf_kr.EtfDataUnavailable("down")))
    with _pytest.raises(etf_kr.EtfDataUnavailable):
        etf_kr.get_etf_universe(cache_path=tmp_path / "none.csv")
```

- [ ] **Step 2: Run** → FAIL · **Step 3: Implement** (append; `_save_cache`/`_load_cache` via `csv.DictWriter` over `dataclasses.asdict`, empty-string ↔ None round-trip for `nav/deviation_pct/ret_3m_pct`, bools as `"1"/"0"`; `get_etf_universe` try-live → save → return, except → load-cache → notes `["etf universe: live fetch failed (...); serving stale cache from <mtime iso>"]`, else re-raise) · **Step 4: Run** → 8 passed · **Step 5: Commit** `feat(etf): universe CSV cache with visible stale fallback`

---

### Task 4: CLI — `etf list` / `etf info`

**Files:**
- Modify: `stock_cli.py` (new `etf` subparser group next to the `catalyst` group; follow its structure)
- Test: `tests/test_etf_cli.py` (in-process, pattern of `tests/test_catalyst_cli.py`)

**Interfaces:**
- Consumes: `get_etf_universe`, `fetch_etf_detail` from `etf_kr` (mcp-market-data already on sys.path in stock_cli).
- Produces: `stock-cli etf list [--asset-class X] [--min-aum N(억)] [--include-leverage] [--refresh] [--limit N]` → JSON `{asof, source, count, notes, etfs:[...]}` sorted by `aum_100m_krw` desc, leverage/inverse EXCLUDED unless `--include-leverage`. `stock-cli etf info CODE` → universe row merged with detail (`fund_pay_pct`, `base_index`) + combined `notes`.

- [ ] **Step 1: Failing tests** (monkeypatch `etf_kr.get_etf_universe`/`fetch_etf_detail` with PAYLOAD rows; capsys-parse JSON; assert: default excludes 122630, `--min-aum 100000` keeps only 069500/360750, `etf info 360750` carries `fund_pay_pct` and `base_index`, unknown code exits nonzero with error JSON)
- [ ] **Step 2: Run** → FAIL · **Step 3: Implement** `cmd_etf_list` / `cmd_etf_info` + parser wiring (JSON via existing `_print_json`-style helper; import etf_kr the same way events is imported) · **Step 4: Run** → PASS, then full `uv run pytest -m "not network"` → all green · **Step 5: Commit** `feat(cli): etf list/info subcommands`

---

### Task 5: stage docs + index

**Files:**
- Create: `docs/stage-26/etf-data-layer.md` (Why/What/How/Code locations/Retrospective; Review section appended later by the review loop)
- Modify: `docs/summary.md` (append `## Stage 26 — KR ETF data layer` + one line), `README.md` (one CLI example line under the CLI section), `CLAUDE.md` (add `etf` to the subcommand list line if one exists)

- [ ] **Step 1: Write stage doc + index entry + README/CLAUDE.md one-liners**
- [ ] **Step 2: Full test run** `uv run pytest -m "not network"` → green
- [ ] **Step 3: Commit** `docs(stage-26): ETF data layer stage doc + index`

## Self-Review Notes

- Spec coverage: universe+metadata (T1/T2), cache fail-open (T3), CLI (T4), docs (T5). 추적오차 is deliberately absent (source unavailable; scoring in stage 27 works without it, per spec's "missing metadata downgrades with visible flag").
- No placeholders; interfaces named consistently (`get_etf_universe` tuple shape used by T4).
- One network-marked smoke test may be added by the implementer for the live endpoints (`@pytest.mark.network`), optional.
