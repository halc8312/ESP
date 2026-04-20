"""
HTML-aware text segmentation for translation.

Rich-text descriptions are stored as a small, sanitised subset of HTML
(see :mod:`services.rich_text`). When translating we must preserve the
tag structure and only rewrite the text nodes; otherwise the Shopify
export would lose paragraph breaks, bold/italic styling, or link hrefs.

The segmenter walks the parse tree of an HTML fragment, yields each
human-visible text node together with a callback that writes the
translated text back into the tree, and leaves every attribute and
structural element untouched.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from bs4 import BeautifulSoup, NavigableString


@dataclass
class HtmlTextSegment:
    """A single translatable chunk extracted from an HTML fragment."""

    text: str
    apply: Callable[[str], None]


def iter_html_text_segments(html: str) -> tuple[BeautifulSoup, list[HtmlTextSegment]]:
    """Parse ``html`` and return its soup plus every non-whitespace text node.

    Each segment exposes an ``apply(new_text)`` callback that replaces the
    original text node. The returned soup is the live tree; call
    ``str(soup)`` after applying translations to recover the rewritten
    HTML fragment.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    segments: list[HtmlTextSegment] = []

    for node in _iter_text_nodes(soup):
        text = str(node)
        stripped = text.strip()
        if not stripped:
            continue

        leading_ws = text[: len(text) - len(text.lstrip())]
        trailing_ws = text[len(text.rstrip()):]

        def _apply(
            translated: str,
            *,
            original_node: NavigableString = node,
            leading: str = leading_ws,
            trailing: str = trailing_ws,
        ) -> None:
            replacement = f"{leading}{translated}{trailing}"
            original_node.replace_with(NavigableString(replacement))

        segments.append(HtmlTextSegment(text=stripped, apply=_apply))

    return soup, segments


def _iter_text_nodes(soup: BeautifulSoup) -> Iterable[NavigableString]:
    """Yield every NavigableString below ``soup`` in document order."""
    # ``descendants`` is a lazy generator so we materialise first to avoid
    # mutating the tree while iterating.
    for node in list(soup.descendants):
        if isinstance(node, NavigableString):
            # Skip comment / CDATA subclasses — treat only real text nodes.
            if type(node) is NavigableString:
                yield node
