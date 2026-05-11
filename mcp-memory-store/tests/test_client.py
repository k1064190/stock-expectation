"""Tests for MemoryStore — mem0 itself is mocked, no heavy deps required."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from importlib import import_module

mem_module = import_module("mcp-memory-store.client")
schemas_module = import_module("mcp-memory-store.schemas")

MemoryStore = mem_module.MemoryStore
MemoryUnavailable = mem_module.MemoryUnavailable
MemoryRecord = schemas_module.MemoryRecord
SearchHit = schemas_module.SearchHit
CATEGORIES = schemas_module.CATEGORIES


def test_categories_are_canonical():
    assert "predictions" in CATEGORIES
    assert "outcomes" in CATEGORIES
    assert "transmission_chains" in CATEGORIES


def test_validate_category_rejects_unknown():
    store = MemoryStore()
    with pytest.raises(ValueError, match="Unknown category"):
        store.search("query", category="unknown")


def test_ensure_ready_raises_when_mem0_missing(tmp_path):
    store = MemoryStore(qdrant_path=tmp_path / "qdrant")

    # Force the import to fail by making mem0 unimportable.
    with patch.dict(sys.modules, {"mem0": None}):
        with pytest.raises(MemoryUnavailable, match="uv sync --extra memory"):
            store.ensure_ready()


def _build_mock_mem0():
    """Build a fake mem0 module + Memory class for injection."""
    fake_memory = MagicMock(name="fake_memory")
    fake_memory_class = MagicMock(name="MemoryClass")
    fake_memory_class.from_config.return_value = fake_memory

    fake_mem0_module = MagicMock(name="mem0_module")
    fake_mem0_module.Memory = fake_memory_class
    return fake_mem0_module, fake_memory


def test_add_normalises_dict_response(tmp_path):
    fake_mem0, fake_client = _build_mock_mem0()
    fake_client.add.return_value = {"id": "abc-123", "memory": "stored"}

    store = MemoryStore(qdrant_path=tmp_path / "q")
    with patch.dict(sys.modules, {"mem0": fake_mem0}):
        store.ensure_ready()
        rec = MemoryRecord(
            category="predictions",
            content="NVDA BULL 0.72",
            metadata={"ticker": "NVDA"},
        )
        memory_id = store.add(rec)
    assert memory_id == "abc-123"
    fake_client.add.assert_called_once()
    kwargs = fake_client.add.call_args.kwargs
    assert kwargs["user_id"] == "predictions"
    assert kwargs["metadata"] == {"ticker": "NVDA"}


def test_add_normalises_list_response(tmp_path):
    fake_mem0, fake_client = _build_mock_mem0()
    fake_client.add.return_value = [{"id": "list-id-1"}]

    store = MemoryStore(qdrant_path=tmp_path / "q")
    with patch.dict(sys.modules, {"mem0": fake_mem0}):
        store.ensure_ready()
        rec = MemoryRecord(category="news_events", content="x", metadata={})
        assert store.add(rec) == "list-id-1"


def test_search_normalises_results(tmp_path):
    fake_mem0, fake_client = _build_mock_mem0()
    fake_client.search.return_value = [
        {
            "memory": "match A",
            "score": 0.91,
            "metadata": {"ticker": "NVDA"},
            "id": "m1",
        },
        {
            "text": "match B",
            "score": 0.72,
            "metadata": {"ticker": "AMD"},
            "memory_id": "m2",
        },
    ]

    store = MemoryStore(qdrant_path=tmp_path / "q")
    with patch.dict(sys.modules, {"mem0": fake_mem0}):
        store.ensure_ready()
        hits = store.search("AI infrastructure", category="predictions", limit=5)

    assert len(hits) == 2
    assert hits[0].memory == "match A"
    assert hits[0].score == pytest.approx(0.91)
    assert hits[0].memory_id == "m1"
    # Second item used `text` and `memory_id` aliases.
    assert hits[1].memory == "match B"
    assert hits[1].memory_id == "m2"


def test_search_handles_dict_wrapped_results(tmp_path):
    """Newer mem0 wraps results in {"results": [...]}."""
    fake_mem0, fake_client = _build_mock_mem0()
    fake_client.search.return_value = {
        "results": [{"memory": "wrapped", "score": 0.5, "metadata": {}, "id": "w1"}]
    }

    store = MemoryStore(qdrant_path=tmp_path / "q")
    with patch.dict(sys.modules, {"mem0": fake_mem0}):
        store.ensure_ready()
        hits = store.search("anything", category="predictions")

    assert len(hits) == 1
    assert hits[0].memory == "wrapped"


def test_stats_counts_per_category(tmp_path):
    fake_mem0, fake_client = _build_mock_mem0()

    # Make get_all return varying counts per category.
    def fake_get_all(user_id):
        return {
            "predictions": [{"id": "1"}, {"id": "2"}, {"id": "3"}],
            "outcomes": [{"id": "o1"}],
            "news_events": [],
            "themes": [],
            "transmission_chains": [{"id": "tc1"}, {"id": "tc2"}],
        }[user_id]

    fake_client.get_all.side_effect = fake_get_all

    store = MemoryStore(qdrant_path=tmp_path / "q")
    with patch.dict(sys.modules, {"mem0": fake_mem0}):
        store.ensure_ready()
        counts = store.stats()

    assert counts["predictions"] == 3
    assert counts["outcomes"] == 1
    assert counts["news_events"] == 0
    assert counts["transmission_chains"] == 2


def test_stats_handles_per_category_failure_without_aborting(tmp_path):
    fake_mem0, fake_client = _build_mock_mem0()

    def fake_get_all(user_id):
        if user_id == "themes":
            raise RuntimeError("simulated mem0 hiccup")
        return [{"id": "1"}]

    fake_client.get_all.side_effect = fake_get_all

    store = MemoryStore(qdrant_path=tmp_path / "q")
    with patch.dict(sys.modules, {"mem0": fake_mem0}):
        store.ensure_ready()
        counts = store.stats()

    # Failed category gets -1 sentinel; others still report.
    assert counts["themes"] == -1
    assert counts["predictions"] == 1


def test_purge_deletes_known_ids(tmp_path):
    fake_mem0, fake_client = _build_mock_mem0()
    fake_client.get_all.return_value = [
        {"id": "p1"},
        {"id": "p2"},
        {"memory_id": "p3"},
        {"no_id_here": True},  # should be skipped, not crash
    ]

    store = MemoryStore(qdrant_path=tmp_path / "q")
    with patch.dict(sys.modules, {"mem0": fake_mem0}):
        store.ensure_ready()
        deleted = store.purge("predictions")

    assert deleted == 3
    assert fake_client.delete.call_count == 3


def test_default_qdrant_path_resolves_under_data():
    store = MemoryStore()
    # Resolves to <project>/data/memory/qdrant
    assert store._qdrant_path.name == "qdrant"
    assert store._qdrant_path.parent.name == "memory"
    assert store._qdrant_path.parent.parent.name == "data"
