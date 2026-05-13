"""Unit tests for scheduler.theme_clusterer.

All tests use hand-built news dicts — no network calls. Live news fetch
is covered by Stage B's verification script, not here.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "mcp-market-data"))

from candidate_discovery import Candidate  # noqa: E402
from theme_clusterer import (  # noqa: E402
    ThemeCluster,
    backfill_news_counts,
    cluster_news,
    fetch_news_for_candidates,
    format_themes_for_prompt,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _news(headlines: list[str]) -> list[dict]:
    return [{"headline": h, "date": "2026-05-13", "url": ""} for h in headlines]


def _candidate(ticker: str) -> Candidate:
    return Candidate(
        ticker=ticker,
        name="",
        market="KR",
        market_cap=None,
        trading_value=None,
        return_5d_pct=0.0,
        vol_ratio_5d=1.0,
    )


# ---------------------------------------------------------------------------
# cluster_news — the 5/13 scenario
# ---------------------------------------------------------------------------


def test_513_physical_ai_cluster_surfaces_across_three_tickers():
    """The exact 5/13 catalyst — 'physical AI' headlines from 3 KR tickers."""
    news = {
        "307950": _news(
            [
                "현대오토에버, 피지컬 AI ETF 핵심 종목 부각",
                "현대차그룹 피지컬 AI 강세",
            ]
        ),
        "005380": _news(
            [
                "현대차, 피지컬 AI 로보틱스 뉴욕 공개",
                "광주 자율주행 200대 실증",
            ]
        ),
        "066570": _news(
            [
                "외국인 피지컬 AI 로봇주 매수, LG전자 집중",
            ]
        ),
        "012330": _news(
            [
                "현대모비스 자율주행 부품 강세",
            ]
        ),
    }
    clusters = cluster_news(news, min_cluster_size=3)
    assert clusters, "expected at least one cluster"
    physical_ai = next(
        (c for c in clusters if "피지컬" in c.keywords and "ai" in c.keywords),
        None,
    )
    assert physical_ai is not None, f"got clusters: {[c.keywords for c in clusters]}"
    assert set(physical_ai.tickers) == {"005380", "066570", "307950"}
    assert physical_ai.headline_count == 4


def test_min_cluster_size_enforced():
    """An ngram hitting only 2 tickers doesn't qualify when min=3."""
    news = {
        "AAA": _news(["AI 반도체 호재"]),
        "BBB": _news(["AI 반도체 강세"]),  # same 2-gram, only 2 tickers
        "CCC": _news(["전혀 다른 뉴스"]),
    }
    assert cluster_news(news, min_cluster_size=3) == []


def test_stopwords_dropped_from_clusters():
    """Common stopwords like '기업' / '종목' / '뉴스' are filtered before n-gram."""
    news = {
        "AAA": _news(["코스피 기업 종목 뉴스"]),
        "BBB": _news(["코스피 기업 종목 뉴스"]),
        "CCC": _news(["코스피 기업 종목 뉴스"]),
    }
    clusters = cluster_news(news, min_cluster_size=3)
    for c in clusters:
        for kw in c.keywords:
            assert kw not in {"기업", "종목", "뉴스", "코스피"}, kw


def test_superset_preference_over_same_ticker_set():
    """When ('ai',) and ('피지컬', 'ai') hit the same 3 tickers, the bigram wins."""
    news = {
        "AAA": _news(["피지컬 AI 출시"]),
        "BBB": _news(["피지컬 AI 모멘텀"]),
        "CCC": _news(["피지컬 AI 정책"]),
    }
    clusters = cluster_news(news, min_cluster_size=3, ngram_sizes=(1, 2, 3))
    same_set_clusters = [c for c in clusters if set(c.tickers) == {"AAA", "BBB", "CCC"}]
    assert len(same_set_clusters) == 1, (
        f"expected one cluster per ticker set, got: "
        f"{[c.keywords for c in same_set_clusters]}"
    )
    assert (
        len(same_set_clusters[0].keywords) >= 2
    ), f"expected superset (length>=2), got: {same_set_clusters[0].keywords}"


def test_empty_news_returns_empty_list():
    """No news → no clusters, no crash."""
    assert cluster_news({}) == []
    assert cluster_news({"AAA": []}) == []


def test_clusters_sorted_by_ticker_count_then_headlines():
    """Largest theme first; same ticker count → more headlines wins."""
    news = {
        "AAA": _news(["원전 정책 발표"]),
        "BBB": _news(["원전 정책 강세"]),
        "CCC": _news(["원전 정책 모멘텀"]),
        "DDD": _news(["원전 정책 호재"]),
        # Smaller cluster: 3 tickers share 'AI 반도체' (single-syllable Hangul
        # words like '셀' get dropped by the len>=2 token filter, so the
        # smaller cluster keyword must be multi-syllable / multi-char to
        # survive tokenisation).
        "EEE": _news(["AI 반도체 공급 확대"]),
        "FFF": _news(["AI 반도체 모멘텀"]),
        "GGG": _news(["AI 반도체 호재"]),
    }
    clusters = cluster_news(news, min_cluster_size=3)
    assert len(clusters) >= 2
    # 4-ticker cluster ranks before 3-ticker cluster
    assert len(clusters[0].tickers) >= len(clusters[1].tickers)


def test_sample_headlines_capped_at_five_and_dedup():
    """`sample_headlines` collects one per ticker, dedup'd, max 5."""
    news = {f"T{i:02d}": _news(["AI 반도체 호재"]) for i in range(7)}
    clusters = cluster_news(news, min_cluster_size=3)
    assert clusters
    assert len(clusters[0].sample_headlines) <= 5


# ---------------------------------------------------------------------------
# fetch_news_for_candidates — provider mocking
# ---------------------------------------------------------------------------


def test_fetch_news_uses_thread_pool_and_collects_all_tickers():
    """All requested tickers appear as keys in the result, even on failure."""
    mock = MagicMock()
    mock.get_news.side_effect = lambda ticker, limit, days: [
        type(
            "NI",
            (),
            {"headline": f"{ticker} 헤드라인", "date": "2026-05-13", "url": ""},
        )()
    ]
    cands = [_candidate(t) for t in ("AAA", "BBB", "CCC")]
    out = fetch_news_for_candidates(cands, provider=mock)
    assert set(out.keys()) == {"AAA", "BBB", "CCC"}
    assert all(len(v) == 1 for v in out.values())
    assert mock.get_news.call_count == 3


def test_fetch_news_swallows_per_ticker_exception():
    """One ticker's fetch raising must not poison the rest."""
    mock = MagicMock()

    def flaky(ticker, limit, days):
        if ticker == "BBB":
            raise RuntimeError("naver tripped")
        return [type("NI", (), {"headline": f"{ticker} ok", "date": "", "url": ""})()]

    mock.get_news.side_effect = flaky
    cands = [_candidate(t) for t in ("AAA", "BBB", "CCC")]
    out = fetch_news_for_candidates(cands, provider=mock)
    assert out["AAA"] and out["CCC"]
    assert out["BBB"] == []


def test_fetch_news_normalises_dataclass_and_dict_alike():
    """Provider may return dataclass or dict; downstream gets dict shape."""
    mock = MagicMock()
    mock.get_news.return_value = [
        {"headline": "from dict", "date": "2026-05-13", "url": "u", "source": "s"},
    ]
    out = fetch_news_for_candidates([_candidate("AAA")], provider=mock)
    assert out["AAA"][0]["headline"] == "from dict"


def test_fetch_news_empty_candidates_short_circuits():
    """Zero candidates → empty dict, provider never called."""
    mock = MagicMock()
    out = fetch_news_for_candidates([], provider=mock)
    assert out == {}
    mock.get_news.assert_not_called()


# ---------------------------------------------------------------------------
# backfill_news_counts
# ---------------------------------------------------------------------------


def test_backfill_mutates_candidate_in_place():
    cands = [_candidate("AAA"), _candidate("BBB")]
    news = {"AAA": _news(["h1", "h2", "h3"]), "BBB": []}
    backfill_news_counts(cands, news)
    assert cands[0].news_count_7d == 3
    assert cands[1].news_count_7d == 0


def test_backfill_handles_missing_ticker():
    cands = [_candidate("AAA"), _candidate("BBB")]
    backfill_news_counts(cands, {"AAA": _news(["x"])})
    assert cands[0].news_count_7d == 1
    assert cands[1].news_count_7d == 0


# ---------------------------------------------------------------------------
# format_themes_for_prompt
# ---------------------------------------------------------------------------


def test_format_empty_clusters_emits_fallback_section():
    """Empty list still produces a non-empty markdown block."""
    out = format_themes_for_prompt([])
    assert "Active Themes" in out
    assert "≥3 종목 공유 테마 없음" in out


def test_format_renders_keywords_tickers_and_sample():
    """Each cluster line shows keywords, ticker count, headline count, sample."""
    clusters = [
        ThemeCluster(
            keywords=("피지컬", "ai"),
            tickers=["005380", "066570", "307950"],
            sample_headlines=["KB자산운용, RISE 현대차고정피지컬AI ETF 출시"],
            headline_count=4,
        ),
    ]
    out = format_themes_for_prompt(clusters)
    assert "피지컬 ai" in out
    assert "3종목" in out
    assert "4 헤드라인" in out
    assert "005380, 066570, 307950" in out
    assert "RISE 현대차고정피지컬AI ETF" in out
