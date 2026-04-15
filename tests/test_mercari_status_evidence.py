"""Tests for the evidence-aggregation status classification in mercari_item_parser.

Covers the required scenarios:
  1. JSON-LD InStock + enabled purchase button → on_sale
  2. Body "売り切れ" text alone → NOT instant sold
  3. Disabled button alone → NOT instant sold (soft evidence only)
  4. sold badge + out-of-stock compound strong negative → sold
  5. Deleted page → deleted
  6. Incomplete / hydration-like DOM → no false sold
"""

import json

from services.html_page_adapter import HtmlPageAdapter
from services.mercari_item_parser import (
    _extract_status,
    _extract_body_text,
    parse_mercari_item_page,
)


# ── Helpers ────────────────────────────────────────────────────────────


class MockElement:
    """Minimal element stub used by the parser's _safe_css / _node_text / _node_attr."""

    def __init__(self, text="", attrib=None):
        self.text = text
        self.attrib = attrib or {}


class MockPage:
    """Mimics the page interface expected by mercari_item_parser."""

    def __init__(self, *, css_map=None, all_text=""):
        self._css_map = css_map or {}
        self._all_text = all_text

    def css(self, selector):
        return list(self._css_map.get(selector, []))

    def get_all_text(self):
        return self._all_text


# ── 1. JSON-LD InStock + enabled purchase button → on_sale ────────────


def test_jsonld_instock_and_purchase_button_on_sale():
    """Hard positives from JSON-LD and button should yield on_sale."""
    page = MockPage(
        css_map={
            "[data-testid='checkout-button']": [
                MockElement(text="購入手続きへ"),
            ],
        },
    )
    status, reasons, strength = _extract_status(
        page,
        body_text="",
        availability="https://schema.org/instock",
        deleted=False,
    )
    assert status == "on_sale"
    assert strength == "hard"
    assert "jsonld-in-stock" in reasons
    assert "checkout-button-enabled" in reasons


def test_jsonld_instock_overrides_body_sold_text():
    """JSON-LD InStock (hard positive) must override body 売り切れ (soft negative)."""
    page = MockPage()
    status, reasons, strength = _extract_status(
        page,
        body_text="この商品は売り切れです",
        availability="https://schema.org/instock",
        deleted=False,
    )
    assert status == "on_sale"
    assert strength == "hard"
    assert "jsonld-in-stock" in reasons
    assert "body-sold-marker" in reasons  # soft negative noted but overridden


# ── 2. Body "売り切れ" text alone → NOT instant sold ─────────────────


def test_body_sold_text_alone_is_soft_evidence():
    """A sold marker in body text only should produce soft evidence, not hard sold."""
    page = MockPage()
    status, reasons, strength = _extract_status(
        page,
        body_text="売り切れ",
        availability="",
        deleted=False,
    )
    assert status == "sold"
    assert strength == "soft"
    assert "body-sold-marker" in reasons


def test_body_sold_text_alone_no_hard_negative():
    """Body-only sold text must not be in hard evidence path."""
    page = MockPage()
    status, reasons, strength = _extract_status(
        page,
        body_text="この商品は売り切れです",
        availability="",
        deleted=False,
    )
    assert strength == "soft"


# ── 3. Disabled button alone → NOT hard sold ─────────────────────────


def test_disabled_checkout_button_alone_is_soft():
    """Disabled checkout button is a soft negative; should not confirm hard sold."""
    page = MockPage(
        css_map={
            "[data-testid='checkout-button']": [
                MockElement(text="購入手続きへ", attrib={"disabled": "disabled"}),
            ],
        },
    )
    status, reasons, strength = _extract_status(
        page,
        body_text="",
        availability="",
        deleted=False,
    )
    assert status == "sold"
    assert strength == "soft"
    assert "checkout-button-disabled" in reasons


def test_aria_disabled_checkout_button_is_soft():
    page = MockPage(
        css_map={
            "[data-testid='checkout-button']": [
                MockElement(text="購入手続きへ", attrib={"aria-disabled": "true"}),
            ],
        },
    )
    status, reasons, strength = _extract_status(
        page,
        body_text="",
        availability="",
        deleted=False,
    )
    assert status == "sold"
    assert strength == "soft"


# ── 4. Sold badge + out-of-stock → hard sold ─────────────────────────


def test_sold_badge_plus_outofstock_is_hard_sold():
    """Visible sold badge + JSON-LD OutOfStock = compound hard negative."""
    page = MockPage(
        css_map={
            "[data-testid='sold-out-badge']": [
                MockElement(text="SOLD"),
            ],
        },
    )
    status, reasons, strength = _extract_status(
        page,
        body_text="",
        availability="https://schema.org/outofstock",
        deleted=False,
    )
    assert status == "sold"
    assert strength == "hard"
    assert "jsonld-out-of-stock" in reasons
    assert "sold-badge-visible" in reasons


def test_sold_badge_visible_with_text_is_hard():
    page = MockPage(
        css_map={
            "[data-testid='sold-out-badge']": [
                MockElement(text="売り切れ"),
            ],
        },
    )
    status, reasons, strength = _extract_status(
        page, body_text="", availability="", deleted=False,
    )
    assert status == "sold"
    # The badge alone is hard negative
    assert "sold-badge-visible" in reasons


def test_hidden_sold_badge_is_skipped():
    """aria-hidden badge should not trigger sold classification."""
    page = MockPage(
        css_map={
            "[data-testid='sold-out-badge']": [
                MockElement(text="SOLD", attrib={"aria-hidden": "true"}),
            ],
        },
    )
    status, reasons, strength = _extract_status(
        page, body_text="", availability="", deleted=False,
    )
    # Hidden badge alone → unknown (no real evidence)
    assert status == "unknown"
    assert "sold-badge-hidden-skipped" in reasons


def test_empty_sold_badge_is_soft():
    """Empty badge node (hydration leftover) should be soft, not hard."""
    page = MockPage(
        css_map={
            "[data-testid='sold-out-badge']": [
                MockElement(text=""),
            ],
        },
    )
    status, reasons, strength = _extract_status(
        page, body_text="", availability="", deleted=False,
    )
    assert strength == "soft"
    assert "sold-badge-empty" in reasons


# ── 5. Deleted page → deleted ─────────────────────────────────────────


def test_deleted_page_returns_deleted():
    page = MockPage()
    status, reasons, strength = _extract_status(
        page, body_text="", availability="", deleted=True,
    )
    assert status == "deleted"
    assert strength == "hard"
    assert "deleted-marker" in reasons


# ── 6. Incomplete hydration DOM → no false sold ───────────────────────


def test_empty_page_yields_unknown():
    """A completely empty page (hydration not started) should be unknown."""
    page = MockPage()
    status, reasons, strength = _extract_status(
        page, body_text="", availability="", deleted=False,
    )
    assert status == "unknown"
    assert strength == "none"


def test_hydration_stub_with_title_but_no_buttons_yields_unknown():
    """Page has a title but no buttons or JSON-LD yet → unknown, not sold."""
    page = MockPage(
        css_map={
            "h1": [MockElement(text="Some Product")],
        },
    )
    status, reasons, strength = _extract_status(
        page, body_text="Some Product", availability="", deleted=False,
    )
    assert status == "unknown"


# ── Integration: parse_mercari_item_page ──────────────────────────────


def test_full_parse_jsonld_instock_returns_on_sale():
    jsonld = json.dumps({
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Test Item",
        "offers": {
            "availability": "https://schema.org/InStock",
            "price": "2980",
            "priceCurrency": "JPY",
        },
    }, ensure_ascii=False)
    page = MockPage(
        css_map={
            "script[type='application/ld+json']": [MockElement(text=jsonld)],
            "meta[name='product:price:amount']": [MockElement(attrib={"content": "2980"})],
            "h1": [MockElement(text="Test Item")],
            "[data-testid='checkout-button']": [MockElement(text="購入手続きへ")],
        },
        all_text="Test Item 購入手続きへ ¥2,980",
    )
    item, meta = parse_mercari_item_page(page, "https://jp.mercari.com/item/m999")
    assert item["status"] == "on_sale"
    assert meta["evidence_strength"] == "hard"
    assert "jsonld-in-stock" in meta["reasons"]


def test_full_parse_deleted_page():
    page = MockPage(
        css_map={},
        all_text="お探しの商品は見つかりません",
    )
    item, meta = parse_mercari_item_page(page, "https://jp.mercari.com/item/m000")
    assert item["status"] == "deleted"


def test_full_parse_body_sold_only_is_not_hard():
    """When body text contains 売り切れ but no other signals, should be soft sold."""
    page = MockPage(
        css_map={
            "h1": [MockElement(text="Some Product")],
        },
        all_text="Some Product 売り切れ",
    )
    item, meta = parse_mercari_item_page(page, "https://jp.mercari.com/item/m111")
    assert item["status"] == "sold"
    assert meta["evidence_strength"] == "soft"


def test_conflict_instock_vs_sold_badge_prefers_instock():
    """When JSON-LD says InStock but a visible sold badge exists, positive wins."""
    jsonld = json.dumps({
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Conflict Item",
        "offers": {
            "availability": "https://schema.org/InStock",
            "price": "1500",
        },
    }, ensure_ascii=False)
    page = MockPage(
        css_map={
            "script[type='application/ld+json']": [MockElement(text=jsonld)],
            "meta[name='product:price:amount']": [MockElement(attrib={"content": "1500"})],
            "h1": [MockElement(text="Conflict Item")],
            "[data-testid='sold-out-badge']": [MockElement(text="SOLD")],
        },
        all_text="Conflict Item",
    )
    item, meta = parse_mercari_item_page(page, "https://jp.mercari.com/item/m222")
    # Hard positive (InStock) + hard negative (badge) → positive wins
    assert item["status"] == "on_sale"
    assert "conflict-positive-wins" in meta["reasons"]


# ── _extract_body_text scope narrowing ────────────────────────────────


def test_body_text_prefers_main_over_all():
    """Narrowed body text should prefer <main> content."""
    html = """<html><body>
        <header>ヘッダー 売り切れ</header>
        <main><p>商品説明 購入手続きへ</p></main>
        <footer>フッター 売り切れ</footer>
    </body></html>"""
    page = HtmlPageAdapter(html, url="https://example.com")
    text = _extract_body_text(page)
    assert "商品説明" in text
    # Header/footer "売り切れ" should NOT be in the narrow-scope text
    # because <main> was found and returned first
    assert "ヘッダー" not in text
    assert "フッター" not in text
