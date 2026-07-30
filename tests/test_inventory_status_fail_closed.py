import json
from unittest.mock import patch

import pytest

from services import scraping_client
from services.html_page_adapter import HtmlPageAdapter
from services.patrol.base_patrol import DELETED_HTTP_STATUSES
from services.patrol.mercari_patrol import MercariPatrol
from services.patrol.offmall_patrol import OffmallPatrol
from services.patrol.snkrdunk_patrol import SnkrdunkPatrol
from services.patrol.surugaya_patrol import SurugayaPatrol
from services.patrol.yahoo_patrol import YahooPatrol
from services.patrol.yahuoku_patrol import YahuokuPatrol
from services.scrape_safety import ScrapeHttpError

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
    def __init__(self, html, url, status_code=200):
        self.content = html.encode("utf-8")
        self.text = html
        self.status_code = status_code
        self.url = url


class MockSession:
    def __init__(self, response):
        self.response = response

    def get(self, url, timeout=30):
        return self.response


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


def test_surugaya_detail_marks_cloudflare_challenge_body_blocked(monkeypatch):
    html = """
    <html>
      <head><title>Just a moment...</title></head>
      <body><script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script></body>
    </html>
    """

    monkeypatch.setattr(
        surugaya_db,
        "_fetch_with_retry",
        lambda session, url, timeout=30, max_attempts=3: (MockResponse(html, url), None),
    )
    monkeypatch.setattr(surugaya_db, "_should_use_global_domain_fallback", lambda: False)

    result = surugaya_db.scrape_item_detail(object(), "https://www.suruga-ya.jp/product/detail/1")

    assert result["status"] == "blocked"
    assert result["price"] is None
    assert result["title"] == ""
    assert result["_scrape_meta"]["strategy"] == "blocked"


def test_surugaya_detail_uses_external_fetch_when_primary_blocked(monkeypatch):
    blocked_html = """
    <html>
      <head><title>Just a moment...</title></head>
      <body><script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script></body>
    </html>
    """
    product_html = """
    <html>
      <head><meta property="og:image" content="https://img.example.com/surugaya.jpg" /></head>
      <body>
        <h1>External Surugaya Item</h1>
        <div class="price_group"><span class="text-price-detail">2,980円(税込)</span></div>
        <button class="btn_buy">カートに入れる</button>
      </body>
    </html>
    """
    blocked_response = MockResponse(blocked_html, "https://www.suruga-ya.jp/product/detail/1", status_code=403)
    external_response = MockResponse(product_html, "https://www.suruga-ya.jp/product/detail/1", status_code=200)
    external_response.source = "test_external"
    monkeypatch.setattr("services.scraping_client.fetch_surugaya_external", lambda url, timeout=60: external_response)

    result = surugaya_db.scrape_item_detail(MockSession(blocked_response), "https://www.suruga-ya.jp/product/detail/1")

    assert result["title"] == "External Surugaya Item"
    assert result["price"] == 2980
    assert result["status"] == "active"


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


def test_mercari_shops_status_treats_purchase_button_as_on_sale():
    body_text = """
    テスト商品
    ¥2,980
    購入する
    """

    assert mercari_db._infer_mercari_shops_status(body_text) == "on_sale"


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


def test_surugaya_patrol_reads_json_ld_offer_when_dom_changes():
    html = f"""
    <html>
      <head>
        <script type="application/ld+json">
          {json.dumps({
              "@context": "https://schema.org",
              "@type": "Product",
              "name": "Surugaya Patrol JSON-LD",
              "offers": {
                  "@type": "Offer",
                  "price": "1980",
                  "availability": "https://schema.org/InStock",
              },
          })}
        </script>
      </head>
      <body><h1>Surugaya Patrol</h1><div id="product_detail">detail text only</div></body>
    </html>
    """

    page = type("SurugayaJsonLdPage", (), {"body": html, "status": 200})()
    with patch("services.scraping_client.fetch_static", return_value=page):
        result = SurugayaPatrol().fetch("https://www.suruga-ya.jp/product/detail/1")

    assert result.price == 1980
    assert result.status == "active"
    assert result.variants == [{"name": "Default Title", "stock": 1, "price": 1980}]


def test_surugaya_patrol_marks_http_block_as_error():
    html = """
    <html>
      <head><title>Just a moment...</title></head>
      <body><script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script></body>
    </html>
    """

    page = type("SurugayaBlockedPage", (), {"body": html, "status": 403})()
    with patch("services.scraping_client.fetch_static", return_value=page), patch(
        "services.scraping_client.fetch_surugaya_external", return_value=None
    ):
        result = SurugayaPatrol().fetch("https://www.suruga-ya.jp/product/detail/1")

    assert result.price is None
    assert result.status == "blocked"
    assert result.error == "HTTP 403"
    assert result.reason == "blocked_http_status"
    assert result.confidence == "low"


def test_surugaya_patrol_uses_external_fetch_when_primary_blocked():
    blocked_html = """
    <html>
      <head><title>Just a moment...</title></head>
      <body><script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script></body>
    </html>
    """
    product_html = """
    <html>
      <body>
        <div class="price_group"><span class="text-price-detail">2,980円(税込)</span></div>
        <button class="btn_buy">カートに入れる</button>
      </body>
    </html>
    """

    blocked_page = type("SurugayaBlockedPage", (), {"body": blocked_html, "status": 403})()
    external_page = type(
        "SurugayaExternalPage",
        (),
        {"body": product_html, "status": 200, "source": "test_external"},
    )()
    with patch("services.scraping_client.fetch_static", return_value=blocked_page), patch(
        "services.scraping_client.fetch_surugaya_external", return_value=external_page
    ):
        result = SurugayaPatrol().fetch("https://www.suruga-ya.jp/product/detail/1")

    assert result.price == 2980
    assert result.status == "active"
    assert result.price_source == "test_external"


def test_surugaya_patrol_marks_challenge_body_as_blocked():
    html = """
    <html>
      <head><title>Just a moment...</title></head>
      <body><script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script></body>
    </html>
    """

    page = type("SurugayaChallengePage", (), {"body": html, "status": 200})()
    with patch("services.scraping_client.fetch_static", return_value=page), patch(
        "services.scraping_client.fetch_surugaya_external", return_value=None
    ):
        result = SurugayaPatrol().fetch("https://www.suruga-ya.jp/product/detail/1")

    assert result.price is None
    assert result.status == "blocked"
    assert result.error == "challenge_page"
    assert result.reason == "blocked_challenge_page"
    assert result.confidence == "low"


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


def test_snkrdunk_patrol_parses_app_router_flight_data_without_css_first():
    class CssCollectionOnlyPage(MockPage):
        def css_first(self, selector):
            raise AssertionError("Scrapling Response does not expose css_first")

    sneaker_data = {
        "sneaker": {
            "id": "CT8013-170",
            "name": 'Nike Air Jordan 12 "Royalty"',
            "imageUrl": "https://cdn.snkrdunk.com/product.webp",
        },
        "sneakerSummary": {
            "minPrice": 32500,
            "usedMinPrice": 20000,
            "listingCount": 28,
            "usedListingCount": 3,
        },
    }
    flight_record = ["$", "$L79", None, {"sneakerData": sneaker_data}]
    flight_data = f"57:{json.dumps(flight_record, ensure_ascii=False)}"
    flight_script = f"self.__next_f.push({json.dumps([1, flight_data], ensure_ascii=False)})"
    page = CssCollectionOnlyPage(
        css_map={
            "script": [MockElement(text=flight_script)],
            "script[type='application/ld+json']": [],
        },
    )

    with patch("services.scraping_client.fetch_static", return_value=page):
        result = SnkrdunkPatrol().fetch(
            "https://snkrdunk.com/products/CT8013-170"
    )

    assert result.success is True
    assert result.price == 32500
    assert result.status == "active"


def test_static_patrols_normalize_missing_item_http_status_as_deleted():
    patrol_cases = (
        (OffmallPatrol, "https://netmall.hardoff.co.jp/product/123/"),
        (YahooPatrol, "https://store.shopping.yahoo.co.jp/test/item-1.html"),
        (YahuokuPatrol, "https://auctions.yahoo.co.jp/jp/auction/f123456789"),
        (SnkrdunkPatrol, "https://snkrdunk.com/products/CT8013-170"),
        (SurugayaPatrol, "https://www.suruga-ya.jp/product/detail/1"),
    )

    for patrol_class, url in patrol_cases:
        page = MockPage()
        page.status = 404
        page.body = ""
        with patch("services.scraping_client.fetch_static", return_value=page):
            result = patrol_class().fetch(url)

        assert result.success is True, patrol_class.__name__
        assert result.status == "deleted", patrol_class.__name__
        assert result.reason == "http-404", patrol_class.__name__


def test_mercari_patrol_normalizes_removed_listing_as_deleted():
    """A removed Mercari listing answers 404; that is "gone", not an error."""
    page = HtmlPageAdapter("", url="https://jp.mercari.com/item/m64539483055", status=404)

    with patch(
        "services.patrol.mercari_patrol.fetch_dynamic", return_value=page
    ) as fetch:
        result = MercariPatrol().fetch("https://jp.mercari.com/item/m64539483055")

    assert result.success is True
    assert result.error is None
    assert result.status == "deleted"
    assert result.reason == "http-404"
    assert fetch.call_args.kwargs["allowed_statuses"] == DELETED_HTTP_STATUSES


def test_mercari_patrol_browser_pool_path_also_normalizes_404():
    page = HtmlPageAdapter("", url="https://jp.mercari.com/item/m1", status=410)

    with patch(
        "services.patrol.mercari_patrol.should_use_mercari_browser_pool_patrol",
        return_value=True,
    ), patch(
        "services.patrol.mercari_patrol.fetch_mercari_page_and_payloads_via_browser_pool_sync",
        return_value=(page, []),
    ) as fetch:
        result = MercariPatrol().fetch("https://jp.mercari.com/item/m1")

    assert result.success is True
    assert result.status == "deleted"
    assert result.reason == "http-410"
    assert fetch.call_args.kwargs["allowed_statuses"] == DELETED_HTTP_STATUSES


def test_dynamic_fetch_returns_404_page_when_status_is_allowed(monkeypatch):
    """Without allowed_statuses a 404 must still fail closed."""
    async def fake_run_browser_page_task(_site, task, **_kwargs):
        class Response:
            status = 404

        class Page:
            url = "https://jp.mercari.com/item/m1"

            async def goto(self, *_a, **_k):
                return Response()

            async def content(self):
                return "<html></html>"

            async def wait_for_load_state(self, *_a, **_k):
                return None

            async def wait_for_timeout(self, *_a, **_k):
                return None

        class Context:
            async def route(self, _pattern, _handler):
                return None

        return await task(Page(), Context())

    monkeypatch.setattr(
        "services.browser_pool.run_browser_page_task", fake_run_browser_page_task
    )

    page = scraping_client.fetch_dynamic(
        "https://jp.mercari.com/item/m1",
        network_idle=False,
        allowed_statuses=DELETED_HTTP_STATUSES,
    )
    assert page.status == 404

    with pytest.raises(ScrapeHttpError, match="404"):
        scraping_client.fetch_dynamic(
            "https://jp.mercari.com/item/m1",
            network_idle=False,
        )
