"""Cypher snippets + node/edge type definitions for the trading graph.

Schema overview:

  Nodes
    Stock         {ticker, market, name}
    Sector        {name}
    Theme         {name, lifecycle_stage}
    NewsEvent     {url, headline, date, sentiment_score, source}
    Disclosure    {rcept_no, report_nm, rcept_dt, market}
    Prediction    {id, direction, confidence, timeframe, created_at, status}
    Outcome       {status, return_pct, resolved_at}

  Relationships
    (Stock)-[:IN_SECTOR]->(Sector)
    (Stock)-[:LINKED_TO {strength}]->(Theme)
    (Prediction)-[:ABOUT]->(Stock)
    (Prediction)-[:RESOLVED_TO]->(Outcome)
    (NewsEvent)-[:MENTIONS]->(Stock)
    (Disclosure)-[:FILED_BY]->(Stock)

The schema is small on purpose. Theme assignment uses Stage 7-A's mem0
semantic similarity at ingestion time — we don't try to store the
similarity score here, just the discrete LINKED_TO edge.
"""

from __future__ import annotations

# Constraints + indexes to run once via `stock-cli graph init`.
# Each statement is idempotent under Cypher's IF NOT EXISTS clause.
INIT_STATEMENTS: tuple[str, ...] = (
    # Uniqueness constraints.
    "CREATE CONSTRAINT stock_ticker IF NOT EXISTS "
    "FOR (s:Stock) REQUIRE (s.ticker, s.market) IS UNIQUE",
    "CREATE CONSTRAINT sector_name IF NOT EXISTS "
    "FOR (s:Sector) REQUIRE s.name IS UNIQUE",
    "CREATE CONSTRAINT theme_name IF NOT EXISTS "
    "FOR (t:Theme) REQUIRE t.name IS UNIQUE",
    "CREATE CONSTRAINT prediction_id IF NOT EXISTS "
    "FOR (p:Prediction) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT disclosure_rcept IF NOT EXISTS "
    "FOR (d:Disclosure) REQUIRE d.rcept_no IS UNIQUE",
    "CREATE CONSTRAINT news_url IF NOT EXISTS "
    "FOR (n:NewsEvent) REQUIRE n.url IS UNIQUE",
    # Lookup indexes.
    "CREATE INDEX news_date IF NOT EXISTS FOR (n:NewsEvent) ON (n.date)",
    "CREATE INDEX prediction_created IF NOT EXISTS "
    "FOR (p:Prediction) ON (p.created_at)",
    "CREATE INDEX disclosure_dt IF NOT EXISTS " "FOR (d:Disclosure) ON (d.rcept_dt)",
)

# Convenience canned queries — strings, not callables, so callers can
# parameterise as they like.
CANNED_QUERIES: dict[str, str] = {
    # Top-N stocks sharing themes with the given ticker.
    "similar_stocks_by_theme": """
        MATCH (input:Stock {ticker: $ticker, market: $market})-[:LINKED_TO]->(t:Theme)
              <-[:LINKED_TO]-(other:Stock)
        WHERE other.ticker <> input.ticker OR other.market <> input.market
        WITH other, collect(t.name) AS shared_themes, count(t) AS shared_count
        RETURN other.ticker AS ticker,
               other.market AS market,
               other.name   AS name,
               shared_count,
               shared_themes
        ORDER BY shared_count DESC
        LIMIT $limit
    """,
    # Win rate per theme over the last N weeks.
    "theme_winners_recent": """
        MATCH (p:Prediction)-[:ABOUT]->(s:Stock)-[:LINKED_TO]->(t:Theme),
              (p)-[:RESOLVED_TO]->(o:Outcome)
        WHERE p.created_at >= datetime() - duration({weeks: $weeks})
        WITH t.name AS theme,
             count(p) AS total,
             sum(CASE WHEN o.status = 'HIT' THEN 1 ELSE 0 END) AS wins
        WHERE total >= 3
        RETURN theme,
               total,
               wins,
               toFloat(wins) / total AS win_rate
        ORDER BY win_rate DESC, total DESC
        LIMIT $limit
    """,
    # Stocks mentioned in news with a given negative keyword in last N days.
    "stocks_with_negative_news": """
        MATCH (n:NewsEvent)-[:MENTIONS]->(s:Stock)
        WHERE n.date >= date() - duration({days: $days})
          AND ANY(kw IN $keywords WHERE toLower(n.headline) CONTAINS toLower(kw))
        RETURN s.ticker AS ticker,
               s.market AS market,
               collect(DISTINCT n.headline)[..5] AS sample_headlines,
               count(DISTINCT n) AS news_count
        ORDER BY news_count DESC
        LIMIT $limit
    """,
}
