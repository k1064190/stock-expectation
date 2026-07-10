"""KR-listed ETF universe + metadata layer (Naver Finance source).

Verified 2026-07-10: finance.naver.com etfItemList returns all KR ETFs as
cp949 JSON. pykrx ETF endpoints are broken (KeyError '시장') and are not used.
Fail-open philosophy: fetch errors raise EtfDataUnavailable; callers serve the
stale CSV cache with a visible note instead of silent zeros.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path

import httpx

UNIVERSE_URL = "https://finance.naver.com/api/sise/etfItemList.nhn"
_HEADERS = {"User-Agent": "Mozilla/5.0"}

# etfTabCode → asset class (Naver ETF tab taxonomy).
TAB_ASSET_CLASS = {
    1: "domestic_equity",  # 국내 시장지수
    2: "domestic_sector",  # 국내 업종/테마
    3: "domestic_derivative",  # 국내 파생 (레버리지/인버스)
    4: "overseas_equity",  # 해외 주식
    5: "commodity",  # 원자재
    6: "bond",  # 채권
    7: "other",  # 기타 (리츠/혼합 등)
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
        tax = (
            "domestic_equity_type"
            if (tab in _DOMESTIC_EQUITY_TABS and not lev)
            else "other_type"
        )
        ret_3m = it.get("threeMonthEarnRate")
        out.append(
            EtfInfo(
                code=code,
                name=name,
                price=price,
                nav=nav,
                deviation_pct=deviation,
                aum_100m_krw=int(it.get("marketSum") or 0),
                value_million_krw=int(it.get("amonut") or 0),
                ret_3m_pct=float(ret_3m) if ret_3m is not None else None,
                tab_code=tab,
                asset_class=TAB_ASSET_CLASS.get(tab, "other"),
                tax_type=tax,
                hedged=name.rstrip().endswith("(H)"),
                leveraged_or_inverse=lev,
            )
        )
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
        r = httpx.get(
            DETAIL_URL.format(code=code),
            headers=_HEADERS,
            timeout=timeout,
            follow_redirects=True,
        )
        r.raise_for_status()
        return _parse_detail(r.json())
    except Exception as e:  # noqa: BLE001
        return {
            "fund_pay_pct": None,
            "base_index": None,
            "notes": [f"etf detail fetch failed for {code}: {e}"],
        }


# Universe cache — rewritten on every successful live fetch; served stale (with
# a visible note) when the live fetch fails.
DEFAULT_CACHE_PATH = Path(__file__).resolve().parents[1] / "data" / "etf_universe_kr.csv"

# Fields serialized as empty string when None and parsed back to None/float.
_OPTIONAL_FLOAT_FIELDS = ("nav", "deviation_pct", "ret_3m_pct")
_BOOL_FIELDS = ("hedged", "leveraged_or_inverse")
_INT_FIELDS = ("aum_100m_krw", "value_million_krw", "tab_code")


def _save_cache(rows: list[EtfInfo], path: Path) -> None:
    """Write the universe to a CSV cache (None ↔ "", bools as "1"/"0").

    Args: rows — parsed universe; path — CSV destination (parents created).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    names = [f.name for f in fields(EtfInfo)]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=names)
        w.writeheader()
        for row in rows:
            d = asdict(row)
            for k in _OPTIONAL_FLOAT_FIELDS:
                d[k] = "" if d[k] is None else d[k]
            for k in _BOOL_FIELDS:
                d[k] = "1" if d[k] else "0"
            w.writerow(d)


def _load_cache(path: Path) -> list[EtfInfo]:
    """Read the CSV cache back into EtfInfo rows (inverse of ``_save_cache``).

    Returns: cached rows. Raises EtfDataUnavailable if the file is missing,
    unreadable, or empty.
    """
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            raw = list(csv.DictReader(fh))
    except OSError as e:
        raise EtfDataUnavailable(f"etf universe cache unreadable: {e}") from e
    if not raw:
        raise EtfDataUnavailable("etf universe cache is empty")
    out: list[EtfInfo] = []
    for d in raw:
        for k in _OPTIONAL_FLOAT_FIELDS:
            d[k] = float(d[k]) if d[k] != "" else None
        for k in _BOOL_FIELDS:
            d[k] = d[k] == "1"
        for k in _INT_FIELDS:
            d[k] = int(d[k])
        d["price"] = float(d["price"])
        out.append(EtfInfo(**d))
    return out


def get_etf_universe(
    cache_path: Path | None = None, refresh: bool = False
) -> tuple[list[EtfInfo], str, list[str]]:
    """Get the KR ETF universe, live-first with a stale-cache fallback.

    Args:
        cache_path: CSV cache location (default data/etf_universe_kr.csv).
        refresh: unused hint kept for CLI symmetry — fetches are always
            live-first; the cache only serves when the live fetch fails.

    Returns:
        (rows, source, notes) where source is "live" or "cache-stale". A live
        fetch rewrites the cache. On EtfDataUnavailable the stale cache is
        served with a visible note; if the cache also fails, re-raises.
    """
    path = cache_path or DEFAULT_CACHE_PATH
    try:
        rows = fetch_etf_universe()
    except EtfDataUnavailable as live_err:
        rows = _load_cache(path)  # re-raises EtfDataUnavailable if unusable
        mtime = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        return rows, "cache-stale", [
            f"etf universe: live fetch failed ({live_err}); serving stale cache from {mtime}"
        ]
    _save_cache(rows, path)
    return rows, "live", []
