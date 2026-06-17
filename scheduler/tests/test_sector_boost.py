"""Tests for the sector-rotation boost in scheduler.candidate_discovery.

Covers ``_load_sector_verdicts`` (reads data/sector_rs_{market}.json,
never-raise) and ``apply_sector_boost`` (in-place stamping + multiplier map),
plus the critical no-regression guarantee: with no sector map on disk the boost
is a strict no-op and ranking is byte-identical.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "mcp-market-data"))

import candidate_discovery as cd  # noqa: E402
from candidate_discovery import (  # noqa: E402
    Candidate,
    _load_sector_verdicts,
    apply_sector_boost,
)


def _cand(ticker: str, ret: float, market: str = "US") -> Candidate:
    """Minimal Candidate with an equal return so only the boost differs."""
    return Candidate(
        ticker=ticker,
        name=ticker,
        market=market,
        market_cap=None,
        trading_value=None,
        return_5d_pct=ret,
        vol_ratio_5d=1.0,
    )


def _write_sector_json(tmp_path: Path, market: str, sectors: list[dict]) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    path = data_dir / f"sector_rs_{market.lower()}.json"
    path.write_text(
        json.dumps({"market": market.upper(), "sectors": sectors}),
        encoding="utf-8",
    )
    return path


# --- _load_sector_verdicts ------------------------------------------------- #
def test_load_maps_constituents_and_etf(tmp_path, monkeypatch):
    """Both the proxy ETF and each constituent resolve to the sector verdict."""
    _write_sector_json(
        tmp_path,
        "US",
        [
            {
                "sector": "Technology",
                "etf": "XLK",
                "verdict": "FAVOR",
                "stage": "EARLY",
                "constituents": ["AAPL", "MSFT"],
            }
        ],
    )
    monkeypatch.setattr(cd, "PROJECT_ROOT", tmp_path)
    verdicts = _load_sector_verdicts("US")
    assert verdicts["AAPL"]["verdict"] == "FAVOR"
    assert verdicts["AAPL"]["stage"] == "EARLY"
    assert verdicts["MSFT"]["sector"] == "Technology"
    assert verdicts["XLK"]["verdict"] == "FAVOR"


def test_load_missing_file_returns_empty(tmp_path, monkeypatch):
    """Absent file -> {} (the no-op path), never raises."""
    monkeypatch.setattr(cd, "PROJECT_ROOT", tmp_path)
    assert _load_sector_verdicts("US") == {}


def test_load_malformed_json_returns_empty(tmp_path, monkeypatch):
    """Corrupt JSON -> {} (never-raise)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "sector_rs_us.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(cd, "PROJECT_ROOT", tmp_path)
    assert _load_sector_verdicts("US") == {}


def test_load_unexpected_shape_returns_empty(tmp_path, monkeypatch):
    """A payload without a 'sectors' list -> {} (never-raise)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "sector_rs_us.json").write_text(
        json.dumps({"market": "US"}), encoding="utf-8"
    )
    monkeypatch.setattr(cd, "PROJECT_ROOT", tmp_path)
    assert _load_sector_verdicts("US") == {}


# --- apply_sector_boost: ranking effect ------------------------------------ #
def test_favor_early_outranks_equal_return_avoid():
    """With equal raw return, a FAVOR/EARLY candidate sorts above an AVOID one
    once the multiplier is folded into the sort key."""
    favor = _cand("AAPL", ret=20.0)
    avoid = _cand("XOM", ret=20.0)
    verdicts = {
        "AAPL": {"verdict": "FAVOR", "stage": "EARLY", "sector": "Technology"},
        "XOM": {"verdict": "AVOID", "stage": "MID", "sector": "Energy"},
    }
    cands = [avoid, favor]  # start with AVOID first to prove the re-rank
    mult = apply_sector_boost(cands, verdicts)

    assert mult["AAPL"] == 1.3
    assert mult["XOM"] == 0.6
    # Fields stamped in place.
    assert favor.sector_verdict == "FAVOR"
    assert favor.sector_stage == "EARLY"
    assert avoid.sector_verdict == "AVOID"

    ranked = sorted(
        cands,
        key=lambda c: abs(c.return_5d_pct) * mult[c.ticker],
        reverse=True,
    )
    assert [c.ticker for c in ranked] == ["AAPL", "XOM"]


def test_rotating_out_late_is_most_punitive():
    """ROTATING_OUT at LATE stage -> 0.6 (same haircut as AVOID)."""
    c = _cand("TSLA", ret=10.0)
    verdicts = {"TSLA": {"verdict": "ROTATING_OUT", "stage": "LATE", "sector": "X"}}
    mult = apply_sector_boost([c], verdicts)
    assert mult["TSLA"] == 0.6


def test_rotating_out_non_late_is_softer():
    """ROTATING_OUT at non-LATE stage -> 0.8 (softer than the LATE haircut)."""
    c = _cand("TSLA", ret=10.0)
    verdicts = {"TSLA": {"verdict": "ROTATING_OUT", "stage": "MID", "sector": "X"}}
    mult = apply_sector_boost([c], verdicts)
    assert mult["TSLA"] == 0.8


def test_unknown_ticker_gets_neutral_multiplier():
    """A candidate whose sector isn't in the map keeps fields None, mult 1.0."""
    c = _cand("ZZZZ", ret=10.0)
    verdicts = {"AAPL": {"verdict": "FAVOR", "stage": "EARLY", "sector": "Tech"}}
    mult = apply_sector_boost([c], verdicts)
    assert mult["ZZZZ"] == 1.0
    assert c.sector_verdict is None
    assert c.sector_stage is None


# --- no-regression: empty verdicts is a strict no-op ----------------------- #
def test_empty_verdicts_is_byte_identical_no_op():
    """With no sector map, fields stay None, multipliers are all 1.0, and the
    ranking is unchanged from the pre-sector baseline."""
    cands = [_cand("AAPL", 30.0), _cand("XOM", 20.0), _cand("MSFT", 25.0)]
    baseline = sorted(cands, key=lambda c: abs(c.return_5d_pct), reverse=True)
    baseline_order = [c.ticker for c in baseline]

    mult = apply_sector_boost(cands, {})
    assert all(v == 1.0 for v in mult.values())
    assert all(c.sector_verdict is None and c.sector_stage is None for c in cands)

    boosted = sorted(
        cands, key=lambda c: abs(c.return_5d_pct) * mult[c.ticker], reverse=True
    )
    assert [c.ticker for c in boosted] == baseline_order
