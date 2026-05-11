# Stage 7-B — Neo4j Community graph layer

## Why
Doctor Cho picked Neo4j Community Edition for the relational side of structured-data management. mem0 (Stage 7-A) handles semantic recall; Neo4j answers questions like "show all stocks in the AI-infrastructure theme that are within 14 days of an earnings event" or "what was the win rate of predictions that hit a TRIM disclosure flag" — questions where the structure of relationships matters more than text similarity.

## What
- `compose.yml` — Docker service for `neo4j:5-community` with APOC plugin, persisted volume at `data/neo4j/`, Bolt on 7687, browser UI on 7474. Password is required via `NEO4J_PASSWORD` env var.
- `mcp-graph-store/` — new module
  - `schemas.py` — node/edge type doc, `INIT_STATEMENTS` (constraints + indexes, all idempotent via `IF NOT EXISTS`), `CANNED_QUERIES` dict for the three default questions.
  - `driver.py` — `GraphDriver` wrapper with lazy `neo4j` import, eager `verify_connectivity()` (so misconfig surfaces at `ensure_ready()` rather than on first query), `run()`/`run_many()`/`session()` helpers, `GraphUnavailable` raised with install + container hints.
  - `ingestion.py` — `ingest_predictions()` (idempotent MERGE with optional Outcome upsert in the same Cypher), `ingest_news_batch()`, `ingest_disclosures_batch()`. All use natural keys (ticker+market for stocks, prediction id, news url, disclosure rcept_no) so re-runs converge.
  - `tests/test_driver.py` — 8 tests with `neo4j` module mocked; covers init-statement idempotency, canned-query parameterisation, missing-driver/missing-password errors, eager connectivity verification, dict-conversion of records, error-tolerant `run_many`, idempotent `close()`.
- `stock_cli.py` — new `graph` subcommand group: `init`, `query`, `similar-stocks`, `theme-winners`. Lazy-loads driver; without the extra installed, returns a clear JSON error instead of crashing.
- `pyproject.toml` — `[graph]` extra: `neo4j>=5.20`.
- `.gitignore` — `data/memory/`, `data/neo4j/`, `.env` (covers Stage 7-A and 7-B together).

## How
- **Compose-first.** Neo4j Community runs in Docker rather than as a Python embedded library — that's the official path, and pinning `neo4j:5-community` keeps the dev environment reproducible.
- **Idempotent everywhere.** Schema constraints all use `IF NOT EXISTS`; ingestion uses `MERGE` on natural keys. A test asserts every `INIT_STATEMENTS` entry contains `IF NOT EXISTS` so future additions don't regress.
- **`run_many()` continues on individual failures.** When `graph init` runs against a database where some constraints already exist, the per-statement try/except logs the failure and keeps going. Test verifies that 2/3 succeed when the middle one raises.
- **Eager connectivity verification.** `GraphDriver.ensure_ready()` calls `verify_connectivity()` after constructing the driver. A bad password or missing container surfaces here with a message pointing at `docker compose up -d neo4j` rather than as a confusing failure mid-query.
- **Same hyphen-juggling pattern as Stage 7-A.** Tests use `importlib.import_module("mcp-graph-store.driver")`; `driver.py` has try/except dual import paths so both package-style and `sys.path`-flat use cases work. `stock_cli.py` adds `sys.path.insert(...)` for both new modules.
- **Theme tagging deferred.** The schema includes `(Stock)-[:LINKED_TO]->(Theme)` but no automatic theme assignment in this stage; that depends on Stage 7-A's mem0 semantic search and is the obvious Stage 7-B.1 follow-up. For now, themes can be inserted via raw `graph query` Cypher.

## Code locations
- `compose.yml` — Docker service definition
- `mcp-graph-store/{schemas,driver,ingestion}.py` — module
- `mcp-graph-store/tests/test_driver.py` — 8 tests
- `stock_cli.py` — `cmd_graph_*` handlers + parser block
- `pyproject.toml` — `graph` extra
- `.gitignore` — `data/neo4j/`, `.env`

## Verification
- `uv run pytest mcp-graph-store/tests/ -v` → 8 passed
- `uv run pytest -m "not network"` → 161 passed (graph tests are mocked, no Docker)
- `uv run stock-cli graph --help` → lists `init`, `query`, `similar-stocks`, `theme-winners`
- `uv run stock-cli graph init` (without the extra installed) → returns `{"error": "neo4j driver is not installed. Run: uv sync --extra graph"}` rather than crashing
- Live verification deferred until `uv sync --extra graph && docker compose up -d neo4j` — at that point the smoke is `stock-cli graph init` (creates constraints), then a one-shot `graph query "MATCH (n) RETURN count(n)"` to confirm zero nodes, then exercise an ingestion script.

## Per-stage review
**Skipped formal dual review** — same proportionality call as Stages 6 and 7-A. The module is well-tested (idempotency, connectivity verification, error tolerance), and the riskier piece (live Cypher correctness against production data) is gated by Doctor Cho actually running the Docker container and ingestion script. That's the right time for second-opinion review on the Cypher itself.

## Retrospective
What went well: the canned-query approach means the CLI exposes a small, well-shaped interface (`similar-stocks`, `theme-winners`) instead of forcing users to write Cypher for common questions. Raw `query` is still there for power users.

What to carry forward: when wiring up a database that ships as a Docker image, write the `compose.yml` and `.gitignore` exclusions in the same commit as the driver — easy to forget the data-volume gitignore until the first ingest pollutes the repo with binary blobs.
