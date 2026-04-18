import json
from pathlib import Path

from services.html_page_adapter import HtmlPageAdapter
from services.mercari_item_parser import parse_mercari_item_page


class MockElement:
    def __init__(self, text="", attrib=None):
        self.text = text
        self.attrib = attrib or {}


class MockPage:
    def __init__(self, *, css_map=None, all_text=""):
        self._css_map = css_map or {}
        self._all_text = all_text

    def css(self, selector):
        return list(self._css_map.get(selector, []))

    def get_all_text(self):
        return self._all_text


def test_parse_mercari_item_page_extracts_lazy_data_src_images():
    image_url = "https://static.mercdn.net/item/detail/orig/photos/m123456789_1.jpg"
    page = MockPage(
        css_map={
            "h1": [MockElement(text="Lazy Mercari Item")],
            "img[data-src*='static.mercdn.net'][data-src*='/item/'][data-src*='/photos/']": [
                MockElement(attrib={"src": "", "data-src": image_url})
            ],
        },
        all_text="購入手続きへ",
    )

    item, meta = parse_mercari_item_page(page, "https://jp.mercari.com/item/m123456789")

    assert item["image_urls"] == [image_url]
    assert meta["field_sources"]["image_urls"] == "dom"


def test_parse_mercari_item_page_prefers_highest_resolution_srcset_image():
    image_url = "https://static.mercdn.net/item/detail/orig/photos/m123456789_2.jpg"
    page = MockPage(
        css_map={
            "h1": [MockElement(text="Srcset Mercari Item")],
            "img[srcset*='static.mercdn.net'][srcset*='/item/'][srcset*='/photos/']": [
                MockElement(
                    attrib={
                        "srcset": (
                            "https://static.mercdn.net/item/detail/thumb/photos/m123456789_2.jpg 1x, "
                            f"{image_url} 2x"
                        )
                    }
                )
            ],
        },
        all_text="購入手続きへ",
    )

    item, meta = parse_mercari_item_page(page, "https://jp.mercari.com/item/m123456789")

    assert item["image_urls"] == [image_url]
    assert meta["field_sources"]["image_urls"] == "dom"


def test_parse_mercari_item_page_falls_back_to_jsonld_images():
    image_url = "https://static.mercdn.net/item/detail/orig/photos/m123456789_3.jpg"
    product_jsonld = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "JSON-LD Mercari Item",
            "image": [image_url],
            "offers": {"availability": "https://schema.org/InStock"},
        },
        ensure_ascii=False,
    )
    page = MockPage(
        css_map={
            "script[type='application/ld+json']": [MockElement(text=product_jsonld)],
        },
        all_text="購入手続きへ",
    )

    item, meta = parse_mercari_item_page(page, "https://jp.mercari.com/item/m123456789")

    assert item["image_urls"] == [image_url]
    assert meta["field_sources"]["image_urls"] == "jsonld"


def test_parse_mercari_item_page_falls_back_to_meta_image():
    image_url = "https://static.mercdn.net/item/detail/orig/photos/m123456789_4.jpg"
    page = MockPage(
        css_map={
            "h1": [MockElement(text="Meta Image Mercari Item")],
            "meta[property='og:image']": [MockElement(attrib={"content": image_url})],
        },
        all_text="購入手続きへ",
    )

    item, meta = parse_mercari_item_page(page, "https://jp.mercari.com/item/m123456789")

    assert item["image_urls"] == [image_url]
    assert meta["field_sources"]["image_urls"] == "meta"


def test_parse_mercari_item_page_merges_jsonld_images_when_dom_has_only_first_image():
    dom_image_url = "https://static.mercdn.net/item/detail/orig/photos/m123456789_1.jpg"
    extra_jsonld_image_url = "https://static.mercdn.net/item/detail/orig/photos/m123456789_2.jpg"
    product_jsonld = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "Merged Image Mercari Item",
            "image": [dom_image_url, extra_jsonld_image_url],
            "offers": {"availability": "https://schema.org/InStock"},
        },
        ensure_ascii=False,
    )
    page = MockPage(
        css_map={
            "h1": [MockElement(text="Merged Image Mercari Item")],
            "[data-testid^='image-'] img": [MockElement(attrib={"src": dom_image_url})],
            "script[type='application/ld+json']": [MockElement(text=product_jsonld)],
        },
        all_text="購入手続きへ",
    )

    item, meta = parse_mercari_item_page(page, "https://jp.mercari.com/item/m123456789")

    assert item["image_urls"] == [dom_image_url, extra_jsonld_image_url]
    assert meta["field_sources"]["image_urls"] == "dom+jsonld"


def test_parse_mercari_item_page_keeps_multiple_thumb_item_images_for_same_item():
    image_url_1 = "https://static.mercdn.net/thumb/item/webp/m123456789_1.jpg?1772957892"
    image_url_2 = "https://static.mercdn.net/thumb/item/webp/m123456789_2.jpg?1772957892"
    other_item_image = "https://static.mercdn.net/thumb/item/webp/m999999999_1.jpg?1772957892"
    page = MockPage(
        css_map={
            "h1": [MockElement(text="Thumb Mercari Item")],
            "[data-testid^='image-'] img": [
                MockElement(attrib={"src": image_url_1}),
                MockElement(attrib={"src": image_url_2}),
                MockElement(attrib={"src": other_item_image}),
            ],
        },
        all_text="購入手続きへ",
    )

    item, meta = parse_mercari_item_page(page, "https://jp.mercari.com/item/m123456789")

    assert item["image_urls"] == [image_url_1, image_url_2]
    assert meta["field_sources"]["image_urls"] == "dom"


def test_parse_mercari_item_page_recovers_images_from_embedded_html():
    image_url_1 = "https://static.mercdn.net/item/detail/orig/photos/m123456789_5.jpg"
    image_url_2 = "https://static.mercdn.net/item/detail/orig/photos/m123456789_6.jpg"
    page = MockPage(
        css_map={
            "h1": [MockElement(text="Embedded Mercari Item")],
        },
        all_text="購入手続きへ",
    )
    page.body = (
        "<html><body>"
        "<script>"
        "window.__NEXT_DATA__={\"props\":{\"pageProps\":{\"item\":{\"photos\":["
        f"\"{image_url_1}\",\"{image_url_2}\""
        "]}}}};"
        "</script>"
        "</body></html>"
    )

    item, meta = parse_mercari_item_page(page, "https://jp.mercari.com/item/m123456789")

    assert item["image_urls"] == [image_url_1, image_url_2]
    assert meta["field_sources"]["image_urls"] == "html"


def test_parse_mercari_item_page_normalizes_embedded_html_urls_with_escaped_trailing_backslashes():
    image_url_1 = "https://static.mercdn.net/item/detail/orig/photos/m123456789_7.jpg?1776430877"
    image_url_2 = "https://static.mercdn.net/item/detail/orig/photos/m123456789_8.jpg?1776430877"
    page = MockPage(
        css_map={
            "h1": [MockElement(text="Escaped Embedded Mercari Item")],
        },
        all_text="購入手続きへ",
    )
    page.body = (
        "<html><body><script>"
        f"\"{image_url_1}\\\\\",\"{image_url_2}\\\\\",\"{image_url_1}\""
        "</script></body></html>"
    )

    item, meta = parse_mercari_item_page(page, "https://jp.mercari.com/item/m123456789")

    assert item["image_urls"] == [image_url_1, image_url_2]
    assert meta["field_sources"]["image_urls"] == "html"


def test_parse_mercari_item_page_deleted_fixture_omits_broken_description():
    fixture_path = Path(__file__).resolve().parents[1] / "mercari_page_dump.html"
    page_url = "https://jp.mercari.com/item/m71383569733"
    html = fixture_path.read_text(encoding="utf-8")
    page = HtmlPageAdapter(html, url=page_url)

    item, meta = parse_mercari_item_page(page, page_url)

    assert meta["page_type"] == "deleted_detail"
    assert item["status"] == "deleted"
    assert item["description"] == ""


def test_parse_mercari_item_page_live_fixture_preserves_price_source_and_strategy():
    fixture_path = Path(__file__).resolve().parents[1] / "mercari_page_dump_live.html"
    page_url = "https://jp.mercari.com/item/m56789324689"
    html = fixture_path.read_text(encoding="utf-8")
    page = HtmlPageAdapter(html, url=page_url)

    item, meta = parse_mercari_item_page(page, page_url)

    assert meta["page_type"] == "active_detail"
    assert item["status"] == "on_sale"
    assert item["price"] == 4999
    assert item["image_urls"] == [
        "https://static.mercdn.net/item/detail/orig/photos/m56789324689_1.jpg?1772957892",
        "https://static.mercdn.net/item/detail/orig/photos/m56789324689_2.jpg?1772957892",
    ]
    assert meta["price_source"] == "meta"
    assert meta["strategy"] == "meta"
    assert meta["field_sources"]["price"] == "meta"
