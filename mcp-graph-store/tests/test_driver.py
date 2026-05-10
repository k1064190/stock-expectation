"""Tests for GraphDriver — neo4j module is mocked, no Docker needed."""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

driver_module = import_module("mcp-graph-store.driver")
schemas_module = import_module("mcp-graph-store.schemas")

GraphDriver = driver_module.GraphDriver
GraphUnavailable = driver_module.GraphUnavailable
INIT_STATEMENTS = schemas_module.INIT_STATEMENTS
CANNED_QUERIES = schemas_module.CANNED_QUERIES


def test_init_statements_are_idempotent():
    """Every CREATE must include IF NOT EXISTS so re-running graph init is safe."""
    for stmt in INIT_STATEMENTS:
        assert "IF NOT EXISTS" in stmt, f"non-idempotent statement: {stmt}"


def test_canned_queries_have_expected_shape():
    expected = {
        "similar_stocks_by_theme",
        "theme_winners_recent",
        "stocks_with_negative_news",
    }
    assert expected.issubset(CANNED_QUERIES.keys())
    for name, cypher in CANNED_QUERIES.items():
        assert "MATCH" in cypher.upper(), f"{name} should be a MATCH query"
        assert "$" in cypher, f"{name} should be parameterised"


def test_ensure_ready_raises_when_neo4j_missing(monkeypatch):
    monkeypatch.setenv("NEO4J_PASSWORD", "fake")
    driver = GraphDriver()
    with patch.dict(sys.modules, {"neo4j": None}):
        with pytest.raises(GraphUnavailable, match="uv sync --extra graph"):
            driver.ensure_ready()


def test_ensure_ready_raises_without_password(monkeypatch):
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)

    fake_neo4j = MagicMock(name="fake_neo4j")
    fake_neo4j.GraphDatabase = MagicMock()

    driver = GraphDriver()
    with patch.dict(sys.modules, {"neo4j": fake_neo4j}):
        with pytest.raises(GraphUnavailable, match="NEO4J_PASSWORD"):
            driver.ensure_ready()


def _build_fake_neo4j():
    """Build a fake `neo4j` module with a stub driver/session."""
    fake_session = MagicMock(name="session")
    fake_driver = MagicMock(name="driver_instance")
    fake_driver.session.return_value = fake_session
    fake_driver.verify_connectivity = MagicMock()

    fake_db_cls = MagicMock(name="GraphDatabase")
    fake_db_cls.driver.return_value = fake_driver

    fake_neo4j_module = MagicMock(name="neo4j_module")
    fake_neo4j_module.GraphDatabase = fake_db_cls
    return fake_neo4j_module, fake_driver, fake_session


def test_ensure_ready_verifies_connectivity_eagerly(monkeypatch):
    """A bad password / down container surfaces at ensure_ready, not at first query."""
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")

    fake_neo4j, fake_driver, _ = _build_fake_neo4j()
    fake_driver.verify_connectivity.side_effect = RuntimeError("auth failed")

    driver = GraphDriver()
    with patch.dict(sys.modules, {"neo4j": fake_neo4j}):
        with pytest.raises(GraphUnavailable, match="docker compose up"):
            driver.ensure_ready()


class _FakeRecord(dict):
    """Stand-in for neo4j.Record — supports `dict(record)` directly."""


def test_run_returns_records_as_dicts(monkeypatch):
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    fake_neo4j, fake_driver, fake_session = _build_fake_neo4j()

    fake_session.__enter__ = MagicMock(return_value=fake_session)
    fake_session.__exit__ = MagicMock(return_value=False)
    fake_session.run.return_value = [
        _FakeRecord(ticker="NVDA", score=0.9),
        _FakeRecord(ticker="AMD", score=0.8),
    ]

    driver = GraphDriver()
    with patch.dict(sys.modules, {"neo4j": fake_neo4j}):
        result = driver.run("MATCH (s:Stock) RETURN s.ticker AS ticker", limit=5)

    assert len(result) == 2
    assert result[0] == {"ticker": "NVDA", "score": 0.9}
    assert result[1] == {"ticker": "AMD", "score": 0.8}
    fake_session.run.assert_called_once()
    args, kwargs = fake_session.run.call_args
    assert "MATCH" in args[0]
    assert kwargs == {"limit": 5}


def test_run_many_continues_on_individual_failures(monkeypatch, caplog):
    """init batches must not abort if one statement is already applied."""
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    fake_neo4j, _, fake_session = _build_fake_neo4j()
    fake_session.__enter__ = MagicMock(return_value=fake_session)
    fake_session.__exit__ = MagicMock(return_value=False)

    # First call ok, second fails, third ok.
    fake_session.run.side_effect = [None, RuntimeError("constraint exists"), None]

    driver = GraphDriver()
    with patch.dict(sys.modules, {"neo4j": fake_neo4j}):
        ok = driver.run_many(["A;", "B;", "C;"])

    assert ok == 2  # third still ran despite middle failing
    # Failure was logged.
    assert any("constraint exists" in m for m in caplog.text.splitlines())


def test_close_is_idempotent(monkeypatch):
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    fake_neo4j, fake_driver, _ = _build_fake_neo4j()

    driver = GraphDriver()
    with patch.dict(sys.modules, {"neo4j": fake_neo4j}):
        driver.ensure_ready()
        driver.close()
        # Second close is a no-op, doesn't raise.
        driver.close()
    fake_driver.close.assert_called_once()
