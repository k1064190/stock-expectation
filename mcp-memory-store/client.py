"""Memory store client — thin wrapper over mem0 with lazy import.

mem0 + qdrant-client + sentence-transformers are heavy and behind the
``memory`` extra (``uv sync --extra memory``). We import them lazily so the
rest of the project — including the CLI itself — loads without them.
Calls that actually touch the store raise a clear ``MemoryUnavailable`` if
the deps aren't installed, with the install instruction in the message.

Default config:
- Backing vector store: Qdrant in embedded mode at ``data/memory/qdrant/``
- Embedder: sentence-transformers ``all-MiniLM-L6-v2`` (local, no API key)
- LLM (for fact extraction): pluggable; uses Anthropic Haiku 4.5 if
  ``ANTHROPIC_API_KEY`` is set, otherwise mem0's fallback to OpenAI or
  local. Without any LLM config, mem0 will store raw content but skip
  fact extraction.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

try:
    # Package-style import (works when the dir is loaded via importlib).
    from .schemas import CATEGORIES, MemoryRecord, SearchHit
except ImportError:
    # Flat-style import (works when ``mcp-memory-store`` is on sys.path and
    # the file is loaded directly — this is the pattern stock_cli.py uses
    # for sibling modules such as mcp-prediction-store).
    from schemas import CATEGORIES, MemoryRecord, SearchHit  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


class MemoryUnavailable(RuntimeError):
    """Raised when mem0 or its deps are not installed."""


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QDRANT_PATH = PROJECT_ROOT / "data" / "memory" / "qdrant"


def _build_default_config(qdrant_path: Path) -> dict:
    """Construct a mem0 config dict using local Qdrant + sentence-transformers."""
    qdrant_path.mkdir(parents=True, exist_ok=True)
    return {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "path": str(qdrant_path),
                "on_disk": True,
            },
        },
        "embedder": {
            "provider": "huggingface",
            "config": {"model": "sentence-transformers/all-MiniLM-L6-v2"},
        },
    }


class MemoryStore:
    """Lazy mem0 wrapper with category-as-user_id mapping.

    Use a single instance per process. ``ensure_ready()`` is idempotent
    and triggers the heavy import; until you call any read/write method,
    no mem0 deps are touched.
    """

    def __init__(self, qdrant_path: Optional[Path] = None) -> None:
        self._qdrant_path = qdrant_path or DEFAULT_QDRANT_PATH
        self._client = None  # type: ignore[assignment]

    def ensure_ready(self) -> None:
        """Import + initialise the mem0 client. No-op after the first call."""
        if self._client is not None:
            return
        try:
            from mem0 import Memory  # type: ignore[import-not-found]
        except ImportError as exc:
            raise MemoryUnavailable(
                "mem0 is not installed. Run: uv sync --extra memory"
            ) from exc

        config = _build_default_config(self._qdrant_path)
        try:
            self._client = Memory.from_config(config)
        except Exception as exc:
            raise MemoryUnavailable(
                f"mem0 init failed (likely missing optional dep — qdrant-client or "
                f"sentence-transformers): {exc}"
            ) from exc

    def add(self, record: MemoryRecord) -> str:
        """Store a record. Returns mem0's assigned memory_id (best-effort)."""
        self._validate_category(record.category)
        self.ensure_ready()
        result = self._client.add(  # type: ignore[union-attr]
            messages=record.content,
            user_id=record.category,
            metadata=record.metadata,
        )
        # mem0 returns either a string id or a list of dicts depending on
        # version; flatten to a stable shape.
        if isinstance(result, dict):
            return str(result.get("id") or result.get("memory_id") or "")
        if isinstance(result, list) and result:
            first = result[0]
            return str(first.get("id") or first.get("memory_id") or "")
        return str(result) if result else ""

    def search(self, query: str, category: str, limit: int = 5) -> list[SearchHit]:
        """Semantic search within a category. Newest- and most-similar-first."""
        self._validate_category(category)
        self.ensure_ready()
        raw = self._client.search(  # type: ignore[union-attr]
            query=query, user_id=category, limit=limit
        )
        return [self._to_search_hit(item) for item in self._iter_results(raw)]

    def stats(self) -> dict[str, int]:
        """Return per-category counts. Best-effort — mem0 doesn't expose a
        single endpoint, so we run an empty search and count.
        """
        self.ensure_ready()
        out: dict[str, int] = {}
        for cat in CATEGORIES:
            try:
                results = self._client.get_all(user_id=cat)  # type: ignore[union-attr]
                out[cat] = len(self._iter_results(results))
            except Exception as exc:
                logger.warning("stats() failed for %s: %s", cat, exc)
                out[cat] = -1
        return out

    def purge(self, category: str) -> int:
        """Drop all memories in a category. Returns count removed (best-effort)."""
        self._validate_category(category)
        self.ensure_ready()
        try:
            results = self._client.get_all(user_id=category)  # type: ignore[union-attr]
            entries = self._iter_results(results)
            count = 0
            for entry in entries:
                mid = entry.get("id") or entry.get("memory_id")
                if not mid:
                    continue
                try:
                    self._client.delete(memory_id=mid)  # type: ignore[union-attr]
                    count += 1
                except Exception as exc:
                    logger.warning("delete %s failed: %s", mid, exc)
            return count
        except Exception as exc:
            raise MemoryUnavailable(f"purge failed: {exc}") from exc

    @staticmethod
    def _validate_category(category: str) -> None:
        if category not in CATEGORIES:
            raise ValueError(
                f"Unknown category {category!r}. Valid: {', '.join(CATEGORIES)}"
            )

    @staticmethod
    def _iter_results(raw) -> list[dict]:
        """Normalise mem0's varying return shape across versions."""
        if raw is None:
            return []
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            # Newer mem0 wraps results in {"results": [...]} or {"memories": [...]}
            for key in ("results", "memories", "data"):
                if key in raw and isinstance(raw[key], list):
                    return raw[key]
        return []

    @staticmethod
    def _to_search_hit(item: dict) -> SearchHit:
        return SearchHit(
            memory=str(item.get("memory") or item.get("text") or ""),
            score=float(item.get("score") or 0.0),
            metadata=dict(item.get("metadata") or {}),
            memory_id=str(item.get("id") or item.get("memory_id") or ""),
        )
