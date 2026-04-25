"""Unit tests for the translator service internals (no Argos runtime required)."""
from __future__ import annotations

from services.translator.html_segmenter import iter_html_text_segments
from services.translator.source_hash import (
    compute_source_hash,
    normalize_source_for_hash,
)


def test_compute_source_hash_is_stable_across_whitespace_noise():
    first = "  こんにちは  世界  "
    second = "こんにちは 世界"
    assert compute_source_hash(first) == compute_source_hash(second)


def test_compute_source_hash_changes_when_content_changes():
    assert compute_source_hash("こんにちは") != compute_source_hash("さようなら")


def test_compute_source_hash_empty_returns_empty_string():
    assert compute_source_hash("") == ""
    assert compute_source_hash(None) == ""


def test_normalize_source_preserves_content_but_collapses_whitespace():
    assert normalize_source_for_hash("A  B\n\tC") == "A B\nC"
    assert normalize_source_for_hash("  A   B  ") == "A B"


def test_iter_html_text_segments_extracts_visible_text_only():
    html = "<p>こんにちは<br/>世界</p><ul><li>項目1</li><li>項目2</li></ul>"
    _soup, segments = iter_html_text_segments(html)
    texts = [segment.text for segment in segments]
    assert texts == ["こんにちは", "世界", "項目1", "項目2"]


def test_iter_html_text_segments_rewrite_applies_translation_in_place():
    html = "<p>こんにちは<strong>世界</strong></p>"
    soup, segments = iter_html_text_segments(html)
    for segment in segments:
        if segment.text == "こんにちは":
            segment.apply("Hello")
        elif segment.text == "世界":
            segment.apply("World")
    result = str(soup)
    assert "Hello" in result
    assert "World" in result
    assert "<strong>World</strong>" in result


def test_iter_html_text_segments_skips_whitespace_only_nodes():
    html = "<p>   </p><p>本文</p>"
    _soup, segments = iter_html_text_segments(html)
    assert [s.text for s in segments] == ["本文"]
