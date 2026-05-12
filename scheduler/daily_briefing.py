"""Daily briefing generator.

Two modes of operation:
  --mode claude-code (default): Invokes `claude -p` CLI. Uses Claude Code
      subscription, no API key needed. Claude uses `bin/stock-cli` via Bash
      to fetch data and log predictions.
  --mode api: Calls Anthropic API directly. Requires ANTHROPIC_API_KEY.
      Data is pre-fetched by this script and injected into the prompt.
      Predictions are returned as JSON, parsed, and logged by this script.

Run twice daily:
    07:00 KST — Korean market briefing (before KR open)
    21:00 KST — US market briefing (before US pre-market)

Usage:
    uv run python scheduler/daily_briefing.py --market KR
    uv run python scheduler/daily_briefing.py --market US --mode api
    uv run python scheduler/daily_briefing.py --market ALL
"""

import argparse
import json
import logging
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
)
from metrics import get_track_record, get_calibration_report, get_signal_performance
from providers.us import USMarketProvider
from providers.kr import KoreanMarketProvider
from telegram_sender import send_briefing

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
    """Fetch US market data and format as context string.

    Returns:
        Formatted market data string for prompt injection.
    """
    us = USMarketProvider()

    indices = ["SPY", "QQQ", "DIA"]
    sectors = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLP", "XLU"]

    lines = ["## US Market Data\n"]

    for ticker in indices:
        bars = us.get_price_history(ticker, days=10)
        if bars:
            latest = bars[-1]
            prev = bars[-2] if len(bars) > 1 else bars[0]
            change_pct = (latest.close - prev.close) / prev.close * 100
            lines.append(
                f"**{ticker}**: ${latest.close:.2f} ({change_pct:+.1f}%) | "
                f"Vol: {latest.volume:,}"
            )

    lines.append("\n### Sector Performance (latest close)")
    for ticker in sectors:
        bars = us.get_price_history(ticker, days=10)
        if bars and len(bars) >= 5:
            latest = bars[-1]
            week_ago = bars[-5] if len(bars) >= 5 else bars[0]
            change_1w = (latest.close - week_ago.close) / week_ago.close * 100
            lines.append(f"- {ticker}: ${latest.close:.2f} (1W: {change_1w:+.1f}%)")

    return "\n".join(lines)


def fetch_kr_market_data() -> str:
    """Fetch Korean market data and format as context string.

    Returns:
        Formatted market data string for prompt injection.
    """
    kr = KoreanMarketProvider()

    blue_chips = [
        ("005930", "삼성전자"),
        ("000660", "SK하이닉스"),
        ("035420", "NAVER"),
        ("051910", "LG화학"),
        ("006400", "삼성SDI"),
        ("005380", "현대자동차"),
    ]

    lines = ["## Korean Market Data\n"]

    for ticker, name in blue_chips:
        bars = kr.get_price_history(ticker, days=10)
        if bars:
            latest = bars[-1]
            prev = bars[-2] if len(bars) > 1 else bars[0]
            change_pct = (latest.close - prev.close) / prev.close * 100
            lines.append(
                f"**{ticker} ({name})**: ₩{latest.close:,.0f} ({change_pct:+.1f}%) | "
                f"Vol: {latest.volume:,}"
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
    """Build a prompt for Claude Code CLI that instructs it to use stock-cli.

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
        return f"""Generate a US market daily briefing for {today}.

Follow the `daily-briefing` skill in `.claude/skills/daily-briefing/SKILL.md`.
All data access goes through `bin/stock-cli` via Bash.

Specifically:
1. Fetch SPY, QQQ, DIA and sector ETFs (XLK, XLF, XLE, XLV, XLI, XLP, XLU) with
   `bin/stock-cli price <TICKER> --market US --days 10`
2. Check existing state: `bin/stock-cli predict list --status OPEN --market US`
3. Check track record: `bin/stock-cli track-record --days 30 --market US`
4. Generate 2-3 predictions, logging each with
   `bin/stock-cli predict create ... --source LIVE`

Your recent track record (for calibration):
{track_record}

Your current portfolio (pre-fetched — DO NOT skip the portfolio review section
if positions exist below):
{portfolio_context}

Rules:
- Minimum confidence 0.55, maximum 0.85
- Every prediction needs at least 2 signals
- Target must be at least 2x stop distance
- Primary timeframe: 1W (Short). Produce Short/Medium(1M)/Long(6M)/Cycle(1Y) horizons per the expect skill.
- Must actually call `bin/stock-cli predict create` for each horizon ≥ 0.60 confidence per pick
- Use --source LIVE (this is the automated scheduler, not interactive)

After creating predictions, output the full briefing as markdown. Include a
"## Predictions Logged" section listing each prediction ID returned by
the predict create commands."""

    else:  # KR
        return f"""Generate a Korean market daily briefing for {today}.

Follow the `daily-briefing` and `korean-market-analysis` skills in
`.claude/skills/`. All data access goes through `bin/stock-cli` via Bash.

Specifically:
1. Fetch Korean blue chips with
   `bin/stock-cli price <TICKER> --market KR --days 10`:
   005930 (삼성전자), 000660 (SK하이닉스), 035420 (NAVER),
   051910 (LG화학), 006400 (삼성SDI), 005380 (현대자동차)
2. Fetch US reference data for cross-market context:
   `bin/stock-cli price SPY --market US --days 10`
   `bin/stock-cli price NVDA --market US --days 10`
   `bin/stock-cli price SMH --market US --days 10`
3. Check existing state: `bin/stock-cli predict list --status OPEN --market KR`
4. Check track record: `bin/stock-cli track-record --days 30 --market KR`
5. Generate 2-3 Korean predictions, logging each with
   `bin/stock-cli predict create ... --source LIVE`

Your recent track record (for calibration):
{track_record}

Your current portfolio (pre-fetched — DO NOT skip the portfolio review section
if positions exist below):
{portfolio_context}

Rules:
- Korean stocks: same 4-horizon analysis; report Short(1W), Medium(1M), Long(6M), Cycle(1Y).
- Minimum confidence 0.55, maximum 0.85
- Stop-loss wider than US by ~20%
- Target at least 2x stop distance
- Consider won/dollar impact on exporters
- Cross-market: NVDA/SMH moves affect Samsung/SK Hynix with 1-day lag
- Must actually call `bin/stock-cli predict create` for each pick
- Use --source LIVE

After creating predictions, output the full briefing as markdown. Include a
"## Predictions Logged" section listing each prediction ID returned by
the predict create commands."""


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
        timeout=300,  # 5 minute timeout
        cwd=str(PROJECT_ROOT),
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI failed (exit {result.returncode}): {result.stderr[:500]}"
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

    for p in predictions:
        try:
            pred = Prediction(
                ticker=str(p.get("ticker", "")).upper(),
                market=str(p.get("market", "US")).upper(),
                direction=str(p.get("direction", "NEUTRAL")).upper(),
                confidence=float(p.get("confidence", 0.5)),
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


def run_briefing(market: str, mode: str = "claude-code") -> None:
    """Run the full daily briefing pipeline for a market.

    Args:
        market: "US", "KR", or "ALL".
        mode: "claude-code" (uses claude CLI + MCP) or "api" (uses Anthropic API).
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
        choices=["claude-code", "api"],
        default="claude-code",
        help=(
            "claude-code: uses `claude -p` CLI with MCP servers, no API key needed. "
            "api: uses Anthropic API directly, requires ANTHROPIC_API_KEY. "
            "(default: claude-code)"
        ),
    )
    args = parser.parse_args()

    run_briefing(args.market, args.mode)
