"""Neo4j driver wrapper — lazy import + read/write helpers.

Connection params come from env (see ``.env.example`` once added):

  NEO4J_URI       e.g. bolt://localhost:7687
  NEO4J_USER      defaults to "neo4j"
  NEO4J_PASSWORD  required for any non-init call

Like ``mcp-memory-store``, the heavy ``neo4j`` package is behind a
``graph`` extra — install with ``uv sync --extra graph``. The CLI raises
``GraphUnavailable`` with a clear hint when the dep is missing or the
container isn't running.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


class GraphUnavailable(RuntimeError):
    """Raised when the neo4j driver cannot be imported or cannot connect."""


def _resolve_config() -> dict:
    """Pull connection params from env with sensible defaults."""
    return {
        "uri": os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        "user": os.environ.get("NEO4J_USER", "neo4j"),
        "password": os.environ.get("NEO4J_PASSWORD", ""),
    }


class GraphDriver:
    """Thin wrapper over neo4j.GraphDatabase.driver.

    Lazy-imports the ``neo4j`` package on first use; raises a structured
    error message — including install + container hints — when anything
    in the chain fails. Callers should use ``session()`` as a context
    manager for read/write batches.
    """

    def __init__(self) -> None:
        self._driver = None  # type: ignore[assignment]
        self._config: Optional[dict] = None

    def ensure_ready(self) -> None:
        """Import + open the driver. Idempotent.

        Verifies authentication eagerly so misconfigurations surface
        before any query is issued.
        """
        if self._driver is not None:
            return
        try:
            from neo4j import GraphDatabase  # type: ignore[import-not-found]
        except ImportError as exc:
            raise GraphUnavailable(
                "neo4j driver is not installed. Run: uv sync --extra graph"
            ) from exc

        self._config = _resolve_config()
        if not self._config["password"]:
            raise GraphUnavailable(
                "NEO4J_PASSWORD env var is required (set in .env or shell)."
            )

        try:
            self._driver = GraphDatabase.driver(
                self._config["uri"],
                auth=(self._config["user"], self._config["password"]),
            )
            # Cheap auth check — fails fast if container isn't up or creds bad.
            self._driver.verify_connectivity()
        except Exception as exc:
            raise GraphUnavailable(
                f"Could not connect to Neo4j at {self._config['uri']}: {exc}. "
                "Is the container up? `docker compose up -d neo4j`."
            ) from exc

    def close(self) -> None:
        """Close the underlying driver if it was opened."""
        if self._driver is not None:
            try:
                self._driver.close()
            finally:
                self._driver = None

    @contextmanager
    def session(self) -> Iterator:
        """Yield a neo4j session. The driver is ensured before yielding."""
        self.ensure_ready()
        session = self._driver.session()  # type: ignore[union-attr]
        try:
            yield session
        finally:
            session.close()

    def run(self, cypher: str, **params) -> list[dict]:
        """Execute a single Cypher statement. Returns a list of records.

        Each record is converted to a plain dict so callers don't need
        the neo4j module imported.
        """
        with self.session() as session:
            result = session.run(cypher, **params)
            return [dict(record) for record in result]

    def run_many(self, statements: list[str]) -> int:
        """Execute a list of Cypher statements as separate transactions.

        Returns the count of statements that ran without raising. Each
        statement is logged on failure but doesn't abort the rest — useful
        for the init step where some constraints may already exist.
        """
        ok = 0
        with self.session() as session:
            for stmt in statements:
                try:
                    session.run(stmt)
                    ok += 1
                except Exception as exc:
                    logger.warning("init statement failed: %s — %s", stmt, exc)
        return ok
