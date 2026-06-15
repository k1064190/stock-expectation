"""Structured features over a ticker's recent news headlines.

The raw news signal (sentiment-bucket + headline-count) graded "dead" (~33% win)
in calibration. Counting reposts as fresh information and treating a soft
sentiment blip the same as a hard catalyst is most of the problem. This module
turns a list of headlines into a structured signal: deduped, recency-weighted
sentiment plus catalyst/event tags, so the skills can score *information*, not
volume.

Pure functions — no network, no I/O. The CLI ``news`` command attaches the
``summarize_news`` output as a ``signal`` block; tests exercise the logic
directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# Recency half-life: a headline's weight halves every N days of age. News decays
# fast — a 3-day half-life means a week-old item counts ~20% of a fresh one.
RECENCY_HALF_LIFE_DAYS = 3.0

# Shorter headlines than this (in words) only dedup on exact equality, so a
# generic stub does not prefix-swallow distinct longer stories.
DEDUP_MIN_PREFIX_WORDS = 5

# Event categories → lowercase keyword fragments. A headline can match several.
# Event categories → keyword fragments (lowercase). A headline can match
# several. ASCII keywords match on word boundaries (see ``_contains``) to avoid
# substring false positives ("miss" vs "mission"); Korean keywords match as
# substrings (Hangul has no whitespace word boundaries between particles), which
# lets the KR market — where there is no Alpha Vantage sentiment — still get
# event tags from Naver headlines.
EVENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "earnings": (
        "earnings",
        "eps",
        "quarterly results",
        "q1",
        "q2",
        "q3",
        "q4",
        "revenue",
        "실적",
        "어닝",
        "영업이익",
        "순이익",
        "매출",
    ),
    "guidance": (
        "guidance",
        "outlook",
        "forecast",
        "cuts forecast",
        "raises forecast",
        "가이던스",
        "실적전망",
        "전망치",
    ),
    "ma": (
        "acquisition",
        "acquire",
        "acquires",
        "merger",
        "buyout",
        "takeover",
        "인수",
        "합병",
        "지분인수",
    ),
    "analyst": (
        "upgrade",
        "downgrade",
        "price target",
        "initiates",
        "overweight",
        "underweight",
        "reiterates",
        "목표주가",
        "투자의견",
        "매수의견",
    ),
    "regulatory_legal": (
        "lawsuit",
        "investigation",
        "antitrust",
        "probe",
        "fine",
        "settlement",
        "subpoena",
        "소송",
        "조사",
        "제재",
        "과징금",
        "검찰",
    ),
    "product": (
        "launch",
        "unveils",
        "approval",
        "fda",
        "partnership",
        "contract",
        "출시",
        "수주",
        "계약",
        "승인",
    ),
    "leadership": (
        "ceo",
        "cfo",
        "resign",
        "steps down",
        "appoints",
        "대표이사",
        "사임",
        "선임",
    ),
    "capital": (
        "offering",
        "dilution",
        "buyback",
        "repurchase",
        "dividend",
        "stock split",
        "spinoff",
        "유상증자",
        "무상증자",
        "자사주",
        "배당",
        "감자",
    ),
}

# Hard catalysts that should dominate a score regardless of soft sentiment.
# ASCII entries are matched on word boundaries; multi-word phrases are preferred
# over ambiguous single words (e.g. "earnings miss" not "miss", "trading halt"
# not "halt") so the -2 hard cap does not fire on "mission"/"twins"/"heartbeat".
NEGATIVE_CATALYSTS = (
    "bankrupt",
    "bankruptcy",
    "fraud",
    "delist",
    "going concern",
    "default",
    "trading halt",
    "downgrade",
    "downgrades",
    "recall",
    "recalls",
    "lawsuit",
    "investigation",
    "probe",
    "guidance cut",
    "cuts guidance",
    "profit warning",
    "earnings miss",
    "misses estimates",
    "slumps",
    "plunges",
    "상장폐지",
    "거래정지",
    "관리종목",
    "감자",
    "유상증자",
    "횡령",
    "배임",
    "적자전환",
    "급락",
)
POSITIVE_CATALYSTS = (
    "beats estimates",
    "tops estimates",
    "raises guidance",
    "record revenue",
    "upgrade",
    "upgrades",
    "fda approval",
    "wins contract",
    "to acquire",
    "acquires",
    "surges",
    "흑자전환",
    "신고가",
    "수주",
    "급등",
)


@dataclass
class NewsSignal:
    """Structured summary of a ticker's recent headlines.

    Args:
        unique_count: Headline count after near-duplicate removal.
        raw_count: Headline count before dedup.
        mean_sentiment: Unweighted mean of available sentiment scores, or None.
        recency_weighted_sentiment: Sentiment mean weighted by an exponential
            recency decay (fresher headlines count more), or None when no item
            carries a sentiment score.
        event_tags: Sorted list of catalyst categories present.
        has_positive_catalyst: A hard positive catalyst keyword appeared.
        has_negative_catalyst: A hard negative catalyst keyword appeared.
    """

    unique_count: int
    raw_count: int
    mean_sentiment: Optional[float]
    recency_weighted_sentiment: Optional[float]
    event_tags: list[str] = field(default_factory=list)
    has_positive_catalyst: bool = False
    has_negative_catalyst: bool = False


def _normalize(headline: str) -> str:
    """Lowercase, strip non-alphanumerics, collapse whitespace (for dedup)."""
    return re.sub(
        r"\s+", " ", re.sub(r"[^a-z0-9가-힣 ]", " ", headline.lower())
    ).strip()


def _contains(text_lower: str, keyword: str) -> bool:
    """Whether ``keyword`` occurs in ``text_lower`` as a meaningful match.

    ASCII keywords match on word boundaries (``\\bkeyword\\b``) so "miss" does
    not fire on "mission" and "wins" not on "twins". Korean keywords match as
    plain substrings — Hangul runs together without whitespace word boundaries,
    so substring is the correct (and lower-false-positive) test for terms like
    감자/유상증자.
    """
    if keyword.isascii():
        return re.search(r"\b" + re.escape(keyword) + r"\b", text_lower) is not None
    return keyword in text_lower


def classify_event(headline: str) -> list[str]:
    """Return the catalyst categories a headline matches (possibly empty).

    Args:
        headline: Article title.

    Returns:
        Sorted list of category keys from ``EVENT_KEYWORDS``.
    """
    h = headline.lower()
    return sorted(
        cat for cat, kws in EVENT_KEYWORDS.items() if any(_contains(h, k) for k in kws)
    )


def dedup_headlines(headlines: list[str]) -> list[str]:
    """Drop near-duplicate headlines, preserving first-seen order.

    Two headlines are duplicates when one normalized form is a word-prefix of
    the other (handles exact matches and wire reposts where one outlet appends a
    tail like "…, shares jump"). The shorter headline must have at least
    ``DEDUP_MIN_PREFIX_WORDS`` words to count as a prefix match, so a short
    generic headline ("Nvidia shares rise") does not swallow distinct stories
    that happen to start the same way. Returns survivors in first-seen order.

    Args:
        headlines: Raw headline strings.

    Returns:
        Deduplicated headlines in original order.
    """

    def _is_prefix(a: str, b: str) -> bool:
        # True when word-list a is a leading sublist of b, and a is long enough
        # that the shared prefix is specific rather than a generic stub.
        wa, wb = a.split(), b.split()
        if len(wa) < DEDUP_MIN_PREFIX_WORDS:
            return wa == wb  # too short to prefix-match; require exact equality
        return len(wa) <= len(wb) and wb[: len(wa)] == wa

    seen: list[str] = []
    out: list[str] = []
    for h in headlines:
        norm = _normalize(h)
        if not norm:
            continue
        if any(_is_prefix(norm, s) or _is_prefix(s, norm) for s in seen):
            continue
        seen.append(norm)
        out.append(h)
    return out


def _age_days(item_date: str, asof_date: str) -> Optional[float]:
    """Whole-day age of an item relative to asof, from ISO date strings.

    Both inputs use only their leading YYYY-MM-DD. Returns None on parse
    failure, and clamps negative ages (future-dated items) to 0.
    """
    try:
        from datetime import date

        di = date.fromisoformat(item_date[:10])
        da = date.fromisoformat(asof_date[:10])
    except (ValueError, TypeError, IndexError):
        return None
    return max((da - di).days, 0)


def summarize_news(items: list, asof_date: str) -> NewsSignal:
    """Build a NewsSignal from NewsItem-like objects (need .headline/.date/.sentiment_score).

    Args:
        items: NewsItem instances (or any object with ``headline``, ``date``,
            ``sentiment_score`` attributes).
        asof_date: ISO date used as the recency anchor (today, or a prediction's
            created_at for backfills).

    Returns:
        A NewsSignal. Sentiment fields are None when no item carries a score.
    """
    headlines = [getattr(it, "headline", "") or "" for it in items]
    unique = dedup_headlines(headlines)
    unique_set = set(unique)

    # Keep one item per surviving headline, first-seen.
    kept = []
    used: set[str] = set()
    for it in items:
        h = getattr(it, "headline", "") or ""
        if h in unique_set and h not in used:
            used.add(h)
            kept.append(it)

    tags: set[str] = set()
    pos_cat = neg_cat = False
    sent_vals: list[float] = []
    weighted_num = weighted_den = 0.0
    for it in kept:
        h = getattr(it, "headline", "") or ""
        tags.update(classify_event(h))
        hl = h.lower()
        if any(_contains(hl, k) for k in NEGATIVE_CATALYSTS):
            neg_cat = True
        if any(_contains(hl, k) for k in POSITIVE_CATALYSTS):
            pos_cat = True
        s = getattr(it, "sentiment_score", None)
        if s is not None:
            sent_vals.append(s)
            age = _age_days(getattr(it, "date", "") or "", asof_date)
            w = 0.5 ** (age / RECENCY_HALF_LIFE_DAYS) if age is not None else 1.0
            weighted_num += w * s
            weighted_den += w

    mean_sent = sum(sent_vals) / len(sent_vals) if sent_vals else None
    rw_sent = weighted_num / weighted_den if weighted_den > 0 else None
    return NewsSignal(
        unique_count=len(unique),
        raw_count=len(headlines),
        mean_sentiment=mean_sent,
        recency_weighted_sentiment=rw_sent,
        event_tags=sorted(tags),
        has_positive_catalyst=pos_cat,
        has_negative_catalyst=neg_cat,
    )
