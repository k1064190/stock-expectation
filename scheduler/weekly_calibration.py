"""Weekly calibration aggregator.

Reads predictions.db, computes win rate / Brier / per-signal performance for
multiple look-back windows, and writes both a human-readable markdown report
and a machine-readable trend JSON.

Intended to run weekly (cron entry in crontab.example):
    Sunday 22:00 KST: uv run python scheduler/weekly_calibration.py

Usage:
    uv run python scheduler/weekly_calibration.py            # default windows: 7, 30, 90 days
    uv run python scheduler/weekly_calibration.py --since 30d  # single window
    uv run python scheduler/weekly_calibration.py --dry-run   # no files written

Outputs:
    reports/weekly-calibration-YYYY-MM-DD.md
    state/calibration-trend.json (rolling last 12 weeks)

Tracked signals (per-signal win rate aggregation):
    Algorithmic signals - technical, news, fundamental, momentum, volume, cycle,
        valuation, mean_reversion, disclosure, sector, cross_market.
    LLM-judgment signal (added 2026-05): llm_context — captures macro/narrative
        context score from /expect Step 5b and daily-briefing Section 4.
        First 6-8 weeks of data will calibrate whether this signal adds alpha
        over the deterministic ALGO/NEWS table; expect noisy initial readings.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "mcp-prediction-store"))

# Auto-load .env for any API keys / config.
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass

from metrics import (
    get_track_record,
    get_track_record_ci,
    get_calibration_report,
    get_signal_performance,
    get_signal_decay,
    permutation_test_confidence,
)
from models import get_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("weekly_calibration")

KR_TZ = ZoneInfo("Asia/Seoul")
DEFAULT_WINDOWS_DAYS = (7, 30, 90)
TREND_HISTORY_WEEKS = 12  # how many recent reports to keep in calibration-trend.json


def parse_since(value: str) -> int:
    """Parse a ``--since`` flag value into days.

    Accepts ``"30d"``, ``"30"``, ``"4w"``, ``"2m"``. Months use 30-day approximation.
    """
    value = value.strip().lower()
    if value.endswith("d"):
        return int(value[:-1])
    if value.endswith("w"):
        return int(value[:-1]) * 7
    if value.endswith("m"):
        return int(value[:-1]) * 30
    return int(value)


def find_overconfident_buckets(buckets, drift_threshold: float = 0.10) -> list[str]:
    """Return labels of confidence buckets where predicted overshoots actual.

    A bucket counts as overconfident when its predicted confidence exceeds the
    realised accuracy by ``drift_threshold`` or more, AND there are at least
    3 closed predictions in that bucket (small samples are too noisy).
    """
    flagged = []
    for b in buckets:
        if b.count < 3:
            continue
        if b.predicted_confidence - b.actual_accuracy >= drift_threshold:
            flagged.append(b.confidence_range)
    return flagged


def find_worst_signals(
    signals, min_predictions: int = 5, max_win_rate: float = 0.40
) -> list[dict]:
    """Return signal entries that are notably underperforming.

    A signal is surfaced when either condition holds:
      - its significance ``verdict`` is "dead" (hit rate significantly below a
        50% coin flip — a statistical anti-signal worth pruning/inverting), or
      - the legacy heuristic: ``total >= min_predictions`` and
        ``win_rate < max_win_rate`` (catches borderline cases the binomial
        test rates as merely "weak" on small samples).
    """
    return [
        {
            "signal": s.signal,
            "total": s.total,
            "win_rate": s.win_rate,
            "p_value": s.p_value,
            "verdict": s.verdict,
        }
        for s in signals
        if s.verdict == "dead"
        or (s.total >= min_predictions and s.win_rate < max_win_rate)
    ]


def find_decaying_signals(decay) -> list[dict]:
    """Surface signals whose edge did not hold out-of-sample.

    Flags ``train_only`` (worked historically, now a coin flip) and
    ``reversed`` (now a significant loser) — the OOS grades that a static
    all-history win rate would miss.
    """
    return [
        {
            "signal": s.signal,
            "train": f"{s.train_wins}/{s.train_total}",
            "test": f"{s.test_wins}/{s.test_total}",
            "label": s.label,
        }
        for s in decay
        if s.label in ("train_only", "reversed")
    ]


def compute_window(conn, days: int) -> dict:
    """Compute calibration data for a single look-back window.

    Every windowed view (track record, calibration curve, signal performance)
    is restricted to predictions that closed within the last ``days`` days, so
    each report section reflects its own header rather than all-history data.
    ``get_signal_decay`` is the one exception: its train/test split is an
    all-history out-of-sample analysis and is intentionally not windowed.

    Two calibration curves are computed per window: ``buckets`` on the model's
    RAW confidence and ``buckets_recal`` on the as-logged (post-recalibration)
    confidence. Comparing the two shows whether the isotonic recalibrator is
    closing the overconfidence gap. The headline overconfidence warning is based
    on the as-logged curve (what actually ships); the raw flags are kept in
    ``overconfident_buckets_raw`` for context.

    Returns a dict suitable for JSON serialisation; downstream rendering
    converts it into the markdown table.
    """
    track = get_track_record(conn, days=days)
    # Use a low ``min_count`` so the early-life-of-system reports surface
    # their signal mix; a future PR can raise this once volumes grow.
    buckets = get_calibration_report(conn, days=days)
    buckets_recal = get_calibration_report(conn, days=days, use_raw=False)
    signals = get_signal_performance(conn, min_count=3, days=days)
    decay = get_signal_decay(conn, min_count=6)
    # Match the windowed track_record above so the report doesn't pair a
    # windowed point estimate with all-history uncertainty.
    tr_ci = get_track_record_ci(conn, days=days)
    conf_perm = permutation_test_confidence(conn, days=days)
    return {
        "days": days,
        "track_record": asdict(track),
        "buckets": [asdict(b) for b in buckets],
        "buckets_recal": [asdict(b) for b in buckets_recal],
        "signals": [asdict(s) for s in signals],
        # Warn on the as-logged (post-recalibration) curve — the confidence that
        # actually ships — and retain the raw flags for context.
        "overconfident_buckets": find_overconfident_buckets(buckets_recal),
        "overconfident_buckets_raw": find_overconfident_buckets(buckets),
        "worst_signals": find_worst_signals(signals),
        "decaying_signals": find_decaying_signals(decay),
        "robustness": {
            "n": tr_ci.n,
            "win_rate": tr_ci.win_rate,
            "win_rate_ci95": tr_ci.win_rate_ci,
            "brier_ci95": tr_ci.brier_ci,
            "prob_better_than_coin": tr_ci.prob_better_than_coin,
            "confidence_permutation": conf_perm,
        },
    }


def _render_bucket_table(lines: list[str], title: str, buckets: list[dict]) -> None:
    """Append one calibration table (predicted vs actual + drift) under ``title``.

    ``buckets`` are the JSON-serialised CalibrationBucket dicts produced by
    ``compute_window``. A drift of +0.10 or more over a bucket of at least 3
    predictions earns a ⚠️ marker, matching the headline flag threshold.
    """
    lines.append(f"### {title}")
    lines.append("")
    lines.append("| Range | Predicted | Actual | n | Drift |")
    lines.append("|---|---|---|---|---|")
    for b in buckets:
        drift = b["predicted_confidence"] - b["actual_accuracy"]
        marker = " ⚠️" if drift >= 0.10 and b["count"] >= 3 else ""
        lines.append(
            f"| {b['confidence_range']} | {b['predicted_confidence']:.2f} "
            f"| {b['actual_accuracy']:.2f} | {b['count']} | {drift:+.2f}{marker} |"
        )
    lines.append("")


def _render_blend_eval(lines: list[str], blend_eval) -> None:
    """Append the offline learned-blend CV comparison (stage 4a) if available."""
    if not blend_eval:
        return
    lines.append("## Learned blend (offline walk-forward CV)")
    lines.append("")
    if blend_eval.get("status") != "ok":
        lines.append(f"- Status: {blend_eval.get('status')}")
        lines.append("")
        return
    lines.append(
        f"- Rows: **{blend_eval['n_rows']}** components-tagged closed, "
        f"{blend_eval['folds']} expanding folds"
    )
    lines.append("")
    lines.append("| Model | Brier ↓ | AUC ↑ |")
    lines.append("|---|---|---|")
    for name in ("blend", "isotonic", "raw"):
        m = blend_eval.get(name) or {}
        auc_txt = f"{m['auc']:.3f}" if m.get("auc") is not None else "n/a"
        lines.append(f"| {name} | {m.get('brier', 'n/a')} | {auc_txt} |")
    lines.append("")
    verdict = (
        "**blend WINS** — candidate for live wiring (stage 4b, reviewed PR)"
        if blend_eval.get("blend_wins")
        else "blend does not beat the isotonic baseline — keep isotonic"
    )
    lines.append(f"- Verdict: {verdict}")
    lines.append("")


def render_markdown(report_date: str, windows: list[dict], blend_eval=None) -> str:
    """Render the report as markdown for human review."""
    lines = [f"# Weekly calibration — {report_date}", ""]

    # Top-line summary across the standard 30-day window (or first window if custom).
    primary = next((w for w in windows if w["days"] == 30), windows[0])
    tr = primary["track_record"]
    lines.append("## Headline (30-day window)")
    lines.append("")
    lines.append(f"- Total closed: **{tr['total']}**")
    if tr["win_rate"] is not None:
        lines.append(
            f"- Win rate: **{tr['win_rate']:.1%}** ({tr['wins']}W / {tr['losses']}L / {tr['expired']}E)"
        )
    else:
        lines.append("- Win rate: *insufficient data*")
    if tr["brier_score"] is not None:
        lines.append(f"- Brier score: **{tr['brier_score']:.3f}** (lower is better)")
    if tr["avg_return"] is not None:
        # outcome_return is stored as percentage points (e.g. -11.68 == -11.68%),
        # so format with a literal "%"; the "%" spec would multiply by 100.
        lines.append(f"- Average return: **{tr['avg_return']:+.2f}%**")
    lines.append(f"- Current streak: **{tr['current_streak']:+d}**")
    if primary["overconfident_buckets"]:
        lines.append(
            f"- ⚠️ Overconfident even after recalibration: **{', '.join(primary['overconfident_buckets'])}** — the recalibrator isn't closing the gap in this band; investigate before any manual trim (the isotonic curve already trims raw confidence)."
        )
    if primary["worst_signals"]:
        worst_str = ", ".join(
            f"{s['signal']} ({s['win_rate']:.0%}, n={s['total']})"
            for s in primary["worst_signals"]
        )
        lines.append(
            f"- ⚠️ Underperforming signal(s): **{worst_str}** — consider down-weighting."
        )
    lines.append("")

    # Per-window detail.
    for w in windows:
        days = w["days"]
        tr = w["track_record"]
        lines.append(f"## {days}-day window")
        lines.append("")
        lines.append(
            f"Total: {tr['total']} | Win rate: "
            + (f"{tr['win_rate']:.1%}" if tr["win_rate"] is not None else "n/a")
            + f" | Brier: "
            + (f"{tr['brier_score']:.3f}" if tr["brier_score"] is not None else "n/a")
            + f" | Avg return: "
            + (f"{tr['avg_return']:+.2f}%" if tr["avg_return"] is not None else "n/a")
        )
        lines.append("")

        raw_buckets = w["buckets"]
        recal_buckets = w.get("buckets_recal")
        if raw_buckets:
            if recal_buckets and recal_buckets != raw_buckets:
                # Recalibration has diverged from raw — show both curves so the
                # drift columns reveal whether the recalibrator closed the gap.
                _render_bucket_table(
                    lines, "Calibration buckets — raw model confidence", raw_buckets
                )
                _render_bucket_table(
                    lines,
                    "Calibration buckets — as-logged (after recalibration)",
                    recal_buckets,
                )
            else:
                _render_bucket_table(lines, "Calibration buckets", raw_buckets)
                if recal_buckets == raw_buckets:
                    lines.append(
                        "_Raw and as-logged confidence coincide — recalibration is "
                        "not yet diverging in this window._"
                    )
                    lines.append("")
                elif recal_buckets == []:
                    # Recalibration pulled every as-logged confidence below the
                    # 0.50 bucket floor, so there is no as-logged curve to show.
                    lines.append(
                        "_As-logged (post-recalibration) confidence all fell below "
                        "the 0.50 bucket floor — recalibration is pulling raw "
                        "confidence out of the actionable band._"
                    )
                    lines.append("")
                # recal_buckets is None (legacy caller) → no note.

        if w["signals"]:
            lines.append("### Signal performance")
            lines.append("")
            lines.append("| Signal | n | Win rate |")
            lines.append("|---|---|---|")
            # Sort by total desc to surface high-volume signals first.
            for s in sorted(w["signals"], key=lambda x: -x["total"]):
                lines.append(f"| {s['signal']} | {s['total']} | {s['win_rate']:.1%} |")
            lines.append("")

    _render_blend_eval(lines, blend_eval)

    lines.append("---")
    lines.append("")
    lines.append(
        "_Generated by `scheduler/weekly_calibration.py`. The trend file at "
        "`state/calibration-trend.json` keeps the last "
        f"{TREND_HISTORY_WEEKS} reports for plotting._"
    )
    return "\n".join(lines)


def update_trend_file(state_path: Path, entry: dict) -> None:
    """Append the latest snapshot to the rolling trend file.

    Keeps only the last ``TREND_HISTORY_WEEKS`` entries so the file stays
    bounded over years of running.
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    history: list[dict] = []
    if state_path.exists():
        try:
            history = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            logger.warning("calibration-trend.json was corrupt; starting fresh.")

    # Replace any existing entry for the same date (idempotent re-runs).
    history = [h for h in history if h.get("report_date") != entry["report_date"]]
    history.append(entry)
    history.sort(key=lambda h: h.get("report_date", ""))
    history = history[-TREND_HISTORY_WEEKS:]
    state_path.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly calibration aggregator")
    parser.add_argument(
        "--since",
        help="Single look-back window (e.g. 30d, 4w, 2m). "
        "If omitted, computes 7d/30d/90d windows.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print to stdout only; do not write report or update trend file.",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=PROJECT_ROOT / "reports",
        help="Directory to write the markdown report into.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=PROJECT_ROOT / "state",
        help="Directory holding calibration-trend.json.",
    )
    args = parser.parse_args()

    if args.since:
        windows_days = (parse_since(args.since),)
    else:
        windows_days = DEFAULT_WINDOWS_DAYS

    conn = get_connection()
    try:
        windows = [compute_window(conn, d) for d in windows_days]
        # Offline learned-blend CV (stage 4a) — report-only, fail-open.
        try:
            import blend

            blend_eval = blend.evaluate(conn)
        except Exception as exc:  # noqa: BLE001 — never block the report
            logger.warning("blend eval failed (report continues): %s", exc)
            blend_eval = None
    finally:
        conn.close()

    report_date = datetime.now(KR_TZ).strftime("%Y-%m-%d")
    markdown = render_markdown(report_date, windows, blend_eval=blend_eval)

    if args.dry_run:
        print(markdown)
        return 0

    args.reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.reports_dir / f"weekly-calibration-{report_date}.md"
    report_path.write_text(markdown, encoding="utf-8")
    logger.info("Wrote %s", report_path)

    primary = next((w for w in windows if w["days"] == 30), windows[0])
    trend_entry = {
        "report_date": report_date,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "primary_window_days": primary["days"],
        "track_record": primary["track_record"],
        "overconfident_buckets": primary["overconfident_buckets"],
        "overconfident_buckets_raw": primary["overconfident_buckets_raw"],
        "worst_signals": primary["worst_signals"],
    }
    update_trend_file(args.state_dir / "calibration-trend.json", trend_entry)
    logger.info("Updated %s", args.state_dir / "calibration-trend.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
