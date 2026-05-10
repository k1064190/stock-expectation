"""Ingest predictions/news/disclosures into Neo4j.

Idempotent — uses ``MERGE`` on natural keys (ticker+market for stocks,
prediction id, news url, disclosure rcept_no). Safe to re-run; latest
attribute values overwrite previous.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

try:
    from .driver import GraphDriver
except ImportError:
    from driver import GraphDriver  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


@dataclass
class GraphIngestStats:
    """Counts for one ingestion pass."""

    stocks_upserted: int = 0
    predictions_upserted: int = 0
    outcomes_linked: int = 0
    errors: int = 0


# Prediction ingest with optional Outcome link in the same MERGE batch.
_PREDICTION_UPSERT_CYPHER = """
MERGE (s:Stock {ticker: $ticker, market: $market})
  ON CREATE SET s.name = $stock_name
  ON MATCH SET s.name = coalesce($stock_name, s.name)
MERGE (p:Prediction {id: $prediction_id})
  ON CREATE SET
    p.direction   = $direction,
    p.confidence  = $confidence,
    p.timeframe   = $timeframe,
    p.created_at  = datetime($created_at),
    p.status      = $status,
    p.entry_price = $entry_price
  ON MATCH SET
    p.status     = $status,
    p.confidence = $confidence
MERGE (p)-[:ABOUT]->(s)
WITH p
CALL {
  WITH p
  WITH p WHERE $outcome_status IN ['HIT', 'MISS', 'EXPIRED']
  MERGE (o:Outcome {prediction_id: p.id})
    ON CREATE SET
      o.status      = $outcome_status,
      o.return_pct  = $outcome_return,
      o.resolved_at = CASE WHEN $outcome_date IS NOT NULL
                           THEN datetime($outcome_date) ELSE null END
    ON MATCH SET
      o.status      = $outcome_status,
      o.return_pct  = $outcome_return,
      o.resolved_at = CASE WHEN $outcome_date IS NOT NULL
                           THEN datetime($outcome_date) ELSE null END
  MERGE (p)-[:RESOLVED_TO]->(o)
  RETURN o
}
RETURN p.id AS pid
"""


def ingest_predictions(
    conn: sqlite3.Connection,
    graph: GraphDriver,
    since_iso: str | None = None,
) -> GraphIngestStats:
    """Pull predictions (and any resolved outcomes) into the graph.

    Args:
        conn: SQLite connection to predictions.db.
        graph: Initialised GraphDriver.
        since_iso: Optional cutoff. When None, ingests every row.

    Returns:
        GraphIngestStats summarising the pass.
    """
    if since_iso:
        rows = conn.execute(
            "SELECT * FROM predictions WHERE created_at > ? ORDER BY created_at ASC",
            (since_iso,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM predictions ORDER BY created_at ASC"
        ).fetchall()

    stats = GraphIngestStats()
    seen_stocks: set[tuple[str, str]] = set()

    for row in rows:
        params = {
            "ticker": row["ticker"],
            "market": row["market"],
            "stock_name": row["ticker"],  # name comes from fundamentals later
            "prediction_id": row["id"],
            "direction": row["direction"],
            "confidence": float(row["confidence"]),
            "timeframe": row["timeframe"],
            "created_at": row["created_at"],
            "status": row["status"],
            "entry_price": float(row["entry_price"]),
            "outcome_status": (
                row["status"] if row["status"] in ("HIT", "MISS", "EXPIRED") else None
            ),
            "outcome_return": (
                float(row["outcome_return"])
                if row["outcome_return"] is not None
                else None
            ),
            "outcome_date": row["outcome_date"],
        }
        try:
            graph.run(_PREDICTION_UPSERT_CYPHER, **params)
            key = (row["ticker"], row["market"])
            if key not in seen_stocks:
                stats.stocks_upserted += 1
                seen_stocks.add(key)
            stats.predictions_upserted += 1
            if params["outcome_status"]:
                stats.outcomes_linked += 1
        except Exception as exc:
            logger.warning("graph ingest of prediction %s failed: %s", row["id"], exc)
            stats.errors += 1

    return stats


# News upsert (one MERGE per item).
_NEWS_UPSERT_CYPHER = """
MERGE (s:Stock {ticker: $ticker, market: $market})
MERGE (n:NewsEvent {url: $url})
  ON CREATE SET
    n.headline        = $headline,
    n.date            = date($date),
    n.source          = $source,
    n.sentiment_score = $sentiment_score
  ON MATCH SET
    n.sentiment_score = coalesce($sentiment_score, n.sentiment_score)
MERGE (n)-[:MENTIONS]->(s)
"""


def ingest_news_batch(
    items: list[dict],
    ticker: str,
    market: str,
    graph: GraphDriver,
) -> int:
    """Upsert a batch of news items for a single ticker.

    Items match the `NewsItem` dataclass shape from stock-cli news output:
    ``{headline, source, date, url, sentiment_score, sentiment_label}``.

    Returns:
        Count of items successfully upserted.
    """
    ok = 0
    for item in items:
        if not item.get("url"):
            # Without a stable URL we can't dedupe — skip.
            continue
        params = {
            "ticker": ticker,
            "market": market,
            "url": item["url"],
            "headline": item.get("headline", ""),
            "date": item.get("date") or None,
            "source": item.get("source", ""),
            "sentiment_score": item.get("sentiment_score"),
        }
        try:
            graph.run(_NEWS_UPSERT_CYPHER, **params)
            ok += 1
        except Exception as exc:
            logger.warning(
                "news upsert failed for %s url=%s: %s",
                ticker,
                item.get("url"),
                exc,
            )
    return ok


# Disclosure upsert.
_DISCLOSURE_UPSERT_CYPHER = """
MERGE (s:Stock {ticker: $ticker, market: 'KR'})
MERGE (d:Disclosure {rcept_no: $rcept_no})
  ON CREATE SET
    d.report_nm = $report_nm,
    d.flr_nm    = $flr_nm,
    d.rcept_dt  = $rcept_dt,
    d.url       = $url
MERGE (d)-[:FILED_BY]->(s)
"""


def ingest_disclosures_batch(
    items: list[dict],
    ticker: str,
    graph: GraphDriver,
) -> int:
    """Upsert KR disclosures for a single ticker."""
    ok = 0
    for item in items:
        if not item.get("rcept_no"):
            continue
        try:
            graph.run(
                _DISCLOSURE_UPSERT_CYPHER,
                ticker=ticker,
                rcept_no=item["rcept_no"],
                report_nm=item.get("report_nm", ""),
                flr_nm=item.get("flr_nm", ""),
                rcept_dt=item.get("rcept_dt", ""),
                url=item.get("url", ""),
            )
            ok += 1
        except Exception as exc:
            logger.warning(
                "disclosure upsert failed for %s rcept_no=%s: %s",
                ticker,
                item.get("rcept_no"),
                exc,
            )
    return ok


def parse_signals_used(raw: str) -> list[str]:
    """Predictions store ``signals_used`` as a JSON array string."""
    if not raw:
        return []
    try:
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except json.JSONDecodeError:
        return []
