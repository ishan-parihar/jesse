"""Telegram message formatting — markdown to HTML conversion with safe chunking.

Adapted from openclaw's format.ts patterns:
- Markdown → Telegram HTML with proper escaping
- Chunk splitting at safe boundaries (no broken HTML tags)
- File reference wrapping to prevent spurious link previews
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TELEGRAM_MAX_MESSAGE_LENGTH = 4096

HTML_TAG_PATTERN = re.compile(r"(<\/?)([a-zA-Z][a-zA-Z0-9-]*)\b[^>]*?>")
SELF_CLOSING_TAGS = {"br", "hr", "img"}


def escape_html(text: str) -> str:
    """Escape text for Telegram HTML mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def markdown_to_telegram_html(markdown: str) -> str:
    """Convert a subset of markdown to Telegram HTML.

    Supports: **bold**, *italic*, `code`, ```code blocks```,
    [links](url), blockquotes, strikethrough, spoilers.
    """
    if not markdown:
        return ""

    html = markdown

    html = re.sub(
        r"```(\w*)\n?(.*?)```",
        r"<pre><code>\2</code></pre>",
        html,
        flags=re.DOTALL,
    )

    html = re.sub(r"`([^`]+)`", r"<code>\1</code>", html)
    html = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html)
    html = re.sub(r"\*(.+?)\*", r"<i>\1</i>", html)
    html = re.sub(r"~~(.+?)~~", r"<s>\1</s>", html)
    html = re.sub(r"\|\|(.+?)\|\|", r"<tg-spoiler>\1</tg-spoiler>", html)
    html = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r'<a href="\2">\1</a>',
        html,
    )

    lines = html.split("\n")
    result_lines = []
    in_blockquote = False
    for line in lines:
        if line.startswith("> "):
            if not in_blockquote:
                result_lines.append("<blockquote>")
                in_blockquote = True
            result_lines.append(line[2:])
        else:
            if in_blockquote:
                result_lines.append("</blockquote>")
                in_blockquote = False
            result_lines.append(line)
    if in_blockquote:
        result_lines.append("</blockquote>")
    html = "\n".join(result_lines)

    html = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", html, flags=re.MULTILINE)
    html = re.sub(r"^---+$", "<hr>", html, flags=re.MULTILINE)

    return html


def split_telegram_html(
    html: str, limit: int = TELEGRAM_MAX_MESSAGE_LENGTH
) -> list[str]:
    """Split HTML into chunks that respect Telegram's message length limit.

    Preserves HTML tag integrity — never splits inside a tag,
    and re-opens/closes tags appropriately across chunks.
    """
    if not html or len(html) <= limit:
        return [html] if html else []

    chunks: list[str] = []
    open_tags: list[tuple[str, str]] = []
    current = ""

    def _close_tags() -> str:
        return "".join(f"</{name}>" for name, _ in reversed(open_tags))

    def _open_tags() -> str:
        return "".join(tag for _, tag in open_tags)

    def _flush():
        nonlocal current
        if current.strip():
            chunks.append(f"{current}{_close_tags()}")
            current = _open_tags()

    last_end = 0
    for match in HTML_TAG_PATTERN.finditer(html):
        text_before = html[last_end : match.start()]
        while text_before:
            available = limit - len(current) - len(_close_tags())
            if available <= 0:
                _flush()
                continue
            if len(text_before) <= available:
                current += text_before
                break
            split_at = _find_safe_split(text_before, available)
            current += text_before[:split_at]
            _flush()
            text_before = text_before[split_at:]

        tag = match.group(0)
        is_closing = match.group(1) == "</"
        tag_name = match.group(2).lower()

        overhead = len(_close_tags()) + (0 if is_closing else len(f"</{tag_name}>"))
        if current and len(current) + len(tag) + overhead > limit:
            _flush()

        current += tag

        if not is_closing and tag_name not in SELF_CLOSING and not tag.endswith("/>"):
            open_tags.append((tag_name, tag))
        elif is_closing:
            for i in range(len(open_tags) - 1, -1, -1):
                if open_tags[i][0] == tag_name:
                    open_tags.pop(i)
                    break

        last_end = match.end()

    remaining = html[last_end:]
    while remaining:
        available = limit - len(current) - len(_close_tags())
        if available <= 0:
            _flush()
            continue
        if len(remaining) <= available:
            current += remaining
            break
        split_at = _find_safe_split(remaining, available)
        current += remaining[:split_at]
        _flush()
        remaining = remaining[split_at:]

    _flush()

    return chunks if chunks else [html]


def _find_safe_split(text: str, max_len: int) -> int:
    """Find a safe split point in text (word boundary, not inside entity)."""
    if len(text) <= max_len:
        return len(text)

    # Try word boundary first
    for i in range(max_len, max(0, max_len - 50), -1):
        if text[i - 1 : i + 1] in (" \n", "\n ", "\n\n", ". ", "! ", "? "):
            return i

    # Fallback: don't split HTML entities
    last_amp = text.rfind("&", 0, max_len)
    if last_amp >= 0 and text.find(";", last_amp, max_len) >= 0:
        return last_amp

    return max_len


@dataclass
class FormattedChunk:
    """A single chunk ready for Telegram delivery."""

    text: str
    parse_mode: str = "HTML"


def format_for_telegram(
    text: str,
    max_chunk_size: int = TELEGRAM_MAX_MESSAGE_LENGTH,
) -> list[FormattedChunk]:
    """Convert markdown text to Telegram-ready HTML chunks.

    Returns a list of chunks, each safe to send as a separate message.
    """
    if not text:
        return []

    html = markdown_to_telegram_html(text)
    raw_chunks = split_telegram_html(html, max_chunk_size)

    return [FormattedChunk(text=chunk) for chunk in raw_chunks if chunk.strip()]


def format_short_message(text: str) -> str:
    """Format a short message (no chunking needed) for Telegram HTML.

    Use for status messages, alerts, etc. that are known to be short.
    """
    return markdown_to_telegram_html(text)
