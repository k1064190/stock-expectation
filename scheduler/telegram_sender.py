"""Telegram message delivery for daily briefings and alerts.

Sends formatted messages via Telegram Bot API. Requires TELEGRAM_BOT_TOKEN
and TELEGRAM_CHAT_ID environment variables.

Usage:
    from scheduler.telegram_sender import send_message, send_briefing
    send_message("Hello from stock-expectation!")
    send_briefing("# Daily Briefing\\n...")
"""

import logging
import os
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}"
MAX_MESSAGE_LENGTH = 4096  # Telegram's limit per message


def _get_config() -> tuple[str, str]:
    """Read Telegram credentials from environment.

    Returns:
        Tuple of (bot_token, chat_id).

    Raises:
        EnvironmentError: If credentials are not set.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise EnvironmentError(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables"
        )
    return token, chat_id


def send_message(
    text: str,
    parse_mode: str = "",
    token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> bool:
    """Send a text message via Telegram Bot API.

    Long messages are automatically split at newline boundaries.

    Plain-text mode (default): every smoke test of the daily briefing on
    2026-05-12 produced at least one ``400 Bad Request: Can't find end of
    the entity`` from a different markdown edge case (``**bold**``, then
    ``[text]`` without URL, then likely ``_`` in snake_case identifiers
    like ``vol_ratio``). The asynchronous, generated-by-LLM nature of the
    briefing text makes any Markdown parser unreliable. We default to
    plain text — markdown markers are stripped from the message body and
    no ``parse_mode`` is sent to Telegram, so the 400 entity errors
    cannot happen. Emojis (📊 🇺🇸 🇰🇷 ✅) carry the visual structure.

    Args:
        text: Message text to send. Markdown markers (``**``, ``*``,
            ``## headers``, orphan ``[brackets]``) are stripped in
            plain-text mode for cleanliness.
        parse_mode: Empty string (default, plain text), ``"Markdown"``,
            or ``"HTML"``. Only set when the caller specifically needs
            formatted output and is prepared for occasional failures.
        token: Bot token override. Defaults to TELEGRAM_BOT_TOKEN env var.
        chat_id: Chat ID override. Defaults to TELEGRAM_CHAT_ID env var.

    Returns:
        True if all message parts sent successfully, False otherwise.
    """
    if not token or not chat_id:
        token, chat_id = _get_config()

    # Strip markdown markers in plain-text mode so the message looks clean
    # rather than displaying literal ``*BUY*`` asterisks. Keep markdown
    # intact when the caller explicitly opted into a parse_mode.
    if not parse_mode:
        text = _to_plaintext(text)

    chunks = _split_message(text)
    success = True

    for chunk in chunks:
        try:
            payload: dict = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode

            resp = httpx.post(
                f"{TELEGRAM_API.format(token=token)}/sendMessage",
                json=payload,
                timeout=30,
            )
            if resp.status_code != 200:
                logger.error("Telegram send failed: %d %s", resp.status_code, resp.text)
                # Last-ditch retry without parse_mode + stripped text.
                if parse_mode:
                    payload.pop("parse_mode", None)
                    payload["text"] = _to_plaintext(chunk)
                    resp = httpx.post(
                        f"{TELEGRAM_API.format(token=token)}/sendMessage",
                        json=payload,
                        timeout=30,
                    )
                    if resp.status_code != 200:
                        success = False
                else:
                    success = False
        except Exception as e:
            logger.error("Telegram send error: %s", e)
            success = False

    return success


def send_briefing(briefing_md: str, title: str = "Daily Briefing") -> bool:
    """Send a formatted daily briefing via Telegram.

    Converts markdown briefing to Telegram-friendly format.

    Args:
        briefing_md: Full briefing in markdown format.
        title: Brief title prefix for the message.

    Returns:
        True if sent successfully.
    """
    # Telegram Markdown has limited support — simplify
    text = _simplify_markdown(briefing_md)
    return send_message(text)


def send_alert(
    ticker: str,
    market: str,
    alert_type: str,
    message: str,
    name: str = "",
) -> bool:
    """Send a prediction alert (approaching target/stop, expired, etc.).

    Args:
        ticker: Stock ticker symbol.
        market: "US" or "KR".
        alert_type: "TARGET", "STOP", "EXPIRED", "HIT", "MISS".
        message: Alert details.
        name: Optional company name to display alongside the ticker.
            When provided, the header line becomes ``{ticker} {name} ({market})``
            so KR tickers like ``005380`` are recognisable at a glance.

    Returns:
        True if sent successfully.
    """
    emoji = {
        "TARGET": "🎯",
        "STOP": "⚠️",
        "EXPIRED": "⏰",
        "HIT": "✅",
        "MISS": "❌",
    }.get(alert_type, "📊")

    name_suffix = f" {name}" if name else ""
    text = f"{emoji} *{alert_type}* | {ticker}{name_suffix} ({market})\n{message}"
    return send_message(text)


def _split_message(text: str) -> list[str]:
    """Split a long message into Telegram-compatible chunks.

    Splits at newline boundaries to avoid breaking mid-line.

    Args:
        text: Full message text.

    Returns:
        List of message chunks, each <= MAX_MESSAGE_LENGTH.
    """
    if len(text) <= MAX_MESSAGE_LENGTH:
        return [text]

    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > MAX_MESSAGE_LENGTH:
            if current:
                chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line

    if current:
        chunks.append(current)

    return chunks


def _to_plaintext(md: str) -> str:
    """Strip markdown formatting markers for plain-text Telegram delivery.

    Removes the visual noise of ``**bold**`` / ``*bold*`` / ``# Header`` /
    ``[orphan brackets]`` so the message reads cleanly without any
    parse_mode. Preserves intentional content — ``snake_case``
    identifiers, real ``[label](url)`` markdown links (kept as
    ``label (url)``), and inline code-style backticks.

    Args:
        md: Markdown-flavoured text.

    Returns:
        Plain text with markup characters removed.
    """
    # ``# Header`` / ``## Header`` / ``### Header`` → ``Header``
    md = re.sub(r"(?m)^\s*#{1,6}\s+", "", md)

    # ``**bold**`` → ``bold`` (non-greedy, must wrap non-whitespace)
    md = re.sub(r"\*\*(\S(?:.*?\S)?)\*\*", r"\1", md, flags=re.DOTALL)

    # ``*bold*`` → ``bold`` (single asterisk; same boundary rule)
    md = re.sub(r"(?<!\*)\*(\S(?:.*?\S)?)\*(?!\*)", r"\1", md, flags=re.DOTALL)

    # ``[label](url)`` → ``label (url)`` so links survive but the syntax
    # noise is gone. Apply BEFORE the orphan-bracket strip below.
    md = re.sub(r"\[([^\[\]]+)\]\(([^)]+)\)", r"\1 (\2)", md)

    # Orphan ``[text]`` (no following ``(`` ) → ``text``.
    md = re.sub(r"\[([^\[\]]+)\](?!\()", r"\1", md)

    return md


def _simplify_markdown(md: str) -> str:
    """Convert full markdown to Telegram-compatible markdown.

    Telegram's Markdown V1 is limited. This simplifies:
    - ## headers → *bold*
    - ### headers → *bold*
    - GitHub-flavored **bold** → Telegram *bold* (defensive: a single
      stray ``**`` left in the input would otherwise produce
      "Can't find end of the entity" 400 errors and force a fallback to
      parse_mode=None, losing all formatting)
    - Tables → simplified text
    - Code blocks → preserved

    Args:
        md: Full markdown text.

    Returns:
        Telegram-compatible markdown string.
    """
    # Convert ``**bold**`` → ``*bold*`` BEFORE per-line processing so
    # multi-line ranges are handled the same way as single-line ones.
    # We require non-greedy capture and a non-whitespace boundary so
    # accidental ``**`` in code blocks (rare) isn't rewritten across
    # huge spans.
    md = re.sub(r"\*\*(\S(?:.*?\S)?)\*\*", r"*\1*", md, flags=re.DOTALL)

    # Telegram MarkdownV1 treats ``[text](url)`` as a link. An LLM
    # occasionally writes ``[*UNDERCONFIDENT*]`` or ``[1순위 / 2순위]``
    # without a following ``(url)``, which Telegram parses as a link
    # start with no end → 400 "Can't find end of the entity". Strip the
    # square brackets when no immediate ``(`` follows, preserving the
    # inner text.
    md = re.sub(r"\[([^\[\]]+)\](?!\()", r"\1", md)

    lines = []
    for line in md.split("\n"):
        if line.startswith("### "):
            lines.append(f"*{line[4:]}*")
        elif line.startswith("## "):
            lines.append(f"*{line[3:]}*")
        elif line.startswith("# "):
            lines.append(f"*{line[2:]}*")
        else:
            lines.append(line)
    return "\n".join(lines)
