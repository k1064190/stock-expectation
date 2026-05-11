"""Schemas + category definitions for the memory layer.

Memory is partitioned by ``category`` (mem0's ``user_id`` concept). Each
category has its own retention bias and ingest source.

Schemas are intentionally loose — mem0 extracts facts at write time and
reconciles updates against existing memories. We pass structured JSON
strings as content; mem0's fact extractor reads the JSON and stores its
own derived memories on top.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CategoryName = Literal[
    "predictions",
    "news_events",
    "themes",
    "outcomes",
    "transmission_chains",
]

CATEGORIES: tuple[CategoryName, ...] = (
    "predictions",
    "news_events",
    "themes",
    "outcomes",
    "transmission_chains",
)

SCHEMA_VERSION = "v1"


@dataclass
class MemoryRecord:
    """The minimal envelope we send to mem0.

    The ``content`` field is what gets embedded and fact-extracted.
    Everything else is metadata that mem0 stores alongside.

    Args:
        category: One of the canonical category names.
        content: Free-form text or JSON string. Embedded by sentence-transformers.
        metadata: Structured extras (ticker, market, run_id, ...).
            Searchable as filters but not part of the embedding.
    """

    category: CategoryName
    content: str
    metadata: dict


@dataclass
class SearchHit:
    """A single result from a memory search.

    Args:
        memory: The memory text (mem0's fact-extracted version, not the
            original content).
        score: Similarity score in [0, 1] — higher is closer.
        metadata: Stored alongside the memory.
        memory_id: mem0-assigned identifier for delete/update operations.
    """

    memory: str
    score: float
    metadata: dict
    memory_id: str
