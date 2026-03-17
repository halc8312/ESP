import ast
import inspect
import json
from pathlib import Path
from unittest.mock import patch

from services.patrol.base_patrol import PatrolResult


MODULES_WITHOUT_SELENIUM = [
    "yahoo_db",
    "offmall_db",
    "snkrdunk_db",
    "yahuoku_db",
    "surugaya_db",
    "services.patrol.yahoo_patrol",
    "services.patrol.offmall_patrol",
    "services.patrol.snkrdunk_patrol",
    "services.patrol.yahuoku_patrol",
    "services.patrol.surugaya_patrol",
]


class MockElement:
    def __init__(self, text="", attrib=None):
        self.text = text
        self.attrib = attrib or {}


class MockPage:
    def __init__(self, *, find_map=None, css_map=None, text="", body=""):
        self._find_map = find_map or {}
        self._css_map = css_map or {}
        self._text = text
        self.body = body

    def find(self, selector):
        return self._find_map.get(selector)

    def css(self, selector):
        return list(self._css_map.get(selector, []))

    def css_first(self, selector):
        nodes = self.css(selector)
        return nodes[0] if nodes else None

    def get_all_text(self):
        return self._text


class MockResponse:
    def __init__(self, html, url):
        self.content = html.encode("utf-8")
        self.text = html
        self.status_code = 200
        self.url = url


def test_debug_scripts_removed():
    assert not Path("debug_scrape.py").exists()
    assert not Path("debug_children.py").exists()
    assert not Path("debug_variant_json.py").exists()


def test_no_selenium_imports_in_stage4_modules():
    for module_name in MODULES_WITHOUT_SELENIUM:
        module = __import__(module_name, fromlist=["dummy"])
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imported_module = getattr(node, "module", None) or ""
                imported_names = [alias.name for alias in node.names]
                assert "selenium" not in imported_module, module_name
                assert "selenium" not in imported_names, module_name
                assert "create_driver" not in imported_names, module_name
                assert "webdriver_manager" not in imported_module, module_name


def test_patrol_fetch_ignores_driver_for_stage4_sites():
    from services.patrol.offmall_patrol import OffmallPatrol
    from services.patrol.snkrdunk_patrol import SnkrdunkPatrol
    from services.patrol.surugaya_patrol import SurugayaPatrol
    from services.patrol.yahoo_patrol import YahooPatrol
    from services.patrol.yahuoku_patrol import YahuokuPatrol

    patrol_classes = [
        YahooPatrol,
        OffmallPatrol,
        SnkrdunkPatrol,
        YahuokuPatrol,
        SurugayaPatrol,
    ]

    for patrol_cls in patrol_classes:
        patrol = patrol_cls()
        with patch.object(patrol, "_fetch_with_scrapling", return_value=PatrolResult(price=1000, status="active")) as mock_fetch:
            result = patrol.fetch("https://example.com/item", driver="legacy-driver")
            mock_fetch.assert_called_once_with("https://example.com/item")
            assert result.success
            assert result.price == 1000


def test_yahoo_scrape_item_detail_supports_legacy_signature():
    import yahoo_db

    page = MockPage(
        find_map={
            "#__NEXT_DATA__": MockElement(
                text=json.dumps(
                    {
                        "props": {
                            "pageProps": {
                                "item": {
                                    "name": "Yahoo Test Item",
                                    "applicablePrice": 1234,
                                    "description": "detail description",
                                    "images": {"list": [{"src": "https://img.example.com/yahoo.jpg"}]},
                                    "stock": {"quantity": 2},
                                    "stockTableOneAxis": {
                                        "firstOption": {
                                            "name": "Color",
                                            "choiceList": [
                                                {
                                                    "choiceName": "Blue",
                                                    "stock": {"quantity": 2},
                                                    "price": 1234,
                                                }
                                            ],
                                        }
                                    },
                                }
                            }
                        }
                    }
                )
            )
        },
        css_map={"meta[name='description']": []},
        text="Yahoo Test Item",
    )

    with patch("services.scraping_client.fetch_static", return_value=page):
        result = yahoo_db.scrape_item_detail(object(), "https://store.shopping.yahoo.co.jp/test/item.html")

    assert result["title"] == "Yahoo Test Item"
    assert result["price"] == 1234
    assert result["variants"][0]["option1_value"] == "Blue"


def test_offmall_search_result_uses_http_only_detail_scrape():
    import offmall_db

    search_page = MockPage(
        css_map={
            "a[href*='/product/']": [
                MockElement(attrib={"href": "https://netmall.hardoff.co.jp/product/12345/"})
            ]
        }
    )
    detail_page = MockPage(
        css_map={
            "script[type='application/ld+json']": [
                MockElement(
                    text=json.dumps(
                        {
                            "@type": "Product",
                            "name": "Offmall Camera",
                            "description": "camera description",
                            "brand": {"name": "HardOff"},
                            "image": ["https://img.example.com/offmall.jpg"],
                            "offers": {"price": "9800", "availability": "https://schema.org/InStock"},
                        }
                    )
                )
            ],
            "meta[property='og:image']": [],
            "img[src*='hardoff']": [],
            ".item-condition, .condition, [class*='rank'], [class*='condition']": [],
        },
        text="camera description",
    )

    with patch("services.scraping_client.fetch_static", side_effect=[search_page, detail_page]):
        results = offmall_db.scrape_search_result("https://netmall.hardoff.co.jp/search?q=camera", max_items=1)

    assert len(results) == 1
    assert results[0]["title"] == "Offmall Camera"
    assert results[0]["status"] == "active"


def test_offmall_detail_prefers_visible_tax_included_price():
    import offmall_db

    detail_page = MockPage(
        css_map={
            "script[type='application/ld+json']": [
                MockElement(
                    text=json.dumps(
                        {
                            "@type": "Product",
                            "name": "Offmall Camera",
                            "description": "camera description",
                            "offers": {"price": "2000", "availability": "https://schema.org/InStock"},
                        }
                    )
                )
            ],
            "span.product-detail-price__main": [MockElement(text="2,200")],
            "div.product-detail-point__box": [],
            "#panel1 th, .product-detail-spec th": [],
            "meta[property='og:image']": [],
            "img[src*='hardoff']": [],
            ".item-condition, .condition, [class*='rank'], [class*='condition']": [],
        },
        text="カートに入れる 2,200 (税込)",
    )

    with patch("services.scraping_client.fetch_static", return_value=detail_page):
        result = offmall_db.scrape_item_detail_light("https://netmall.hardoff.co.jp/product/12345/")

    assert result["price"] == 2200
    assert result["status"] == "active"


def test_offmall_patrol_prefers_visible_tax_included_price():
    from services.patrol.offmall_patrol import OffmallPatrol

    detail_page = MockPage(
        css_map={
            "script[type='application/ld+json']": [
                MockElement(
                    text=json.dumps(
                        {
                            "@type": "Product",
                            "name": "Offmall Camera",
                            "offers": {"price": "2000", "availability": "https://schema.org/InStock"},
                        }
                    )
                )
            ],
            "span.product-detail-price__main": [MockElement(text="2,200")],
        },
        text="カートに入れる 2,200 (税込)",
    )

    with patch("services.scraping_client.fetch_static", return_value=detail_page):
        result = OffmallPatrol().fetch("https://netmall.hardoff.co.jp/product/12345/")

    assert result.price == 2200
    assert result.status == "active"


def test_yahuoku_search_result_uses_http_only_detail_scrape():
    import yahuoku_db

    search_page = MockPage(
        css_map={
            ".Product__titleLink": [
                MockElement(attrib={"href": "https://page.auctions.yahoo.co.jp/auction/g123456789"})
            ]
        }
    )
    detail_page = MockPage(
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
                                                "title": "Auction Console",
                                                "price": {"current": 5555},
                                                "description": "auction description",
                                                "imageList": ["https://img.example.com/auction.jpg"],
                                                "seller": {"name": "seller"},
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
        css_map={"meta[name='description']": [], "meta[property='og:image']": []},
        text="開催中",
    )

    with patch("services.scraping_client.fetch_static", side_effect=[search_page, detail_page]):
        results = yahuoku_db.scrape_search_result("https://auctions.yahoo.co.jp/search/search?p=console", max_items=1)

    assert len(results) == 1
    assert results[0]["title"] == "Auction Console"
    assert results[0]["price"] == 5555


def test_yahuoku_detail_prefers_tax_included_price_and_keeps_open_status():
    import yahuoku_db

    detail_page = MockPage(
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
                                                "title": "Auction Console",
                                                "price": 15000,
                                                "taxinPrice": 16500,
                                                "status": "open",
                                                "seller": {"name": "seller"},
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
        css_map={"meta[name='description']": [], "meta[property='og:image']": []},
        text="価格 16,500円 （税込） 3月21日（土）14時1分 終了予定 購入手続きへ",
    )

    with patch("services.scraping_client.fetch_static", return_value=detail_page):
        result = yahuoku_db.scrape_item_detail_light("https://auctions.yahoo.co.jp/jp/auction/f123456789")

    assert result["price"] == 16500
    assert result["status"] == "active"


def test_yahuoku_patrol_ignores_end_scheduled_text_for_open_auction():
    from services.patrol.yahuoku_patrol import YahuokuPatrol

    detail_page = MockPage(
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
                                                "title": "Auction Console",
                                                "price": 15000,
                                                "taxinPrice": 16500,
                                                "status": "open",
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
        text="価格 16,500円 （税込） 3月21日（土）14時1分 終了予定",
    )

    with patch("services.scraping_client.fetch_static", return_value=detail_page):
        result = YahuokuPatrol().fetch("https://auctions.yahoo.co.jp/jp/auction/f123456789")

    assert result.price == 16500
    assert result.status == "active"


def test_snkrdunk_search_result_uses_scrapling_dynamic_fetch():
    import snkrdunk_db

    search_page = MockPage(
        css_map={
            "a[class*='productTile']": [
                MockElement(attrib={"href": "https://snkrdunk.com/products/CT8013-170"})
            ]
        }
    )
    detail_page = MockPage(
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
        css_map={"meta[name='description']": [], "meta[property='og:image']": []},
        text="Jordan Test",
    )

    with patch("services.scraping_client.fetch_dynamic", return_value=search_page) as mock_dynamic, patch(
        "services.scraping_client.fetch_static", return_value=detail_page
    ):
        results = snkrdunk_db.scrape_search_result("https://snkrdunk.com/search?keywords=jordan", max_items=1)

    mock_dynamic.assert_called_once()
    assert len(results) == 1
    assert results[0]["title"] == "Jordan Test"


def test_snkrdunk_detail_static_jsonld_avoids_dynamic_fallback():
    import snkrdunk_db

    detail_page = MockPage(
        css_map={
            "script[type='application/ld+json']": [
                MockElement(
                    text=json.dumps(
                        {
                            "@context": "https://schema.org/",
                            "@type": "Product",
                            "name": "Jordan Static",
                            "image": "https://img.example.com/snkrdunk-static.jpg",
                            "description": "static description",
                            "offers": {
                                "@type": "AggregateOffer",
                                "lowPrice": 35200,
                                "availability": "https://schema.org/InStock",
                            },
                        }
                    )
                )
            ],
            "meta[name='description']": [MockElement(attrib={"content": "meta description"})],
            "meta[property='og:image']": [MockElement(attrib={"content": "https://img.example.com/fallback.jpg"})],
            "meta[property='og:title']": [MockElement(attrib={"content": "Jordan Staticの新品/中古フリマ(通販)｜スニダン"})],
            "title": [MockElement(text="Jordan Staticの新品/中古フリマ(通販)｜スニダン")],
        },
        text="Jordan Static",
    )

    with patch("services.scraping_client.fetch_static", return_value=detail_page) as mock_static, patch(
        "services.scraping_client.fetch_dynamic"
    ) as mock_dynamic:
        result = snkrdunk_db.scrape_item_detail_light("https://snkrdunk.com/products/CT8013-170")

    mock_static.assert_called_once()
    mock_dynamic.assert_not_called()
    assert result["title"] == "Jordan Static"
    assert result["price"] == 35200
    assert result["description"] == "static description"
    assert result["image_urls"] == ["https://img.example.com/snkrdunk-static.jpg"]
    assert result["status"] == "on_sale"


def test_surugaya_scrape_single_item_works_without_browser_fallback():
    import surugaya_db

    html = """
    <html>
      <head><title>Surugaya Test</title></head>
      <body>
        <h1>Surugaya Test</h1>
        <div class="price_group"><span class="text-price-detail">1,980円(税込)</span></div>
        <div class="btn_buy">カートに入れる</div>
        <div id="product_detail">detail text</div>
        <img id="item_picture" src="https://img.example.com/surugaya.jpg" />
      </body>
    </html>
    """

    with patch("surugaya_db.get_session", return_value=object()), patch(
        "surugaya_db._fetch_with_retry",
        return_value=(MockResponse(html, "https://www.suruga-ya.jp/product/detail/1"), None),
    ):
        results = surugaya_db.scrape_single_item("https://www.suruga-ya.jp/product/detail/1")

    assert len(results) == 1
    assert results[0]["title"] == "Surugaya Test"
    assert results[0]["price"] == 1980
