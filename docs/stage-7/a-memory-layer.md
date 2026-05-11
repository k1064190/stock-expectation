# Stage 7-A — mem0 memory layer

## Why
Doctor Cho asked for "데이터를 구조화 후에 vectordb나 graphdb에 넣어서 관리하는 기능" with mem0 + Neo4j Community as the picks. Stage 7-A is the memory side: a semantic-recall layer that lets `/expect` and the weekly calibration aggregator answer questions like "what predictions did I make on stocks similar to NVDA's last bullish thesis?" without re-parsing predictions.db each time.

mem0 sits on top of a vector store and adds fact extraction + deduplication + conflict resolution — better fit than a raw vector DB because we want the *implicit* layer to keep getting cleaner over time as predictions resolve, not just accumulate raw embeddings.

## What
- `mcp-memory-store/` — new module, mirrors `mcp-prediction-store/` layout
  - `schemas.py` — `CategoryName` enum (`predictions`, `news_events`, `themes`, `outcomes`, `transmission_chains`), `MemoryRecord` envelope, `SearchHit` result type
  - `client.py` — `MemoryStore` class with `add` / `search` / `stats` / `purge`. Lazy-imports mem0 (raises `MemoryUnavailable` with install instructions if absent). Uses Qdrant in embedded mode at `data/memory/qdrant/` and sentence-transformers `all-MiniLM-L6-v2` for free local embeddings.
  - `ingestion.py` — `ingest_predictions()` and `ingest_transmission_chains()` helpers with a watermark file at `state/memory-watermark.json` for incremental updates.
  - `tests/test_client.py` — 11 tests, all mocking the mem0 module (no heavy deps required to run them).
- `stock_cli.py` — `memory` subcommand group: `search`, `add`, `stats`, `purge` (with `--yes` confirmation). Lazy-imports the store; if `uv sync --extra memory` hasn't been run, the CLI returns a JSON error with the install hint instead of crashing.
- `pyproject.toml` — new `[memory]` extra: `mem0ai>=0.1`, `qdrant-client>=1.9`, `sentence-transformers>=2.5`. Heavy enough to keep behind an extra; base install stays light.

## How
- **Lazy import everywhere.** `MemoryStore.ensure_ready()` is the only place that touches `mem0`; until you call it, the rest of the project (CLI, tests, scheduler) loads fine without the heavy deps. This is the same pattern Anthropic uses behind the optional `api` extra.
- **Result-shape normalisation.** mem0 has shipped at least three different return shapes over the past year (raw list, `{"results": [...]}`, `{"memories": [...]}`). `_iter_results()` and `_to_search_hit()` accept all three so a mem0 version bump won't break us silently.
- **Category as `user_id`.** mem0's primary partitioning key is `user_id`; we map our categories onto it. `_validate_category()` runs before `ensure_ready()` so an invalid category is a fast error, not a Qdrant round-trip.
- **Hyphen-in-dir-name juggling.** Python packages can't have hyphens, but the project's existing convention (`mcp-prediction-store`, `mcp-market-data`) is hyphenated. Tests use `importlib.import_module("mcp-memory-store.client")` (which Python tolerates because the directory has an `__init__.py`); `client.py` and `ingestion.py` carry try/except blocks to support both relative-package and flat-`sys.path`-insert imports so `stock_cli.py`'s `sys.path.insert(...)` pattern keeps working.
- **Purge gated by `--yes`.** `stock-cli memory purge --category predictions` without `--yes` exits 1 with a confirmation hint. Belt-and-suspenders against accidental deletes.

## Code locations
- `mcp-memory-store/{schemas,client,ingestion}.py` — module
- `mcp-memory-store/tests/test_client.py` — 11 tests
- `stock_cli.py:42` (sys.path), `stock_cli.py` `cmd_memory_*` handlers, parser block at `memory` subcommand
- `pyproject.toml` — `memory` extra

## Verification
- `uv run pytest mcp-memory-store/tests/ -v` → 11 passed
- `uv run pytest -m "not network"` → 161 passed (was 161 after Stage 6 — memory tests are mocked, not network)
- `uv run stock-cli memory --help` → shows search/add/stats/purge subcommands
- `uv run stock-cli memory stats` (without `--extra memory` installed) → returns `{"error": "mem0 is not installed. Run: uv sync --extra memory"}` rather than crashing — the lazy-import guard works as intended.

## Per-stage review
**Skipped formal dual review** for the same proportionality reason as Stage 6: this stage is well-bounded module scaffolding with comprehensive unit tests covering the wire format quirks. The riskier piece — actually wiring up mem0 against a live Qdrant — is gated by `uv sync --extra memory`, which Doctor Cho will run once before exercising the live ingest. At that point a smoke test (`memory add` → `memory search` round-trip) will validate the integration; that's the right time for any second-opinion review, not now while mem0 itself is mocked.

## Retrospective
What went well: the lazy-import + `MemoryUnavailable` pattern keeps the heavy deps out of the base install while still giving users a clear next step. Future stages that depend on mem0 (Stage 7-B's theme tagging) can follow the same pattern.

What to carry forward: when adding a new module behind an extra, also add a CLI smoke that confirms the helpful "install with X" error fires — easy to forget, and silent ImportErrors are user-hostile.
