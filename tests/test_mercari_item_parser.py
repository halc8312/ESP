import json

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
