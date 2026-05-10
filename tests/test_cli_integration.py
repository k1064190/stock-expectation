"""End-to-end CLI integration tests.

Invokes ``bin/stock-cli`` (or ``uv run stock-cli``) as a subprocess and
asserts on the JSON output shape. These prove that argparse wiring, the
sys.path insertions for the four ``mcp-*`` modules, and the dotenv
auto-load all hang together — not just the library code in isolation.

Network-touching tests are marked ``network`` so the default
``pytest -m "not network"`` run stays offline.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_cli(
    *args: str, env: dict[str, str] | None = None, timeout: int = 60
) -> tuple[int, dict | list | str]:
    """Invoke `uv run stock-cli ...` and return (returncode, parsed_json | raw_stdout).

    We deliberately go through `uv run` rather than calling the Python module
    directly, so the wrapper script + venv resolution + dotenv auto-load
    path is exercised exactly as a user would.
    """
    cmd = ["uv", "run", "stock-cli", *args]
    proc_env = os.environ.copy()
    if env is not None:
        proc_env.update(env)
    result = subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        env=proc_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    stdout = result.stdout.strip()
    try:
        payload = json.loads(stdout) if stdout else ""
    except json.JSONDecodeError:
        payload = stdout
    return result.returncode, payload


# ---------------------------------------------------------------------------
# Help / parser wiring — the cheapest sanity tests
# ---------------------------------------------------------------------------


def test_bin_wrapper_help_succeeds():
    """The shipped bash wrapper at ``bin/stock-cli`` must forward to the
    Python CLI — exercising the wrapper path, executable bit, and
    ``--project`` resolution that ``uv run stock-cli`` would skip.
    """
    wrapper = PROJECT_ROOT / "bin" / "stock-cli"
    assert wrapper.exists(), "bin/stock-cli wrapper missing"
    assert os.access(wrapper, os.X_OK), "bin/stock-cli is not executable"
    proc = subprocess.run(
        [str(wrapper), "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"bin/stock-cli --help failed: {proc.stderr}"
    assert "stock-cli" in proc.stdout.lower() or "subcommand" in proc.stdout.lower()


def test_top_level_help_lists_all_subcommands():
    """Every redesign subcommand must be discoverable via --help."""
    proc = subprocess.run(
        ["uv", "run", "stock-cli", "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    out = proc.stdout
    for cmd in (
        "price",
        "price-batch",
        "horizon-metrics",
        "horizon-metrics-batch",
        "news",
        "disclosure",
        "memory",
        "graph",
        "predict",
        "track-record",
        "calibration",
        "portfolio",
    ):
        assert cmd in out, f"--help missing subcommand: {cmd}"


@pytest.mark.parametrize(
    "subcommand",
    ["news", "disclosure", "horizon-metrics-batch", "memory", "graph"],
)
def test_each_subcommand_help_returns_zero(subcommand):
    """Each new subcommand's --help must succeed and mention the command name."""
    proc = subprocess.run(
        ["uv", "run", "stock-cli", subcommand, "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"{subcommand} --help failed: {proc.stderr}"
    assert subcommand in proc.stdout


@pytest.mark.parametrize(
    "argv",
    [
        ("memory", "search", "--help"),
        ("memory", "add", "--help"),
        ("memory", "stats", "--help"),
        ("memory", "purge", "--help"),
        ("graph", "init", "--help"),
        ("graph", "query", "--help"),
        ("graph", "similar-stocks", "--help"),
        ("graph", "theme-winners", "--help"),
    ],
)
def test_each_nested_subcommand_help_succeeds(argv):
    """Pin every nested subcommand's argparse wiring at the help level.

    Catches regressions where a parser is registered but its handler is
    typo'd, or where a flag rename breaks --help string-formatting.
    """
    proc = subprocess.run(
        ["uv", "run", "stock-cli", *argv],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"`stock-cli {' '.join(argv)}` failed: {proc.stderr}"
    # The leaf subcommand name must appear in the usage line.
    assert argv[-2] in proc.stdout


def test_unknown_subcommand_returns_non_zero():
    proc = subprocess.run(
        ["uv", "run", "stock-cli", "definitely-not-a-real-subcommand"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0


# ---------------------------------------------------------------------------
# Memory + Graph — both layers should fail gracefully when their extras
# aren't installed (rather than crash with a stack trace).
# ---------------------------------------------------------------------------


def test_memory_stats_returns_install_hint_when_extra_missing():
    """`stock-cli memory stats` without `--extra memory` must emit a JSON
    error with the install command, not a Python traceback.
    """
    rc, payload = run_cli("memory", "stats")
    assert isinstance(payload, dict), f"expected JSON object, got: {payload!r}"
    if rc != 0:
        assert "error" in payload
        assert "uv sync --extra memory" in payload["error"]
    else:
        # If mem0 happens to be installed, stats should still succeed and
        # return a counts dict — accept that path too.
        assert "counts" in payload


def test_memory_search_rejects_unknown_category():
    """Argparse-time validation must catch a bad --category before any I/O."""
    rc, payload = run_cli(
        "memory", "search", "any-query", "--category", "not-a-real-category"
    )
    assert rc == 1
    assert isinstance(payload, dict)
    assert "error" in payload
    assert "unknown category" in payload["error"].lower()


def test_memory_purge_requires_yes_flag():
    """Purge without --yes must refuse rather than silently delete."""
    rc, payload = run_cli("memory", "purge", "--category", "predictions")
    assert rc == 1
    assert isinstance(payload, dict)
    assert "purge is destructive" in payload.get(
        "error", ""
    ).lower() or "purge is destructive" in str(payload.get("error", ""))


@pytest.mark.parametrize(
    "bad_json",
    [
        "{not: valid json}",  # malformed
        "[]",  # well-formed but wrong shape (array)
        "null",  # well-formed but not an object
    ],
)
def test_memory_add_rejects_invalid_metadata_json(bad_json):
    """`--metadata-json` must be a JSON object. Malformed text and
    well-formed-but-wrong-shape inputs both fail — feeding non-dict to
    the store would surface as a confusing downstream TypeError.
    """
    rc, payload = run_cli(
        "memory",
        "add",
        "--category",
        "predictions",
        "--content",
        "irrelevant",
        "--metadata-json",
        bad_json,
    )
    assert rc == 1, f"expected non-zero exit for {bad_json!r}"
    assert isinstance(payload, dict)
    assert payload.get("error"), f"expected an error message, got: {payload!r}"


@pytest.mark.parametrize(
    "bad_json",
    ["{not: valid json}", "[]", "null"],
)
def test_graph_query_rejects_invalid_params_json(bad_json):
    """Same JSON-object contract for graph query."""
    rc, payload = run_cli("graph", "query", "RETURN 1 AS x", "--params-json", bad_json)
    assert rc == 1, f"expected non-zero exit for {bad_json!r}"
    assert isinstance(payload, dict)
    assert payload.get("error"), f"expected an error message, got: {payload!r}"


@pytest.mark.parametrize("limit", ["-1", "0"])
def test_news_rejects_non_positive_limit(limit):
    """``--limit`` must reject zero/negative values. Without this gate the
    provider's ``items[:limit]`` slice silently returns wrong results
    (Python's negative slicing yields counter-intuitive output).
    """
    rc, payload = run_cli("news", "AAPL", "--market", "US", "--limit", limit)
    assert rc != 0, f"--limit {limit} should be rejected"
    if isinstance(payload, dict):
        assert payload.get("error")


def test_graph_init_returns_install_hint_when_extra_missing():
    """`stock-cli graph init` without `--extra graph` must emit a JSON error."""
    rc, payload = run_cli("graph", "init")
    assert isinstance(payload, dict)
    if rc != 0:
        assert "error" in payload
        # Either "neo4j driver is not installed" (extra missing) or
        # "Could not connect" (extra installed but Docker not running) is acceptable.
        err = payload["error"]
        assert any(
            phrase in err
            for phrase in (
                "neo4j driver is not installed",
                "uv sync --extra graph",
                "Could not connect to Neo4j",
                "NEO4J_PASSWORD",
            )
        ), f"unexpected error: {err}"


# ---------------------------------------------------------------------------
# Disclosure — KR-only, requires OPEN_DART_API_KEY. With no key the CLI
# should still emit a structured JSON response (count=0), not crash.
# ---------------------------------------------------------------------------


def test_disclosure_without_key_returns_empty_count(monkeypatch):
    """No OPEN_DART_API_KEY => empty list, not a crash."""
    rc, payload = run_cli(
        "disclosure",
        "005930",
        "--since-days",
        "1",
        env={"OPEN_DART_API_KEY": ""},
    )
    assert rc == 0
    assert isinstance(payload, dict)
    assert payload["market"] == "KR"
    assert payload["count"] == 0
    assert payload["items"] == []
    # Staleness pattern: every JSON output must include generated_at.
    assert "generated_at" in payload


# ---------------------------------------------------------------------------
# News — yfinance fallback works without any API keys.
# ---------------------------------------------------------------------------


@pytest.mark.network
def test_news_us_fallback_to_yfinance_without_keys():
    """With every paid key blanked, US news must still return >=1 yfinance item.

    yfinance is rate-limit-flaky; we distinguish "yfinance returned nothing
    because of throttling" (legitimate skip) from "the fallback chain is
    broken" (failure) by requiring a non-empty list with the contract
    fields, but skipping rather than failing on empty.
    """
    rc, payload = run_cli(
        "news",
        "AAPL",
        "--market",
        "US",
        "--limit",
        "3",
        "--since-days",
        "30",
        env={
            "FINNHUB_API_KEY": "",
            "ALPHA_VANTAGE_API_KEY": "",
            "FMP_API_KEY": "",
        },
    )
    assert rc == 0
    assert isinstance(payload, dict)
    assert payload["market"] == "US"
    assert payload["ticker"] == "AAPL"
    assert "generated_at" in payload  # staleness contract
    assert payload["since_days"] == 30
    assert payload["count"] == len(payload["items"])  # count/items consistency
    if not payload["items"]:
        pytest.skip("yfinance returned no items (likely rate-limited)")
    assert len(payload["items"]) <= 3, "limit=3 not honoured"
    for entry in payload["items"]:
        for field in ("headline", "source", "date", "url"):
            assert field in entry, f"item missing field: {field}"
        # No keys means no AV merge happened.
        assert entry.get("sentiment_score") is None


@pytest.mark.network
def test_news_kr_naver_scrape_returns_items():
    """KR Naver scrape requires no keys; the listing must populate with
    Korean text round-tripping through stdout JSON intact (`ensure_ascii=False`).
    """
    rc, payload = run_cli(
        "news", "005930", "--market", "KR", "--limit", "3", "--since-days", "7"
    )
    assert rc == 0
    assert payload["market"] == "KR"
    assert payload["ticker"] == "005930"
    assert "generated_at" in payload
    assert payload["count"] == len(payload["items"])
    if not payload["items"]:
        pytest.skip("Naver returned no items — possible layout change")
    assert len(payload["items"]) <= 3
    first = payload["items"][0]
    assert first["url"].startswith("http")
    # Korean source name must round-trip without ascii-escaping. We probe
    # for at least one Hangul code point or a space-separated CJK name.
    any_hangul = any("가" <= ch <= "힣" for ch in (first["source"] + first["headline"]))
    assert any_hangul, (
        "expected Korean text in source/headline but got pure ASCII — "
        "ensure_ascii=False may have regressed in _print_json"
    )


# ---------------------------------------------------------------------------
# Cross-module: horizon-metrics-batch produces the schema /expect depends on.
# ---------------------------------------------------------------------------


@pytest.mark.network
def test_horizon_metrics_batch_returns_expected_fields():
    """The /expect skill's algorithmic point table reads specific field names.
    If horizon-metrics-batch ever drops or renames a field, /expect breaks
    silently. This test pins the contract.
    """
    rc, payload = run_cli(
        "horizon-metrics-batch",
        "AAPL,MSFT",
        "--market",
        "US",
        "--days",
        "400",
    )
    assert rc == 0, f"horizon-metrics-batch failed: {payload!r}"
    assert payload["market"] == "US"
    assert payload["tickers_requested"] == 2
    expected_fields = {
        "ma20",
        "ma50",
        "ma200",
        "rsi14",
        "return_1w",
        "return_1m",
        "return_6m",
        "return_1y",
        "pct_from_52w_high",
        "pct_from_52w_low",
        "max_drawdown_1y",
        "cycle_risk_flag",
    }
    # If any of the pinned tickers errored, the contract is not actually
    # exercised — fail the test rather than silently pass.
    errored = [
        t for t in ("AAPL", "MSFT") if "error" in (payload["results"].get(t) or {})
    ]
    assert not errored, (
        f"horizon-metrics-batch failed for {errored} — contract not verified. "
        f"Payload: {payload['results']}"
    )
    for ticker in ("AAPL", "MSFT"):
        result = payload["results"][ticker]
        present = set(result.keys())
        missing = expected_fields - present
        assert not missing, f"{ticker} missing fields: {missing}"
