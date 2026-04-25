from __future__ import annotations

import html
import re

import nh3


RICH_TEXT_ALLOWED_TAGS = {"p", "br", "b", "strong", "i", "em", "ul", "ol", "li", "a"}
RICH_TEXT_ALLOWED_ATTRIBUTES = {"a": {"href", "target"}}

_HTML_TAG_PATTERN = re.compile(r"<[A-Za-z][^>]*>")
_BR_TAG_PATTERN = re.compile(r"(?i)<\s*br\s*/?\s*>")
_BLOCK_END_TAG_PATTERN = re.compile(r"(?i)</\s*(p|div|li|ul|ol|blockquote)\s*>")
_HTML_TAG_STRIP_PATTERN = re.compile(r"<[^>]+>")


def _normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _looks_like_html(value: str) -> bool:
    return bool(_HTML_TAG_PATTERN.search(value))


def sanitize_rich_text(raw_html: str | None) -> str:
    return nh3.clean(
        raw_html or "",
        tags=RICH_TEXT_ALLOWED_TAGS,
        attributes=RICH_TEXT_ALLOWED_ATTRIBUTES,
    ).strip()


def normalize_rich_text(raw_value: str | None) -> str:
    value = _normalize_newlines((raw_value or "").strip())
    if not value:
        return ""

    if _looks_like_html(value):
        candidate = value
    else:
        candidate = html.escape(value).replace("\n", "<br>\n")

    return sanitize_rich_text(candidate)


def rich_text_to_plain_text(raw_value: str | None) -> str:
    sanitized = normalize_rich_text(raw_value)
    if not sanitized:
        return ""

    text = _BR_TAG_PATTERN.sub("\n", sanitized)
    text = _BLOCK_END_TAG_PATTERN.sub("\n", text)
    text = _HTML_TAG_STRIP_PATTERN.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")

    normalized_lines: list[str] = []
    previous_blank = True
    for raw_line in text.split("\n"):
        line = " ".join(raw_line.split())
        if line:
            normalized_lines.append(line)
            previous_blank = False
        elif not previous_blank:
            normalized_lines.append("")
            previous_blank = True

    return "\n".join(normalized_lines).strip()


def build_rich_text_excerpt(raw_value: str | None, limit: int = 80) -> str:
    text = rich_text_to_plain_text(raw_value)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"
