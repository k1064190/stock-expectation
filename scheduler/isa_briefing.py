"""Monthly ISA contribution briefing runner.

Mirrors scheduler/daily_briefing.py's structure and REUSES its helpers by
import (LLM CLI runners + macro block) plus telegram_sender.send_briefing —
no copied bodies. Pure-data steps (NAV snapshot, status, rebalance) go through
`bin/stock-cli isa ...` subprocesses so the stage-28 gates (tilt clamp,
decision logging) stay in one place.

Flow:
  1. `isa status` — aborts with a clear actionable message (exit 1, no LLM
     call) when the target or the ISA portfolio is missing.
  2. `isa snapshot` — records this month's NAV row first (skipped on
     --dry-run).
  3. `isa rebalance` — band breaches + contribution-only remedy (skipped on
     --dry-run because it logs a decision row; the status payload's band
     check stands in for the prompt preview).
  4. Build the prompt (status + rebalance + macro block + amount + pointer to
     .claude/skills/isa-briefing/SKILL.md) and dispatch via claude-code or
     codex-cli; send the briefing over Telegram unless --no-telegram.

The monthly amount is ALWAYS an explicit --amount argument — never hardcoded,
never defaulted. Usage:

    uv run python scheduler/isa_briefing.py --amount 1000000
    uv run python scheduler/isa_briefing.py --amount 1000000 --dry-run
"""

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scheduler"))

from daily_briefing import _macro_block, call_claude_code, call_codex_cli  # noqa: E402
from telegram_sender import send_briefing  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

STOCK_CLI = PROJECT_ROOT / "bin" / "stock-cli"
# Generous ceiling: each isa subcommand does a handful of KR price fetches
# plus two benchmark fetches.
CLI_TIMEOUT_S = 300


def _stock_cli_json(cli_args: list[str]) -> dict:
    """Run a `bin/stock-cli` subcommand and parse its JSON output.

    Args:
        cli_args: subcommand argv, e.g. ["isa", "status"].

    Returns:
        Parsed JSON dict (error payloads like {"error": ...} pass through —
        the caller decides; nonzero exits with valid JSON are NOT raised).

    Raises:
        RuntimeError: when the CLI produces unparseable output.
    """
    result = subprocess.run(
        [str(STOCK_CLI), *cli_args],
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_S,
        cwd=str(PROJECT_ROOT),
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"stock-cli {' '.join(cli_args)} produced no JSON "
            f"(exit {result.returncode}): {result.stderr[:300]}"
        ) from e


def build_prompt(
    amount_krw: int, status: dict, rebalance: dict, macro_block: str
) -> str:
    """Build the monthly contribution briefing prompt.

    Args:
        amount_krw: this month's explicit contribution amount (KRW).
        status: `isa status` JSON (target, weights, drift, track record).
        rebalance: `isa rebalance` JSON (or the status band check on dry-run).
        macro_block: daily_briefing's macro/geopolitical context block.

    Returns:
        The full prompt string. Judgment (tilt or not) belongs to the LLM;
        execution goes through `isa allocate`, whose ±10%p clamp and decision
        log are code-enforced.
    """
    return f"""이번 달 ISA 적립 브리핑을 작성해줘. `.claude/skills/isa-briefing/SKILL.md`의
워크플로와 하드 룰을 그대로 따라야 한다.

## 이번 달 적립금 (고정 입력)
{amount_krw:,} KRW — 반드시 `bin/stock-cli isa allocate --amount {amount_krw}`로 배분한다.
틸트를 제안하려면 같은 명령에 `--tilt "class=+N,class=-N"`을 붙인다 (틸트는 코드에서
±10%p로 클램프되고 결정 로그에 기록된다 — CLI가 출력한 클램프 결과가 최종이다).
기본은 틸트 없음이며, 틸트는 예외적 상황에서만 2-3문장의 근거와 함께 쓴다.

## 현재 ISA 상태 (isa status)
{json.dumps(status, ensure_ascii=False, indent=2)}

## 리밸런스 밴드 점검 (isa rebalance)
{json.dumps(rebalance, ensure_ascii=False, indent=2)}

## 매크로 컨텍스트
{macro_block or "(매크로 컨텍스트 없음)"}

주의: 매크로 RISK_OFF는 개별 종목 신규 BULL 예측을 막는 게이트다. ISA 월 적립은
장기 DCA 설계상 리스크오프에도 중단하지 않는다 — 리스크오프면 틸트를 0으로 둘 것.
"""


def main(argv: list[str] | None = None) -> int:
    """Run the monthly ISA briefing.

    Args:
        argv: CLI args (None → sys.argv[1:]).

    Returns:
        Process exit code: 0 on success, 1 on a blocked precondition
        (missing target/portfolio) or runner failure.
    """
    parser = argparse.ArgumentParser(description="Monthly ISA contribution briefing")
    parser.add_argument(
        "--amount",
        type=int,
        required=True,
        help="This month's contribution in KRW (always explicit, never defaulted)",
    )
    parser.add_argument(
        "--mode",
        choices=["claude-code", "codex-cli"],
        default="claude-code",
        help="LLM CLI runner (default claude-code)",
    )
    parser.add_argument(
        "--no-telegram", action="store_true", help="Skip Telegram delivery"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the prompt and exit — no snapshot, no LLM, no Telegram",
    )
    args = parser.parse_args(argv)
    if args.amount <= 0:
        print("--amount must be a positive KRW amount", file=sys.stderr)
        return 1

    status = _stock_cli_json(["isa", "status"])
    if "error" in status:
        print(
            f"ISA briefing blocked: {status['error']}",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        # No side effects: skip the snapshot AND the decision-logging
        # `isa rebalance`; the status payload's band check stands in, with
        # the remedy key kept so the prompt shape SKILL.md expects holds.
        rebalance = dict(status.get("rebalance", {}))
        rebalance["min_contribution_to_restore"] = None
        rebalance["notes"] = rebalance.get("notes", []) + [
            "dry-run: remedy not computed (rebalance skipped to avoid decision logging)"
        ]
        print(build_prompt(args.amount, status, rebalance, _macro_block()))
        return 0

    snapshot = _stock_cli_json(["isa", "snapshot"])
    logger.info("NAV snapshot recorded: id=%s", snapshot.get("id"))
    rebalance = _stock_cli_json(["isa", "rebalance"])

    prompt = build_prompt(args.amount, status, rebalance, _macro_block())
    runner = call_claude_code if args.mode == "claude-code" else call_codex_cli
    logger.info("dispatching ISA briefing via %s", args.mode)
    try:
        briefing = runner(prompt)
    except Exception as e:
        print(f"ISA briefing runner failed: {e}", file=sys.stderr)
        return 1

    print(briefing)
    if not args.no_telegram:
        send_briefing(briefing, title="ISA Monthly Briefing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
