"""Weekly advisory paper-trading review.

Reads the paper-trading book (NAV history + trades) and the prediction store,
attributes realized P&L by exit reason and prediction confidence, and writes
``reports/paper-trading-review-YYYY-MM-DD.md`` with concrete, advisory tuning
recommendations. Advisory only — Doctor Cho applies changes via PR. Extends the
weekly-calibration pattern; the heuristics tie paper P&L back to /expect +
daily-briefing parameters.

    uv run python scheduler/paper_trading_review.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp-prediction-store"))
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass

from paper_trading import models as pt
from paper_trading import review

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] paper_review: %(message)s"
)
logger = logging.getLogger("paper_trading_review")

KR_TZ = ZoneInfo("Asia/Seoul")
CURRENCY_FMT = {"KRW": "₩", "USD": "$"}


def _confidence_by_pid(prediction_ids: list[str]) -> dict:
    """Map prediction_id -> COALESCE(raw_confidence, confidence) from the store."""
    if not prediction_ids:
        return {}
    import models as pred_models

    conn = pred_models.get_connection()
    try:
        placeholders = ",".join("?" * len(prediction_ids))
        rows = conn.execute(
            f"SELECT id, COALESCE(raw_confidence, confidence) AS c "
            f"FROM predictions WHERE id IN ({placeholders})",
            prediction_ids,
        ).fetchall()
    finally:
        conn.close()
    return {r["id"]: r["c"] for r in rows}


def _pct(x) -> str:
    return f"{x:+.2%}" if x is not None else "n/a"


def _recommendations(
    metrics: dict, stats: dict, by_conf: dict, frictions: float, initial: float
) -> list[str]:
    """Heuristic, advisory tuning recommendations derived from the realized stats."""
    recs: list[str] = []
    cum = metrics["cumulative_return"]
    bench = metrics["benchmark_return"]
    if bench is not None and cum is not None and cum < bench - 0.05:
        recs.append(
            f"Lagging the passive benchmark by {bench - cum:.1%}. Fixed target exits cap "
            f"upside in a trending tape — consider an ATR trailing stop (see "
            f"`portfolio/exit_manager.py`) instead of fixed take-profits."
        )
    by_reason = stats.get("by_reason", {})
    n_stop = by_reason.get("stop_hit", {}).get("n", 0)
    n_target = by_reason.get("target_hit", {}).get("n", 0)
    if n_stop > n_target:
        recs.append(
            f"Stop-outs ({n_stop}) exceed target hits ({n_target}) — entries may be chased "
            f"or stops set too tight; raise the BUY confidence floor or widen stops vs ATR."
        )
    for bucket, b in by_conf.items():
        if b["n"] >= 5 and b["avg_ret"] < 0:
            recs.append(
                f"Confidence {bucket} round-trips averaged {b['avg_ret']:+.1%} (n={b['n']}) — "
                f"negative realized edge; raise the BUY confidence floor above this band."
            )
    if (
        stats.get("win_rate") is not None
        and stats["win_rate"] < 0.5
        and stats["n"] >= 10
    ):
        recs.append(
            f"Realized win rate {stats['win_rate']:.0%} is below a coin flip — the long signal "
            f"is not converting to P&L; prioritize signal quality over position sizing."
        )
    if initial:
        recs.append(
            f"Transaction frictions consumed {frictions:,.0f} ({frictions / initial:.2%} of "
            f"initial capital) — a real drag the live system also pays."
        )
    if not recs:
        recs.append("No threshold breached this period; continue monitoring.")
    return recs


def _render_book(conn, market: str) -> str:
    account = pt.get_account(conn, market)
    if account is None:
        return f"## {market}\n\n_No account seeded yet._\n"
    nav_rows = pt.get_nav_history(conn, account.id)
    trades = pt.get_trades(conn, account.id)
    metrics = review.book_metrics(nav_rows)
    realized = review.realized_trades(trades)
    stats = review.win_stats(realized)
    conf_by_pid = _confidence_by_pid([r["prediction_id"] for r in realized])
    by_conf = review.attribute_by_confidence(realized, conf_by_pid)
    frictions = sum(t.fees + t.tax + t.slippage for t in trades)
    sym = CURRENCY_FMT.get(account.base_currency, "")

    lines = [f"## {market} book ({account.base_currency})", ""]
    if not nav_rows:
        lines.append(
            f"_Seeded with {sym}{account.initial_capital:,.0f} but no NAV history yet "
            f"(no trading sessions processed)._\n"
        )
        return "\n".join(lines)
    lines.append(
        f"- Period: **{nav_rows[0].date} → {nav_rows[-1].date}** ({metrics['days']} trading days)"
    )
    lines.append(
        f"- Final NAV: **{sym}{metrics['final_nav']:,.0f}** "
        f"(initial {sym}{account.initial_capital:,.0f})"
    )
    lines.append(f"- Cumulative return: **{_pct(metrics['cumulative_return'])}**")
    lines.append(
        f"- Benchmark (passive index) return: **{_pct(metrics['benchmark_return'])}**"
    )
    if metrics["sharpe"] is not None:
        lines.append(f"- Sharpe (annualized): **{metrics['sharpe']:.2f}**")
    lines.append(f"- Max drawdown: **{_pct(metrics['max_drawdown'])}**")
    lines.append(
        f"- Realized round-trips: **{stats['n']}** | win rate **"
        + (f"{stats['win_rate']:.0%}" if stats["win_rate"] is not None else "n/a")
        + f"** | total P&L **{sym}{stats['total_pnl']:,.0f}**"
    )
    lines.append(f"- Open positions: **{nav_rows[-1].n_positions if nav_rows else 0}**")
    lines.append("")

    if stats["by_reason"]:
        lines.append("### Realized P&L by exit reason")
        lines.append("")
        lines.append("| Exit reason | n | win rate | total P&L |")
        lines.append("|---|---|---|---|")
        for reason, b in sorted(stats["by_reason"].items(), key=lambda kv: -kv[1]["n"]):
            lines.append(
                f"| {reason} | {b['n']} | {b['win_rate']:.0%} | {sym}{b['total_pnl']:,.0f} |"
            )
        lines.append("")

    if by_conf:
        lines.append("### Realized P&L by prediction confidence")
        lines.append("")
        lines.append("| Confidence | n | win rate | avg return | total P&L |")
        lines.append("|---|---|---|---|---|")
        for bucket, b in by_conf.items():
            lines.append(
                f"| {bucket} | {b['n']} | {b['win_rate']:.0%} | {b['avg_ret']:+.1%} | {sym}{b['total_pnl']:,.0f} |"
            )
        lines.append("")

    lines.append("### Recommendations (advisory)")
    lines.append("")
    for rec in _recommendations(
        metrics, stats, by_conf, frictions, account.initial_capital
    ):
        lines.append(f"- {rec}")
    lines.append("")
    return "\n".join(lines)


def render_report(conn, report_date: str, markets: list[str]) -> str:
    lines = [f"# Paper-trading review — {report_date}", ""]
    lines.append(
        "_Simulated long-only book trading the logged LIVE BULL predictions. "
        "Advisory — apply tuning via PR. See `paper_trading/` and "
        "`docs/superpowers/specs/2026-06-23-paper-trading-design.md`._"
    )
    lines.append("")
    for market in markets:
        lines.append(_render_book(conn, market))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly paper-trading review")
    parser.add_argument("--market", choices=["US", "KR", "ALL"], default="ALL")
    parser.add_argument("--as-of", help="Report date YYYY-MM-DD (default: today KST).")
    parser.add_argument("--reports-dir", type=Path, default=PROJECT_ROOT / "reports")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    markets = ["US", "KR"] if args.market == "ALL" else [args.market]
    report_date = args.as_of or datetime.now(KR_TZ).strftime("%Y-%m-%d")

    conn = pt.get_connection()
    try:
        markdown = render_report(conn, report_date, markets)
    finally:
        conn.close()

    if args.dry_run:
        print(markdown)
        return 0

    args.reports_dir.mkdir(parents=True, exist_ok=True)
    path = args.reports_dir / f"paper-trading-review-{report_date}.md"
    path.write_text(markdown, encoding="utf-8")
    logger.info("Wrote %s", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
