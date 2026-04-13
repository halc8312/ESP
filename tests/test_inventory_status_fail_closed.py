import json
from unittest.mock import patch

from services.patrol.offmall_patrol import OffmallPatrol
from services.patrol.snkrdunk_patrol import SnkrdunkPatrol
from services.patrol.surugaya_patrol import SurugayaPatrol
from services.patrol.yahoo_patrol import YahooPatrol
from services.patrol.yahuoku_patrol import YahuokuPatrol

import mercari_db
import offmall_db
import snkrdunk_db
import surugaya_db
import yahoo_db
import yahuoku_db


class MockElement:
    def __init__(self, text="", attrib=None):
        self.text = text
        self.attrib = attrib or {}


class MockPage:
    def __init__(self, *, find_map=None, css_map=None, text=""):
        self._find_map = find_map or {}
        self._css_map = css_map or {}
        self._text = text

    def find(self, selector):
        return self._find_map.get(selector)

    def css(self, selector):
        return list(self._css_map.get(selector, []))

    def css_first(self, selector):
        items = self.css(selector)
        return items[0] if items else None

    def get_all_text(self):
        return self._text


class MockResponse:
    def __init__(self, html, url):
        self.content = html.encode("utf-8")
        self.text = html
        self.status_code = 200
        self.url = url


def test_surugaya_detail_marks_ambiguous_inventory_unknown(monkeypatch):
    html = """
    <html>
      <head>
        <title>Surugaya ambiguous</title>
        <meta property="og:image" content="https://img.example.com/surugaya.jpg" />
      </head>
      <body>
        <h1>Surugaya Test</h1>
        <div class="price_group"><span class="text-price-detail">1,980円(税込)</span></div>
        <div id="product_detail">detail text only</div>
      </body>
    </html>
    """

    monkeypatch.setattr(
        surugaya_db,
        "_fetch_with_retry",
        lambda session, url, timeout=30, max_attempts=3: (MockResponse(html, url), None),
    )

    result = surugaya_db.scrape_item_detail(object(), "https://www.suruga-ya.jp/product/detail/1")

    assert result["title"] == "Surugaya Test"
    assert result["price"] == 1980
    assert result["status"] == "unknown"


def test_yahoo_detail_marks_ambiguous_inventory_unknown(monkeypatch):
    page = MockPage(
        find_map={
            "#__NEXT_DATA__": MockElement(
                text=json.dumps(
                    {
                        "props": {
                            "pageProps": {
                                "item": {
                                    "name": "Yahoo Camera",
                                    "price": 12000,
                                    "description": "detail",
                                }
                            }
                        }
                    }
                )
            )
        },
        css_map={
            "meta[name='description']": [MockElement(attrib={"content": "meta detail"})],
            "meta[property='og:image']": [MockElement(attrib={"content": "https://img.example.com/yahoo.jpg"})],
        },
        text="Yahoo Camera 詳細ページ",
    )

    monkeypatch.setattr("services.scraping_client.fetch_static", lambda url: page)

    result = yahoo_db.scrape_item_detail_light("https://store.shopping.yahoo.co.jp/test/item-1.html")

    assert result["title"] == "Yahoo Camera"
    assert result["status"] == "unknown"


def test_yahuoku_detail_marks_ambiguous_inventory_unknown(monkeypatch):
    page = MockPage(
        find_map={
            "#__NEXT_DATA__": MockElement(
                text=json.dumps(
                    {
                        "props": {
                            "pageProps": {
                                "initialState": {
                                    "item": {
                                        "detail": {
                                            "item": {
                                                "title": "Auction Item",
                                                "currentPrice": 3400,
                                                "auctionID": "f123456789",
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                )
            )
        },
        css_map={},
        text="Auction Item 価格 3,400円",
    )

    monkeypatch.setattr("services.scraping_client.fetch_static", lambda url: page)

    result = yahuoku_db.scrape_item_detail_light("https://auctions.yahoo.co.jp/jp/auction/f123456789")

    assert result["title"] == "Auction Item"
    assert result["status"] == "unknown"


def test_snkrdunk_detail_marks_ambiguous_inventory_unknown(monkeypatch):
    page = MockPage(
        find_map={
            "#__NEXT_DATA__": MockElement(
                text=json.dumps(
                    {
                        "props": {
                            "pageProps": {
                                "item": {
                                    "name": "Jordan Test",
                                    "price": 21111,
                                    "description": "sneaker description",
                                    "images": ["https://img.example.com/snkrdunk.jpg"],
                                }
                            }
                        }
                    }
                )
            )
        },
        css_map={
            "meta[name='description']": [],
            "meta[property='og:image']": [],
            "meta[property='og:title']": [],
            "meta[name='twitter:title']": [],
            "title": [MockElement(text="Jordan Test | スニダン")],
        },
        text="Jordan Test 商品ページ",
    )

    monkeypatch.setattr("services.scraping_client.fetch_static", lambda url: page)

    result = snkrdunk_db.scrape_item_detail_light("https://snkrdunk.com/products/CT8013-170")

    assert result["title"] == "Jordan Test"
    assert result["status"] == "unknown"


def test_mercari_shops_status_requires_purchase_flow_or_stock_count():
    body_text = """
    テスト商品
    ¥2,980
    送料込み
    ショップ情報
    """

    assert mercari_db._infer_mercari_shops_status(body_text) == "unknown"


def test_offmall_detail_marks_ambiguous_inventory_unknown(monkeypatch):
    page = MockPage(
        css_map={
            "script[type='application/ld+json']": [
                MockElement(
                    text=json.dumps(
                        {
                            "@context": "https://schema.org",
                            "@type": "Product",
                            "name": "Offmall Camera",
                            "offers": {"@type": "Offer", "price": "12000"},
                        }
                    )
                )
            ]
        },
        text="Offmall Camera 商品詳細のみ",
    )

    monkeypatch.setattr("services.scraping_client.fetch_static", lambda url: page)

    result = offmall_db.scrape_item_detail_light("https://netmall.hardoff.co.jp/product/123/")

    assert result["title"] == "Offmall Camera"
    assert result["status"] == "unknown"


def test_surugaya_patrol_marks_ambiguous_inventory_unknown():
    html = """
    <html>
      <body>
        <h1>Surugaya Patrol</h1>
        <div class="price_group"><span class="text-price-detail">1,980円(税込)</span></div>
        <div id="product_detail">detail text only</div>
      </body>
    </html>
    """

    page = type("SurugayaPage", (), {"body": html})()
    with patch("services.scraping_client.fetch_static", return_value=page):
        result = SurugayaPatrol().fetch("https://www.suruga-ya.jp/product/detail/1")

    assert result.price == 1980
    assert result.status == "unknown"


def test_offmall_patrol_marks_ambiguous_inventory_unknown():
    page = MockPage(
        css_map={
            "script[type='application/ld+json']": [
                MockElement(
                    text=json.dumps(
                        {
                            "@context": "https://schema.org",
                            "@type": "Product",
                            "name": "Offmall Camera",
                            "offers": {"@type": "Offer", "price": "12000"},
                        }
                    )
                )
            ]
        },
        text="Offmall Camera 商品詳細のみ",
    )

    with patch("services.scraping_client.fetch_static", return_value=page):
        result = OffmallPatrol().fetch("https://netmall.hardoff.co.jp/product/123/")

    assert result.price == 12000
    assert result.status == "unknown"


def test_yahoo_patrol_marks_ambiguous_inventory_unknown():
    page = MockPage(
        find_map={
            "#__NEXT_DATA__": MockElement(
                text=json.dumps(
                    {
                        "props": {
                            "pageProps": {
                                "item": {
                                    "name": "Yahoo Camera",
                                    "price": 12000,
                                }
                            }
                        }
                    }
                )
            )
        },
        text="Yahoo Camera 詳細ページ",
    )

    with patch("services.scraping_client.fetch_static", return_value=page):
        result = YahooPatrol().fetch("https://store.shopping.yahoo.co.jp/test/item-1.html")

    assert result.price == 12000
    assert result.status == "unknown"


def test_yahuoku_patrol_marks_ambiguous_inventory_unknown():
    page = MockPage(
        find_map={
            "#__NEXT_DATA__": MockElement(
                text=json.dumps(
                    {
                        "props": {
                            "pageProps": {
                                "initialState": {
                                    "item": {
                                        "detail": {
                                            "item": {
                                                "title": "Auction Item",
                                                "currentPrice": 3400,
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                )
            )
        },
        text="Auction Item 価格 3,400円",
    )

    with patch("services.scraping_client.fetch_static", return_value=page):
        result = YahuokuPatrol().fetch("https://auctions.yahoo.co.jp/jp/auction/f123456789")

    assert result.price == 3400
    assert result.status == "unknown"


def test_snkrdunk_patrol_marks_ambiguous_inventory_unknown():
    page = MockPage(
        find_map={
            "#__NEXT_DATA__": MockElement(
                text=json.dumps(
                    {
                        "props": {
                            "pageProps": {
                                "item": {
                                    "name": "Jordan Test",
                                    "price": 21111,
                                }
                            }
                        }
                    }
                )
            )
        },
        css_map={"script[type='application/ld+json']": []},
        text="Jordan Test 商品ページ",
    )

    with patch("services.scraping_client.fetch_static", return_value=page):
        result = SnkrdunkPatrol().fetch("https://snkrdunk.com/products/CT8013-170")

    assert result.price == 21111
    assert result.status == "unknown"
