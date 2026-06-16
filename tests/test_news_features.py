"""Tests for structured news features (mcp-market-data/news_features.py)."""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp-market-data"))

from news_features import (  # noqa: E402
    classify_event,
    dedup_headlines,
    summarize_news,
)


@dataclass
class FakeItem:
    headline: str
    date: str
    sentiment_score: Optional[float] = None


# --- classify_event -------------------------------------------------------- #
def test_classify_event_multi_tag():
    tags = classify_event("Acme beats Q3 earnings, raises guidance")
    assert "earnings" in tags
    assert "guidance" in tags


def test_classify_event_none():
    assert classify_event("Acme opens new headquarters building") == []


def test_classify_event_analyst_and_legal():
    assert "analyst" in classify_event("Goldman issues downgrade, cuts price target")
    assert "regulatory_legal" in classify_event("Acme faces SEC investigation")


# --- dedup_headlines ------------------------------------------------------- #
def test_dedup_exact_and_prefix():
    out = dedup_headlines(
        [
            "Acme beats earnings expectations for Q3",
            "Acme beats earnings expectations for Q3",  # exact dup
            "Acme beats earnings expectations for Q3 quarter, shares jump",  # same 8-word prefix
            "Totally different headline about a merger",
        ]
    )
    assert len(out) == 2


def test_dedup_drops_empty():
    assert dedup_headlines(["", "  ", "Real headline here"]) == ["Real headline here"]


# --- summarize_news -------------------------------------------------------- #
def test_summarize_recency_weighting_favors_fresh():
    # Fresh bullish (+0.8 today) vs stale bearish (-0.8 nine days ago).
    items = [
        FakeItem("Acme product launch today", "2026-06-16", 0.8),
        FakeItem("Acme faced weak demand last week", "2026-06-07", -0.8),
    ]
    sig = summarize_news(items, asof_date="2026-06-16")
    assert sig.mean_sentiment == 0.0  # unweighted cancels
    assert sig.recency_weighted_sentiment > 0.5  # recency tilts strongly bullish


def test_summarize_dedup_and_catalysts():
    items = [
        FakeItem("Acme files for bankruptcy", "2026-06-15", -0.9),
        FakeItem("Acme files for bankruptcy", "2026-06-15", -0.9),  # dup
        FakeItem("Analysts upgrade Acme, beat expected", "2026-06-15", 0.4),
    ]
    sig = summarize_news(items, asof_date="2026-06-16")
    assert sig.raw_count == 3
    assert sig.unique_count == 2
    assert sig.has_negative_catalyst is True
    assert sig.has_positive_catalyst is True
    assert "analyst" in sig.event_tags


def test_summarize_no_sentiment_is_none():
    items = [FakeItem("Acme opens office", "2026-06-16", None)]
    sig = summarize_news(items, asof_date="2026-06-16")
    assert sig.mean_sentiment is None
    assert sig.recency_weighted_sentiment is None


def test_summarize_empty():
    sig = summarize_news([], asof_date="2026-06-16")
    assert sig.unique_count == 0
    assert sig.recency_weighted_sentiment is None
    assert sig.event_tags == []


def test_summarize_bad_date_falls_back_to_unit_weight():
    # Two opposing-sentiment items, both bad dates → equal weight → mean 0.0
    # (proves the fallback is unit weight, not some nonzero-but-unequal value).
    items = [
        FakeItem("Acme upgrades outlook", "not-a-date", 0.6),
        FakeItem("Acme faces lawsuit", "also-bad", -0.6),
    ]
    sig = summarize_news(items, asof_date="2026-06-16")
    assert sig.recency_weighted_sentiment == 0.0


# --- false-positive protection (word boundaries) --------------------------- #
def test_no_false_catalyst_on_substrings():
    # "mission"/"twins"/"heartbeat" must NOT trip miss/wins/beat catalysts.
    items = [
        FakeItem(
            "Acme mission succeeds, twins born, heartbeat steady", "2026-06-16", 0.1
        )
    ]
    sig = summarize_news(items, asof_date="2026-06-16")
    assert sig.has_negative_catalyst is False
    assert sig.has_positive_catalyst is False


def test_real_catalyst_phrases_still_fire():
    neg = summarize_news(
        [FakeItem("Acme posts earnings miss", "2026-06-16", -0.3)], "2026-06-16"
    )
    assert neg.has_negative_catalyst is True
    pos = summarize_news(
        [FakeItem("Acme beats estimates handily", "2026-06-16", 0.3)], "2026-06-16"
    )
    assert pos.has_positive_catalyst is True


# --- Korean coverage ------------------------------------------------------- #
def test_korean_event_tags_and_negative_catalyst():
    items = [FakeItem("삼성전자 유상증자 결정, 실적 부진", "2026-06-16", None)]
    sig = summarize_news(items, asof_date="2026-06-16")
    assert "capital" in sig.event_tags  # 유상증자
    assert "earnings" in sig.event_tags  # 실적
    assert sig.has_negative_catalyst is True  # 유상증자 is a KR negative catalyst


# --- dedup does not over-merge short generic headlines --------------------- #
def test_dedup_keeps_distinct_short_headlines():
    out = dedup_headlines(
        [
            "Nvidia shares rise",  # 3 words — generic stub
            "Nvidia shares rise on record datacenter demand",  # distinct story
        ]
    )
    assert len(out) == 2  # short stub must not swallow the longer distinct story


def test_catalyst_detected_on_deduped_longer_variant():
    # Shorter headline first, then a longer dup that appends the hard catalyst.
    # Dedup keeps the shorter, but the catalyst scan must still see "guidance cut".
    items = [
        FakeItem("Acme shares fall after weak quarter results", "2026-06-16", -0.2),
        FakeItem(
            "Acme shares fall after weak quarter results, guidance cut",
            "2026-06-16",
            -0.3,
        ),
    ]
    sig = summarize_news(items, asof_date="2026-06-16")
    assert sig.unique_count == 1  # deduped
    assert sig.has_negative_catalyst is True  # catalyst still caught


# --- recency edge cases ---------------------------------------------------- #
def test_future_dated_clamps_to_full_weight():
    items = [FakeItem("Acme guides higher", "2026-06-20", 0.5)]  # after asof
    sig = summarize_news(items, asof_date="2026-06-16")
    assert sig.recency_weighted_sentiment == 0.5  # age clamped to 0


def test_very_old_headline_is_dominated_by_fresh():
    items = [
        FakeItem("Acme old bullish note", "2025-06-16", 0.9),  # ~1y old
        FakeItem("Acme fresh mild note", "2026-06-16", 0.2),
    ]
    sig = summarize_news(items, asof_date="2026-06-16")
    assert sig.recency_weighted_sentiment < 0.25  # fresh item dominates
