"""Daily briefing generator.

Three modes of operation:
  --mode codex-cli (default): Invokes `codex exec` CLI. Uses ChatGPT Plus
      credit, no API key needed. Codex reads .claude/skills/ markdown
      files as context and runs `bin/stock-cli` via Bash to fetch data
      and log predictions. Switched to default on 2026-05-18 after the
      Anthropic headless subscription quota started silently throttling
      claude-code cron runs (see docs/stage-9/codex-cli-mode.md).
  --mode claude-code: Invokes `claude -p` CLI. Uses Claude Code
      subscription, no API key needed. Same prompt path as codex-cli.
      Subject to Anthropic headless-subscription throttling as of
      2026-05; kept as a fallback for when codex-cli has its own issues.
  --mode api: Calls Anthropic API directly. Requires ANTHROPIC_API_KEY.
      Data is pre-fetched by this script and injected into the prompt.
      Predictions are returned as JSON, parsed, and logged by this script.

Run twice daily:
    07:00 KST — Korean market briefing (before KR open)
    21:00 KST — US market briefing (before US pre-market)

Usage:
    uv run python scheduler/daily_briefing.py --market KR
    uv run python scheduler/daily_briefing.py --market US --mode codex-cli
    uv run python scheduler/daily_briefing.py --market US --mode api
    uv run python scheduler/daily_briefing.py --market ALL
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project paths
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp-prediction-store"))
sys.path.insert(0, str(PROJECT_ROOT / "mcp-market-data"))

# Auto-load .env for ANTHROPIC_API_KEY / FMP_API_KEY / Telegram credentials.
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass

from models import (
    Prediction,
    get_connection,
    insert_prediction,
    validate_prediction_dict,
)
from metrics import (
    get_track_record,
    get_calibration_report,
    get_signal_performance,
    recalibrate_confidence,
)
from providers.us import USMarketProvider
from providers.kr import KoreanMarketProvider
from indicators import compute_horizon_metrics
from telegram_sender import send_briefing
from blended_funnel import (
    assemble_blended_candidates,
    format_blended_for_prompt,
)
from theme_clusterer import (
    backfill_news_counts,
    cluster_news,
    fetch_news_for_candidates,
    format_themes_for_prompt,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("daily_briefing")

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Token budget: cap feedback loop at ~10% of total prompt
MAX_TRACK_RECORD_CHARS = 800


# ---------------------------------------------------------------------------
# Data fetching (used by both modes for pre-fetch, and by API mode for prompt)
# ---------------------------------------------------------------------------


def fetch_us_market_data() -> str:
    """Fetch US market data via the dynamic candidate scanner + news themes.

    Parallel to ``fetch_kr_market_data`` — uses the static
    ``data/us_universe.csv`` (~135 S&P 500 + ETF + ADR names) as the
    enumeration source, filters by 5-day momentum / volume, merges
    the 3 broad-market ETF anchors (SPY/QQQ/DIA), then runs the same
    n-gram theme clusterer over the news fetched for each survivor.

    Returns:
        Formatted market data string for prompt injection (API mode).
        Falls back to anchors-only if the provider can't fetch
        anything — never raises.
    """
    us = USMarketProvider()
    picks, anchors = assemble_blended_candidates("US", provider=us)
    cands = picks + anchors
    # Guard the second batch fetch (yfinance can raise on transient
    # network/auth failures). The function "never raises" per docstring,
    # so swallow into an empty dict — candidates and themes still flow
    # to the prompt, just without the per-ticker $price snapshot block
    # (Codex Stage 5 P2 finding).
    try:
        bars_by_ticker = us.get_price_history_batch([c.ticker for c in cands], days=10)
    except Exception as exc:  # noqa: BLE001 — never block on data provider
        logger.warning(
            "fetch_us_market_data: 10-day batch fetch failed (%s); "
            "continuing with candidate/theme context only",
            exc,
        )
        bars_by_ticker = {}

    news_by_ticker = fetch_news_for_candidates(cands, days=7, provider=us)
    backfill_news_counts(cands, news_by_ticker)
    themes = cluster_news(news_by_ticker, min_cluster_size=3)

    lines = [
        "## US Market Data\n",
        format_blended_for_prompt(picks, anchors, "US"),
        "",
        format_themes_for_prompt(themes),
        "",
    ]

    for c in cands:
        bars = bars_by_ticker.get(c.ticker, [])
        if not bars:
            continue
        latest = bars[-1]
        prev = bars[-2] if len(bars) > 1 else bars[0]
        change_pct = (
            (latest.close - prev.close) / prev.close * 100 if prev.close else 0.0
        )
        name = c.name or c.ticker
        lines.append(
            f"**{c.ticker} ({name}) [{c.reason}]**: "
            f"${latest.close:,.2f} ({change_pct:+.1f}%) | "
            f"Vol: {latest.volume:,} | "
            f"news7d={c.news_count_7d}"
        )

    return "\n".join(lines)


def fetch_kr_market_data() -> str:
    """Fetch Korean market data via the dynamic candidate scanner + news themes.

    Stage A: ``discover_kr_candidates`` (시총 top-200 ∪ 거래대금 top-50 →
    momentum/volume filter → 3 anchor merge).
    Stage B: ``fetch_news_for_candidates`` (8-worker parallel Naver scrape)
    + ``cluster_news`` (2/3-gram cross-ticker clustering, default
    ``ngram_sizes=(2, 3)``) for the
    Active Themes section.

    Returns:
        Formatted market data string for prompt injection (API mode).
        Falls back to anchors-only if PyKRX fails — never raises.
    """
    kr = KoreanMarketProvider()
    picks, anchors = assemble_blended_candidates("KR", provider=kr)
    cands = picks + anchors
    bars_by_ticker = kr.get_price_history_batch([c.ticker for c in cands], days=10)

    news_by_ticker = fetch_news_for_candidates(cands, days=7, provider=kr)
    backfill_news_counts(cands, news_by_ticker)
    themes = cluster_news(news_by_ticker, min_cluster_size=3)

    lines = [
        "## Korean Market Data\n",
        format_blended_for_prompt(picks, anchors, "KR"),
        "",
        format_themes_for_prompt(themes),
        "",
    ]

    for c in cands:
        bars = bars_by_ticker.get(c.ticker, [])
        if not bars:
            continue
        latest = bars[-1]
        prev = bars[-2] if len(bars) > 1 else bars[0]
        change_pct = (
            (latest.close - prev.close) / prev.close * 100 if prev.close else 0.0
        )
        name = c.name or c.ticker
        lines.append(
            f"**{c.ticker} ({name}) [{c.reason}]**: "
            f"₩{latest.close:,.0f} ({change_pct:+.1f}%) | "
            f"Vol: {latest.volume:,} | "
            f"news7d={c.news_count_7d}"
        )

    return "\n".join(lines)


def get_track_record_context() -> str:
    """Build track record context for prompt injection.

    Capped at MAX_TRACK_RECORD_CHARS to stay within 10% prompt budget.

    Returns:
        Formatted track record string.
    """
    conn = get_connection()
    try:
        record = get_track_record(conn, days=30)
        calibration = get_calibration_report(conn)
        signals = get_signal_performance(conn, min_count=5)

        if record.total == 0:
            return "No predictions closed yet. This is the first run."

        lines = [
            (
                f"Win rate: {record.win_rate:.0%} ({record.wins}W/{record.losses}L)"
                if record.win_rate is not None
                else "Win rate: N/A"
            ),
            (
                f"Avg return: {record.avg_return:+.1f}%"
                if record.avg_return is not None
                else ""
            ),
            f"Current streak: {record.current_streak:+d}",
            (
                f"Brier score: {record.brier_score:.3f}"
                if record.brier_score is not None
                else ""
            ),
        ]

        if calibration:
            lines.append("\nCalibration:")
            for b in calibration:
                status = "OK"
                if b.actual_accuracy > b.predicted_confidence + 0.05:
                    status = "UNDERCONFIDENT"
                elif b.actual_accuracy < b.predicted_confidence - 0.05:
                    status = "OVERCONFIDENT"
                lines.append(
                    f"  {b.confidence_range}: predicted {b.predicted_confidence:.0%}, "
                    f"actual {b.actual_accuracy:.0%} ({b.count} preds) [{status}]"
                )

        if signals:
            lines.append("\nSignal performance:")
            for s in signals[:5]:
                lines.append(
                    f"  {s.signal}: {s.win_rate:.0%} win rate ({s.total} preds)"
                )

        result = "\n".join(lines)
        return result[:MAX_TRACK_RECORD_CHARS]
    finally:
        conn.close()


def get_portfolio_context(market: str) -> str:
    """Pre-fetch portfolio state for prompt injection.

    Why this lives in Python (not in SKILL.md instructions): a previous
    KR briefing emitted "보유 종목 없음 — 본 task 범위 밖이라 미조회"
    even though 5 KR + 11 US positions existed in the local DB. The
    claude -p run treated portfolio fetch as optional and skipped it.
    Putting the data directly in the prompt makes it impossible to ignore.

    Steps:
      1. Attempt ``portfolio sync`` (idempotent; failures are swallowed so a
         missing/expired tossctl never blocks the briefing).
      2. Fetch ``portfolio positions`` for the target market and format
         each holding into a line the LLM can quote directly.

    Args:
        market: "US" or "KR".

    Returns:
        Multi-line text block. Header line says whether positions exist;
        body lists each position (ticker, qty, avg cost). On any error,
        returns a single-line "Portfolio unavailable" note so the prompt
        is always well-formed.
    """
    import json as _json
    import subprocess as _sp

    project_root = Path(__file__).parent.parent
    cli = project_root / "bin" / "stock-cli"

    # Step 1 — Toss sync (best-effort, never raises).
    try:
        _sp.run(
            [str(cli), "portfolio", "sync"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:  # noqa: BLE001 — sync failure must never block briefing
        pass

    # Step 2 — Fetch positions.
    try:
        out = _sp.run(
            [str(cli), "portfolio", "positions", "--market", market],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return f"[{market}] 포트폴리오 데이터 조회 실패 — 점검 섹션 생략 가능."
        data = _json.loads(out.stdout)
        positions = data.get("positions") or []
    except Exception as e:  # noqa: BLE001
        return f"[{market}] 포트폴리오 데이터 파싱 실패: {e} — 점검 섹션 생략 가능."

    if not positions:
        return (
            f"[{market}] 보유 종목 없음. "
            "출력 시 '보유 종목 없음' 한 줄로 안내하고 다음 섹션으로 진행."
        )

    lines = [f"[{market}] 현재 보유 {len(positions)}종목 (Toss 동기화 직후):"]
    for p in positions:
        ticker = p.get("ticker", "?")
        # The positions API uses 'quantity' + 'avg_price'; older snapshots
        # used 'qty' + 'avg_cost'. Support both, but never fail on absence.
        qty = p.get("quantity", p.get("qty", "?"))
        avg = p.get("avg_price", p.get("avg_cost", p.get("average_cost", "?")))
        total_cost = p.get("total_cost")
        realized = p.get("realized_pnl")

        # Format the cost basis as an integer for KR (no decimals on KRW)
        # and 2-dec for US. ``positions`` doesn't return current_price, so
        # the LLM still has to fetch live prices via `bin/stock-cli price`
        # before computing unrealized P&L — that's an instruction in the
        # SKILL.md, not something we pre-compute here.
        try:
            if market == "KR":
                avg_str = f"{float(avg):,.0f}원"
            else:
                avg_str = f"${float(avg):,.2f}"
        except (TypeError, ValueError):
            avg_str = str(avg)

        realized_part = ""
        if isinstance(realized, (int, float)) and realized != 0:
            realized_part = f", 실현손익={realized:+,.0f}"

        lines.append(f"  - {ticker} | qty={qty}, 평단={avg_str}{realized_part}")
    lines.append(
        "위 각 포지션에 대해 (1) `bin/stock-cli price <ticker> --market "
        f"{market} --days 5` 로 현재가 확인, (2) 평단 대비 P&L 산정, "
        "(3) SKILL.md '내 포트폴리오 점검' 룰에 따라 "
        "보유/추가/부분/손절/전량 중 하나 추천. "
        "이 데이터는 prompt에 이미 주입됐으니 '미조회'로 스킵 금지."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude Code CLI mode
# ---------------------------------------------------------------------------


def build_claude_code_prompt(market: str) -> str:
    """Build the briefing prompt for the LLM CLI subprocess.

    Originally named for Claude Code; now also fed to Codex CLI (--mode
    codex-cli) and shared between both routes because the prompt body is
    LLM-agnostic — it references `.claude/skills/` markdown files and
    `bin/stock-cli` shell commands that either CLI can follow. Kept under
    the same name to avoid touching callers.

    In this mode, Claude Code invokes `bin/stock-cli` via Bash to fetch data
    and log predictions. The daily-briefing skill is also auto-loaded from
    `.claude/skills/`, so Claude will follow that workflow.

    Args:
        market: "US" or "KR".

    Returns:
        Prompt string for `claude -p`.
    """
    track_record = get_track_record_context()
    portfolio_context = get_portfolio_context(market)
    today = datetime.now().strftime("%Y-%m-%d")

    if market == "US":
        us_provider = USMarketProvider()
        picks, anchors = assemble_blended_candidates("US", provider=us_provider)
        us_candidates = picks + anchors
        us_news = fetch_news_for_candidates(us_candidates, days=7, provider=us_provider)
        backfill_news_counts(us_candidates, us_news)
        us_themes = cluster_news(us_news, min_cluster_size=3)
        candidate_block = format_blended_for_prompt(picks, anchors, "US")
        themes_block = format_themes_for_prompt(us_themes)
        ticker_csv = ",".join(c.ticker for c in us_candidates) or "SPY,QQQ,DIA"
        return f"""Generate a US market daily briefing for {today}.

Follow the `daily-briefing` skill in `.claude/skills/daily-briefing/SKILL.md`.
All data access goes through `bin/stock-cli` via Bash.

The following candidates were chosen by a Python scanner in two complementary
streams: PRE-SURGE (base/pullback/RS setups that have NOT yet run — review these
FIRST) and MOMENTUM (already-surged 5d names — BUY only if not overextended).
Anchors (SPY/QQQ/DIA) are macro reference, NOT pick slots. Analyze/recommend
ONLY from this list — do not add new tickers on your own judgment.

{candidate_block}

{themes_block}

Specifically:
1. Fetch each candidate's price/volume:
   `bin/stock-cli price-batch {ticker_csv} --market US --days 10`
2. Check existing state: `bin/stock-cli predict list --status OPEN --market US`
3. Check track record: `bin/stock-cli track-record --days 30 --market US`
4. Generate 5-6 predictions (matches the daily-briefing SKILL.md spec
   of 5-6 picks per market / 10-12 total per ALL run), logging each with
   `bin/stock-cli predict create ... --source LIVE`

Your recent track record (for calibration):
{track_record}

Your current portfolio (pre-fetched — DO NOT skip the portfolio review section
if positions exist below):
{portfolio_context}

Rules:
- Minimum confidence 0.60 (matches the per-horizon logging gate below), maximum 0.85
- Every prediction needs at least 2 signals
- Target must be at least 2x stop distance; reward:risk ≥ 1.5 minimum, else do NOT log (WATCH only)
- GATE R1 (regime): run `bin/stock-cli regime --market US`. RISK_OFF → log NO new BULL (cap WATCH); NEUTRAL → raise the BUY bar (composite ≥ 9.0) and trim confidence one step.
- GATE R2 (overextension): from horizon-metrics `overextension_level` — EXTREME → WATCH only, never BULL; ELEVATED → raise the BUY bar + trim confidence.
- PARABOLIC CAP: any name already up >20% over the trailing month (`return_1m` > 0.20) is WATCH only, never a new BULL.
- COMPONENTS (mandatory): every `predict create` must pass `--components` JSON including the pillar scores AND `"overextension"` (NONE/ELEVATED/EXTREME) AND `"return_1m"` (decimal) AND `"discovery_source"` (presurge/momentum) AND `"setup_type"`. The store HARD-REJECTS a LIVE BULL with overextension EXTREME or return_1m>0.20 — pass these honestly so the gate and cohort tracking work.
- Primary timeframe: 1W (Short). Produce Short/Medium(1M)/Long(6M)/Cycle(1Y) horizons per the expect skill.
- HORIZON by stream: PRE-SURGE picks anchor conviction at 1M+ (base/pullback setups need weeks — backtested ~60% expire dead at 1W vs ~11% at 1M); MOMENTUM picks may anchor at 1W.
- Must actually call `bin/stock-cli predict create` for each horizon ≥ 0.60 confidence per pick
- Use --source LIVE --recalibrate (this is the automated scheduler, not interactive)

After creating predictions, output the full briefing as markdown. Include a
"## Predictions Logged" section listing each prediction ID returned by
the predict create commands.

CRITICAL OUTPUT REQUIREMENT:
Your final assistant message MUST BE THE FULL BRIEFING MARKDOWN ITSELF —
every section (요약, 매크로 환경, 종목 추천, 내 포트폴리오 점검, 트랙
레코드, 주요 이벤트, Predictions Logged) inline as your final response.

DO NOT end with a meta-summary like "Briefing complete", "8 predictions
logged across 3 picks", "Done", or any one-line wrapup. The summary
sentence is NOT the deliverable; the briefing itself is. The scheduler
captures ONLY your final assistant message; anything you put in
intermediate tool-call narrations will not reach the user. Make sure the
last thing you emit is the full markdown briefing, beginning with
"# 📊 Daily Market Briefing — [YYYY-MM-DD]" and continuing through
every section to the Predictions Logged table."""

    else:  # KR
        kr_provider = KoreanMarketProvider()
        picks, anchors = assemble_blended_candidates("KR", provider=kr_provider)
        kr_candidates = picks + anchors
        kr_news = fetch_news_for_candidates(kr_candidates, days=7, provider=kr_provider)
        backfill_news_counts(kr_candidates, kr_news)
        kr_themes = cluster_news(kr_news, min_cluster_size=3)
        candidate_block = format_blended_for_prompt(picks, anchors, "KR")
        themes_block = format_themes_for_prompt(kr_themes)
        ticker_csv = ",".join(c.ticker for c in kr_candidates) or "005930,000660"
        return f"""Generate a Korean market daily briefing for {today}.

Follow the `daily-briefing` and `korean-market-analysis` skills in
`.claude/skills/`. All data access goes through `bin/stock-cli` via Bash.

다음 후보 종목은 Python 스캐너가 2개 보완 스트림으로 결정했다: PRE-SURGE
(아직 안 오른 base/pullback/RS 셋업 — 먼저 검토)와 MOMENTUM (이미 급등한 5일
종목 — 과열 아닐 때만 BUY). 앵커(005930/000660/069500)는 시장 참고용이며 추천
슬롯이 아니다. LLM은 이 목록 안에서만 분석/추천하라 — 새 종목 추가 금지.

{candidate_block}

{themes_block}

Specifically:
1. Fetch each candidate's price/volume:
   `bin/stock-cli price-batch {ticker_csv} --market KR --days 10`
2. Fetch US reference data for cross-market context:
   `bin/stock-cli price SPY --market US --days 10`
   `bin/stock-cli price NVDA --market US --days 10`
   `bin/stock-cli price SMH --market US --days 10`
3. Check existing state: `bin/stock-cli predict list --status OPEN --market KR`
4. Check track record: `bin/stock-cli track-record --days 30 --market KR`
5. Generate 5-6 Korean predictions (matches the daily-briefing SKILL.md
   spec of 5-6 picks per market / 10-12 total per ALL run), logging each
   with `bin/stock-cli predict create ... --source LIVE`

Your recent track record (for calibration):
{track_record}

Your current portfolio (pre-fetched — DO NOT skip the portfolio review section
if positions exist below):
{portfolio_context}

Rules:
- Korean stocks: same 4-horizon analysis; report Short(1W), Medium(1M), Long(6M), Cycle(1Y).
- Minimum confidence 0.60 (matches the per-horizon logging gate below), maximum 0.85
- Stop-loss wider than US by ~20%; target at least 2x stop distance (reward:risk ≥ 1.5, else WATCH only)
- GATE R1 (regime): run `bin/stock-cli regime --market KR`. RISK_OFF → log NO new BULL (cap WATCH); NEUTRAL → raise the BUY bar + trim confidence.
- GATE R2 (overextension): horizon-metrics `overextension_level` EXTREME → WATCH only; ELEVATED → raise the bar + trim.
- PARABOLIC CAP: any name already up >20% over the trailing month (`return_1m` > 0.20) is WATCH only, never a new BULL.
- COMPONENTS (mandatory): every `predict create` passes `--components` JSON with the pillar scores AND `"overextension"` AND `"return_1m"` (decimal) AND `"discovery_source"` AND `"setup_type"`. The store HARD-REJECTS a LIVE BULL with overextension EXTREME or return_1m>0.20 — pass these honestly.
- HORIZON by stream: PRE-SURGE picks anchor conviction at 1M+ (base/pullback setups need weeks — backtested ~60% expire dead at 1W vs ~11% at 1M); MOMENTUM picks may anchor at 1W. (KR default is already 1M.)
- Consider won/dollar impact on exporters
- Cross-market: US 반도체·AI·auto-tech 모멘텀은 KR 반도체·전장·SW 통합사로
  통상 1일 지연 전이된다. 위 'Active Themes' 블록이 비어있지 않다면 거기에
  명시된 테마가 가장 활성화된 narrative — 추천 thesis에 직접 인용 가능.
- Must actually call `bin/stock-cli predict create` for each horizon ≥ 0.60 confidence per pick (same as US workflow)
- Use --source LIVE --recalibrate

After creating predictions, output the full briefing as markdown. Include a
"## Predictions Logged" section listing each prediction ID returned by
the predict create commands.

CRITICAL OUTPUT REQUIREMENT:
Your final assistant message MUST BE THE FULL BRIEFING MARKDOWN ITSELF —
every section (요약, 매크로 환경, 종목 추천, 내 포트폴리오 점검, 트랙
레코드, 주요 이벤트, Predictions Logged) inline as your final response.

DO NOT end with a meta-summary like "Briefing complete", "8 predictions
logged across 3 picks", "Done", or any one-line wrapup. The summary
sentence is NOT the deliverable; the briefing itself is. The scheduler
captures ONLY your final assistant message; anything you put in
intermediate tool-call narrations will not reach the user. Make sure the
last thing you emit is the full markdown briefing, beginning with
"# 📊 Daily Market Briefing — [YYYY-MM-DD]" and continuing through
every section to the Predictions Logged table."""


def call_claude_code(prompt: str) -> str:
    """Invoke `claude -p` CLI in non-interactive mode.

    Claude Code runs inside the project directory where `.claude/skills/`
    is auto-loaded, and uses `bin/stock-cli` via Bash to fetch data and
    log predictions.

    Args:
        prompt: The prompt to send to Claude Code.

    Returns:
        Claude Code's response text.

    Raises:
        RuntimeError: If claude CLI is not found or returns an error.
    """
    claude_path = shutil.which("claude")
    if not claude_path:
        raise RuntimeError(
            "claude CLI not found. Install Claude Code or use --mode api"
        )

    result = subprocess.run(
        [
            claude_path,
            "-p",
            prompt,
            "--output-format",
            "text",
        ],
        capture_output=True,
        text=True,
        # 15 min ceiling. KR briefing typically runs 3-5 min; US can run
        # 5-8 min when the portfolio review section is exercised against
        # 10+ positions (each needs a current-price fetch + P&L calc +
        # recommendation), and the 2026-05-12 21:00 cron firing hit
        # the previous 300s ceiling. 900s keeps headroom while still
        # bounding the worst case.
        timeout=900,
        cwd=str(PROJECT_ROOT),
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI failed (exit {result.returncode}): {result.stderr[:500]}"
        )

    return result.stdout


# ---------------------------------------------------------------------------
# Codex CLI mode (alternative to claude-code)
# ---------------------------------------------------------------------------


def call_codex_cli(prompt: str) -> str:
    """Invoke `codex exec` CLI in non-interactive mode.

    Mirrors call_claude_code but routes through OpenAI's Codex CLI instead of
    Anthropic's Claude Code. Useful when Anthropic's subscription headless
    quota is exhausted or rate-limited (as observed 2026-05-18 cron run).
    Codex reads .claude/skills/ markdown files as plain context — it does
    not "load" them as native skills, but the prompt explicitly references
    them so Codex follows the instructions inline.

    Workarounds applied:
    - ``--disable apps``: codex-cli 0.130.0 ships a broken
      ``_create_map_with_locations`` MCP tool whose schema OpenAI rejects.
      Disabling the apps feature skips it.
    - ``--sandbox workspace-write``: required for codex to execute
      ``bin/stock-cli`` and other bash commands the prompt instructs.
    - ``--full-auto``: auto-approve tool calls without interactive prompts
      (we run under cron, no human to approve).

    Args:
        prompt: The prompt to send to Codex.

    Returns:
        Codex's final assistant message text (stdout, stderr suppressed
        to drop reasoning summaries and MCP startup noise).

    Raises:
        RuntimeError: If codex CLI is not found or returns a non-zero exit.
    """
    codex_path = shutil.which("codex")
    if not codex_path:
        raise RuntimeError(
            "codex CLI not found. Install Codex or use --mode claude-code / --mode api"
        )

    # NOTE(stage-9): `--disable apps` papers over a codex-cli 0.130.0 bundled
    # MCP tool (`_create_map_with_locations`) whose JSON schema OpenAI rejects.
    # When codex-cli 0.131+ ships, smoke-test removing this flag and drop it
    # if the bundled tool schema is fixed upstream. Without this flag every
    # codex exec call fails with HTTP 400.
    #
    # CODEX_MODEL env var override (default gpt-5.5): codex-cli requires a
    # specific model name and gpt-5.5 is the project's preferred quality tier
    # (matches codex-subagent skill default). OpenAI's rollout is gradual and
    # individual accounts may lose/gain access — set CODEX_MODEL=gpt-5.4
    # (or similar) in the environment to override without touching this file.
    codex_model = os.environ.get("CODEX_MODEL", "gpt-5.5")
    result = subprocess.run(
        [
            codex_path,
            "exec",
            "--skip-git-repo-check",
            "--disable",
            "apps",
            "-m",
            codex_model,
            "--config",
            'model_reasoning_effort="high"',
            # Explicit network grant: codex's workspace-write sandbox can be
            # configured (per-host or per-profile) to disable network by
            # default. bin/stock-cli needs outbound HTTPS (yfinance, Naver
            # Finance, Telegram delivery, FMP fallback) so we override here
            # rather than trusting the host default.
            "--config",
            "sandbox_workspace_write.network_access=true",
            "--sandbox",
            "workspace-write",
            "--full-auto",
            "-C",
            str(PROJECT_ROOT),
            prompt,
        ],
        capture_output=True,
        text=True,
        # Same 15 min ceiling as claude-code. Codex generally completes in
        # 3-6 min for KR/US briefings; the headroom catches occasional
        # slow tool-call chains without leaving stuck cron processes.
        timeout=900,
        cwd=str(PROJECT_ROOT),
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"codex CLI failed (exit {result.returncode}): {result.stderr[:500]}"
        )

    return result.stdout


# ---------------------------------------------------------------------------
# Anthropic API mode (fallback)
# ---------------------------------------------------------------------------


def call_claude_api(prompt: str) -> str:
    """Call Anthropic API directly with the briefing prompt.

    Uses Claude Sonnet for cost efficiency. Requires ANTHROPIC_API_KEY.
    Requires the `api` extra: `uv sync --extra api`.

    Args:
        prompt: Full prompt with market data and track record injected.

    Returns:
        Claude's response text.
    """
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError(
            "--mode api requires the anthropic package. "
            "Install with: uv sync --extra api"
        ) from e

    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def build_api_prompt(market: str) -> str:
    """Build a self-contained prompt for Anthropic API mode.

    In API mode, Claude has no MCP access, so we pre-fetch all data
    and inject it into the prompt. Predictions are returned as JSON
    for the script to parse and log.

    Args:
        market: "US" or "KR".

    Returns:
        Full prompt string with data embedded.
    """
    if market == "US":
        market_data = fetch_us_market_data()
        prompt_template = (PROMPTS_DIR / "briefing_us.md").read_text()
    else:
        market_data = fetch_kr_market_data()
        prompt_template = (PROMPTS_DIR / "briefing_kr.md").read_text()

    track_record = get_track_record_context()
    prompt = prompt_template.replace("{market_data}", market_data)
    prompt = prompt.replace("{track_record}", track_record)

    if market == "KR":
        try:
            us_context = fetch_us_market_data()
        except Exception:
            us_context = "US market data unavailable"
        prompt = prompt.replace("{us_context}", us_context)

    return prompt


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


def parse_predictions(response: str) -> list[dict]:
    """Extract prediction JSON from Claude's response.

    Looks for JSON arrays inside ```json code blocks.

    Args:
        response: Full response text from Claude.

    Returns:
        List of prediction dicts parsed from JSON blocks.
    """
    predictions = []
    json_blocks = re.findall(r"```json\s*([\s\S]*?)```", response)

    for block in json_blocks:
        try:
            parsed = json.loads(block.strip())
            if isinstance(parsed, list):
                predictions.extend(parsed)
            elif isinstance(parsed, dict):
                predictions.append(parsed)
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse JSON block: %s", e)

    return predictions


def _augment_gate_components(pred, providers: dict) -> None:
    """Ensure a LIVE BULL carries the gate's components (overextension/return_1m).

    In API mode the model returns prediction JSON; if it omits ``components`` the
    store-level overextension gate would fail open. This recomputes those two
    fields authoritatively from fresh bars and merges them (without overwriting
    values the model did supply) so the gate fires regardless. Fail-open: any
    fetch/compute error leaves the prediction unchanged (never blocks logging).

    Args:
        pred: The Prediction about to be inserted (mutated in place).
        providers: ``{"US": USMarketProvider, "KR": KoreanMarketProvider}`` cache.
    """
    if pred.source != "LIVE" or pred.direction != "BULL":
        return
    comps = dict(pred.components or {})
    if "overextension" in comps and "return_1m" in comps:
        return
    try:
        from dataclasses import asdict

        provider = providers.get(pred.market)
        if provider is None:
            return
        bars = provider.get_price_history(pred.ticker, days=400)
        if not bars:
            return
        m = compute_horizon_metrics(
            bars=[asdict(b) for b in bars], ticker=pred.ticker, market=pred.market
        )
        comps.setdefault("overextension", m.overextension_level)
        if m.return_1m is not None:
            comps.setdefault("return_1m", m.return_1m)
        pred.components = comps
    except Exception as e:  # noqa: BLE001 — augmentation is best-effort
        logger.debug("gate-component augmentation failed for %s: %s", pred.ticker, e)


def log_predictions(predictions: list[dict]) -> int:
    """Insert parsed predictions into the database.

    Only used in API mode — in Claude Code mode, predictions are
    logged directly by Claude via MCP create_prediction calls.

    Args:
        predictions: List of prediction dicts from Claude's response.

    Returns:
        Number of predictions successfully logged.
    """
    conn = get_connection()
    logged = 0
    # Reused across rows so the gate-component augmentation fetch happens on one
    # provider instance per market, not one per prediction.
    providers = {"US": USMarketProvider(), "KR": KoreanMarketProvider()}

    for p in predictions:
        try:
            # Validate the raw JSON contract before constructing/inserting so a
            # malformed LLM row is rejected with clear messages, not an opaque
            # downstream error.
            schema_errors = validate_prediction_dict(p)
            if schema_errors:
                logger.warning(
                    "Skipping malformed prediction %s: %s",
                    p.get("ticker"),
                    "; ".join(schema_errors),
                )
                continue

            # Honour the Stage 11 recalibration guarantee in API mode too: map
            # the model's raw confidence through the source-scoped curve, keep the
            # raw value in raw_confidence, and persist any component scores. This
            # mirrors `predict create --recalibrate`, which the CLI-driven modes use.
            raw_conf = float(p.get("confidence", 0.5))
            stored_conf, _ = recalibrate_confidence(conn, raw_conf, "LIVE")
            comps = p.get("components")
            pred = Prediction(
                ticker=str(p.get("ticker", "")).upper(),
                market=str(p.get("market", "US")).upper(),
                direction=str(p.get("direction", "NEUTRAL")).upper(),
                confidence=stored_conf,
                raw_confidence=raw_conf,
                components=comps if isinstance(comps, dict) else None,
                timeframe=str(p.get("timeframe", "1W")),
                reasoning=str(p.get("reasoning", "")),
                entry_price=float(p.get("entry_price", 0)),
                signals_used=p.get("signals_used", []),
                source="LIVE",
                target_price=p.get("target_price"),
                stop_price=p.get("stop_price"),
            )

            # Validate required fields
            if not pred.ticker or pred.entry_price <= 0:
                logger.warning("Skipping invalid prediction: %s", p)
                continue

            # Populate the overextension gate's components from fresh data when
            # the model omitted them, so the store gate can't fail open here.
            _augment_gate_components(pred, providers)

            # LIVE BEAR predictions are gated at the store (measured ~0% win
            # rate). Skip them explicitly here so they are an intentional,
            # visible no-op rather than an opaque insert error swallowed below.
            if pred.direction == "BEAR":
                logger.info(
                    "Skipping LIVE BEAR prediction (gated): %s %s %s",
                    pred.ticker,
                    pred.market,
                    pred.timeframe,
                )
                continue

            insert_prediction(conn, pred)
            logged += 1
            logger.info(
                "Logged prediction: %s %s %s (conf: %.0f%%, tf: %s)",
                pred.ticker,
                pred.market,
                pred.direction,
                pred.confidence * 100,
                pred.timeframe,
            )
        except Exception as e:
            logger.error("Failed to log prediction: %s — %s", p, e)

    conn.close()
    return logged


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_briefing(market: str, mode: str = "codex-cli") -> None:
    """Run the full daily briefing pipeline for a market.

    Args:
        market: "US", "KR", or "ALL".
        mode: "codex-cli" (default, codex CLI), "claude-code" (claude CLI),
            or "api" (Anthropic API directly).
    """
    markets = ["US", "KR"] if market == "ALL" else [market.upper()]

    for mkt in markets:
        logger.info("Generating %s market briefing (mode: %s)", mkt, mode)

        try:
            if mode == "claude-code":
                prompt = build_claude_code_prompt(mkt)
                logger.info("Calling claude CLI for %s briefing", mkt)
                response = call_claude_code(prompt)
                # In claude-code mode, predictions are logged by Claude via MCP.
                # No need to parse and log separately.
            elif mode == "codex-cli":
                prompt = build_claude_code_prompt(mkt)
                logger.info("Calling codex CLI for %s briefing", mkt)
                response = call_codex_cli(prompt)
                # In codex-cli mode, predictions are logged by Codex executing
                # bin/stock-cli predict create via Bash (same as claude-code).
                # Guard against silent failure: codex sometimes exits 0 with a
                # short apology message instead of the briefing. The expected
                # briefing starts with "# 📊 Daily Market Briefing" and runs
                # several KB. Anything materially smaller indicates the LLM
                # never produced the deliverable.
                stripped = response.strip()
                if len(stripped) < 500 or "Daily Market Briefing" not in stripped:
                    raise RuntimeError(
                        f"codex returned suspiciously short/non-briefing output "
                        f"({len(stripped)} chars). First 200: {stripped[:200]!r}"
                    )
            else:
                prompt = build_api_prompt(mkt)
                logger.info(
                    "Calling Anthropic API for %s briefing (%d chars)", mkt, len(prompt)
                )
                response = call_claude_api(prompt)
                # In API mode, parse predictions from response and log manually
                predictions = parse_predictions(response)
                logger.info("Parsed %d predictions from response", len(predictions))
                logged = log_predictions(predictions)
                logger.info("Logged %d/%d predictions", logged, len(predictions))

        except Exception as e:
            logger.error("Briefing generation failed: %s", e)
            try:
                send_briefing(f"⚠️ Daily briefing failed for {mkt}: {e}")
            except Exception:
                pass
            continue

        # Send via Telegram
        try:
            send_briefing(response, title=f"{mkt} Daily Briefing")
            logger.info("Briefing sent via Telegram")
        except Exception as e:
            logger.warning("Telegram delivery failed: %s", e)

        logger.info("%s briefing complete", mkt)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate daily market briefing")
    parser.add_argument(
        "--market",
        choices=["US", "KR", "ALL"],
        default="ALL",
        help="Market to generate briefing for (default: ALL)",
    )
    parser.add_argument(
        "--mode",
        choices=["claude-code", "codex-cli", "api"],
        default="codex-cli",
        help=(
            "codex-cli (default): uses `codex exec` CLI (ChatGPT Plus credit), no API key needed. "
            "claude-code: uses `claude -p` CLI; subject to Anthropic headless-subscription throttling "
            "as of 2026-05 (see docs/stage-9/codex-cli-mode.md). "
            "api: uses Anthropic API directly, requires ANTHROPIC_API_KEY."
        ),
    )
    args = parser.parse_args()

    run_briefing(args.market, args.mode)
