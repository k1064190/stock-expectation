"""Theme auto-extraction from candidate news (Stage B).

Pipeline:
    Stage A candidates → ``fetch_news_for_candidates`` (parallel Naver scrape
    via existing ``KoreanMarketProvider.get_news``) → ``cluster_news``
    (cheap n-gram count) → ``format_themes_for_prompt`` (Korean markdown
    block injected into the briefing).

The clusterer is intentionally dumb — pure stdlib, no sklearn, no
sentence-transformers, no KoNLPy. The goal is just to surface n-grams
that recur across ≥3 distinct candidate tickers within the last 7 days
of news. When that happens it's usually a real theme (e.g. "피지컬 AI",
"자율주행"); when it doesn't, the briefing's "Active Themes" section
gracefully empties.

Stage B side-effects on the Candidate list:
  - Backfills ``Candidate.news_count_7d`` so the Stage A re-rank can pick
    up news-driven names that just-barely missed the momentum filter.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

from candidate_discovery import Candidate

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ThemeCluster:
    """A set of n-gram keywords co-occurring across ≥N candidate tickers.

    Args:
        keywords: Lowercased tokens forming the cluster's n-gram (length
            2 or 3). Order preserved from the original headline.
        tickers: Distinct candidate tickers whose headlines contain the
            n-gram. Sorted alphabetically for deterministic output.
        sample_headlines: Up to five representative headlines (one per
            ticker, dedup'd by exact match) for prompt rendering.
        headline_count: Total number of headlines (across all tickers)
            that contain the n-gram. Used for ranking.
    """

    keywords: tuple[str, ...]
    tickers: list[str]
    sample_headlines: list[str] = field(default_factory=list)
    headline_count: int = 0


# ---------------------------------------------------------------------------
# Stopwords — small inline set, intentionally not configurable
# ---------------------------------------------------------------------------

# Korean / market noise. Anything that appears in nearly every KR business
# headline and conveys no theme signal.
_KO_STOPWORDS: frozenset[str] = frozenset(
    {
        "속보",
        "특징주",
        "단독",
        "분석",
        "전망",
        "공시",
        "종목",
        "기업",
        "회사",
        "주식",
        "주가",
        "시장",
        "코스피",
        "코스닥",
        "한국",
        "미국",
        "중국",
        "일본",
        "오늘",
        "어제",
        "내일",
        "이번",
        "지난",
        "최근",
        "기자",
        "보도",
        "뉴스",
        "발표",
        "참고",
        "관련",
        "이상",
        "이하",
        "이내",
        "이후",
        "이전",
        "원",
        "달러",
        "조원",
        "억원",
        "만원",
        # Price-action verbs — recur in nearly every momentum headline and
        # would otherwise form spurious "all candidates have 상승" clusters.
        # We want themes (피지컬 AI, 자율주행), not direction.
        "상승",
        "하락",
        "강세",
        "약세",
        "돌파",
        "급등",
        "급락",
        "반등",
        "상한가",
        "하한가",
        "신고가",
        "신저가",
        "매수",
        "매도",
    }
)

# Generic English particles that show up in mixed-language KR headlines
# (often product names like "AI", "ETF"). We DO keep short tokens like
# "ai", "etf", "ev", "ipo" because they're load-bearing theme tokens.
_EN_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "by",
        "and",
        "or",
        "but",
        "with",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "this",
        "that",
        "these",
        "those",
        "it",
    }
)

# Token must be ≥2 chars after stripping punctuation, and either Hangul/Latin/
# digit-bearing. Tokens that are pure punctuation, single ASCII letters,
# or a known stopword are dropped before n-gram generation.
_TOKEN_RE = re.compile(r"[A-Za-z가-힣0-9]+")


# ---------------------------------------------------------------------------
# News fetching
# ---------------------------------------------------------------------------


def fetch_news_for_candidates(
    cands: list[Candidate],
    days: int = 7,
    max_workers: int = 8,
    provider: Optional[object] = None,
) -> dict[str, list[dict]]:
    """Pull ``get_news`` for every candidate ticker in parallel.

    Args:
        cands: Stage A candidates.
        days: ``since_days`` argument passed to ``get_news``.
        max_workers: ThreadPoolExecutor cap. Naver tolerates ~8 parallel
            connections; higher invites rate-limiting.
        provider: Injectable for tests. Defaults to a fresh
            ``KoreanMarketProvider`` instance.

    Returns:
        ``{ticker: [news_items]}``. Tickers that errored or returned no
        news map to an empty list — caller need not check failure mode.
        ``news_items`` is whatever the provider returns; we don't shape
        it here so the dataclass-vs-dict distinction stays loose.
    """
    if not cands:
        return {}

    if provider is None:
        # Lazy import: avoids a circular if the provider module ever
        # grows a dependency on this file (won't right now, but cheap
        # safety).
        from providers.kr import KoreanMarketProvider

        provider = KoreanMarketProvider()

    out: dict[str, list[dict]] = {c.ticker: [] for c in cands}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_ticker = {
            executor.submit(provider.get_news, c.ticker, 10, days): c.ticker
            for c in cands
        }
        for future in as_completed(future_to_ticker):
            ticker = future_to_ticker[future]
            try:
                items = future.result() or []
            except Exception as exc:
                logger.warning("news fetch failed for %s: %s", ticker, exc)
                items = []
            # Normalise to dict shape — provider returns NewsItem dataclass,
            # tests pass dict fixtures, both have .headline / ["headline"]
            # access. Convert dataclass→dict once for downstream ngram code.
            out[ticker] = [_normalise_news(it) for it in items]
    return out


def _normalise_news(item) -> dict:
    """Coerce a NewsItem dataclass *or* a dict into the dict shape used
    here (``headline`` / ``date`` / ``url``)."""
    if isinstance(item, dict):
        return item
    # Dataclass — read attributes by name.
    return {
        "headline": getattr(item, "headline", ""),
        "date": getattr(item, "date", ""),
        "url": getattr(item, "url", ""),
        "source": getattr(item, "source", ""),
    }


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def cluster_news(
    news_by_ticker: dict[str, list[dict]],
    min_cluster_size: int = 3,
    ngram_sizes: tuple[int, ...] = (2, 3),
    max_themes: int = 8,
) -> list[ThemeCluster]:
    """Find n-grams that appear in headlines across ≥N distinct tickers.

    Args:
        news_by_ticker: Output of ``fetch_news_for_candidates``.
        min_cluster_size: Minimum distinct tickers an n-gram must hit to
            qualify as a theme.
        ngram_sizes: Which n-gram lengths to consider. Default ``(2, 3)``
            — unigrams are too noisy (single common words form spurious
            clusters); 4-grams are too specific (rarely cross 3 tickers).
        max_themes: Cap on returned cluster count.

    Returns:
        Clusters sorted by (ticker_count desc, headline_count desc).
        Supersets win: when two n-grams share the exact same ticker set,
        the longer one is kept and the shorter dropped. This is what
        turns ``("ai",)`` + ``("피지컬", "ai")`` over the same 3 tickers
        into one cluster keyed by the bigram.
    """
    if not news_by_ticker:
        return []

    # Map ngram → {ticker: [headlines containing ngram]}.
    ngram_index: dict[tuple[str, ...], dict[str, list[str]]] = {}

    for ticker, items in news_by_ticker.items():
        for item in items:
            headline = (item.get("headline") or "").strip()
            if not headline:
                continue
            tokens = _tokenise(headline)
            # Within a single headline, dedupe n-grams so a headline that
            # repeats the same phrase ("AI 인프라 도입, AI 인프라 비용") only
            # contributes one entry per cluster (Codex C3 fix: headline_count
            # must count headlines, not n-gram occurrences).
            seen_in_headline: set[tuple[str, ...]] = set()
            for n in ngram_sizes:
                for ngram in _ngrams(tokens, n):
                    if ngram in seen_in_headline:
                        continue
                    seen_in_headline.add(ngram)
                    bucket = ngram_index.setdefault(ngram, {})
                    bucket.setdefault(ticker, []).append(headline)

    # First pass: keep only ngrams hitting ≥min_cluster_size distinct tickers.
    candidates: list[ThemeCluster] = []
    for ngram, ticker_to_headlines in ngram_index.items():
        if len(ticker_to_headlines) < min_cluster_size:
            continue
        tickers = sorted(ticker_to_headlines.keys())
        headline_count = sum(len(v) for v in ticker_to_headlines.values())
        samples = _pick_samples(ticker_to_headlines)
        candidates.append(
            ThemeCluster(
                keywords=ngram,
                tickers=tickers,
                sample_headlines=samples,
                headline_count=headline_count,
            )
        )

    # Second pass: substring dedup. Two distinct themes on the same ticker
    # set must both survive (Codex C1 fix) — earlier we keyed dedup on the
    # ticker tuple itself, which collapsed "피지컬 ai" and "자율주행 로봇"
    # into one cluster when both hit the same 3 tickers. Now we drop only
    # the shorter n-gram when it is a contiguous subsequence of a longer
    # n-gram with the same ticker set (e.g. ``("ai",)`` ⊂ ``("피지컬", "ai")``).
    candidates_sorted = sorted(
        candidates,
        key=lambda c: (len(c.keywords), len(c.tickers), c.headline_count),
        reverse=True,
    )
    deduped: list[ThemeCluster] = []
    for c in candidates_sorted:
        if any(
            tuple(c.tickers) == tuple(kept.tickers)
            and _is_subseq(c.keywords, kept.keywords)
            for kept in deduped
        ):
            continue
        deduped.append(c)
    deduped.sort(
        key=lambda c: (len(c.tickers), c.headline_count),
        reverse=True,
    )
    return deduped[:max_themes]


def _is_subseq(short_kw: tuple[str, ...], long_kw: tuple[str, ...]) -> bool:
    """True if ``short_kw`` is a strictly-shorter contiguous subsequence of
    ``long_kw``. Used by the substring-dedup pass in ``cluster_news``.

    Examples:
        _is_subseq(("ai",), ("피지컬", "ai"))            → True
        _is_subseq(("ai", "인프라"), ("ai", "인프라"))      → False (identical, not strictly shorter)
        _is_subseq(("ai", "인프라"), ("ai", "인프라", "정책")) → True
        _is_subseq(("ai",), ("자율주행", "로봇"))          → False (no shared token)
    """
    if len(short_kw) >= len(long_kw):
        return False
    for i in range(len(long_kw) - len(short_kw) + 1):
        if long_kw[i : i + len(short_kw)] == short_kw:
            return True
    return False


def _tokenise(text: str) -> list[str]:
    """Lowercased token stream with stopwords removed.

    Korean characters survive intact (they're matched by ``[가-힣]+``);
    everything outside [A-Za-z가-힣0-9] becomes a token boundary.
    """
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text):
        tok = raw.lower()
        if len(tok) < 2:
            continue
        if tok in _KO_STOPWORDS or tok in _EN_STOPWORDS:
            continue
        tokens.append(tok)
    return tokens


def _ngrams(tokens: list[str], n: int) -> list[tuple[str, ...]]:
    if n <= 0 or len(tokens) < n:
        return []
    return [tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


def _pick_samples(ticker_to_headlines: dict[str, list[str]]) -> list[str]:
    """One sample headline per ticker, capped at five, deduplicated."""
    seen: set[str] = set()
    out: list[str] = []
    for ticker in sorted(ticker_to_headlines.keys()):
        for headline in ticker_to_headlines[ticker]:
            if headline in seen:
                continue
            seen.add(headline)
            out.append(headline)
            break  # one per ticker
        if len(out) >= 5:
            break
    return out


# ---------------------------------------------------------------------------
# News-count backfill (for Stage A re-rank)
# ---------------------------------------------------------------------------


def backfill_news_counts(
    cands: list[Candidate],
    news_by_ticker: dict[str, list[dict]],
) -> None:
    """Mutate each Candidate's ``news_count_7d`` in place.

    Stage A leaves the field as 0 (news fetch is expensive); Stage B
    fills it after the parallel fetch so the briefing prompt and any
    downstream re-rank logic has live numbers.
    """
    for c in cands:
        c.news_count_7d = len(news_by_ticker.get(c.ticker, []))


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def format_themes_for_prompt(clusters: list[ThemeCluster]) -> str:
    """Render the theme cluster set as a Korean markdown block.

    Empty-cluster case returns a single-line "no themes" note rather
    than an empty string, so the prompt template's ``{active_themes}``
    placeholder always renders as a non-empty section.

    Args:
        clusters: Output of ``cluster_news``.

    Returns:
        Multi-line markdown. Each cluster shows the keyword(s), ticker
        count, total headline count, ticker list, and one sample
        headline.
    """
    if not clusters:
        return (
            "## Active Themes (지난 7일 뉴스 클러스터링)\n"
            "  (≥3 종목 공유 테마 없음 — 후보군 내 news flow가 분산되어 있음.)"
        )

    lines = ["## Active Themes (지난 7일 뉴스 클러스터링)"]
    for c in clusters:
        kw = " ".join(c.keywords)
        ticker_list = ", ".join(c.tickers)
        lines.append(
            f"- {kw} [{len(c.tickers)}종목, {c.headline_count} 헤드라인]: "
            f"{ticker_list}"
        )
        if c.sample_headlines:
            lines.append(f'  예시: "{c.sample_headlines[0]}"')
    return "\n".join(lines)
