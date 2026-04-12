import json

import offmall_db
import snkrdunk_db
import surugaya_db
import yahoo_db
import yahuoku_db


class MockElement:
    def __init__(self, text="", attrib=None):
        self.text = text
        self.attrib = attrib or {}

    def css(self, selector):
        return []


class MockPage:
    def __init__(self, *, find_map=None, css_map=None, text=""):
        self._find_map = find_map or {}
        self._css_map = css_map or {}
        self._text = text

    def find(self, selector):
        return self._find_map.get(selector)

    def css(self, selector):
        return list(self._css_map.get(selector, []))

    def get_all_text(self):
        return self._text


class MockResponse:
    def __init__(self, html, url):
        self.content = html.encode("utf-8")
        self.text = html
        self.status_code = 200
        self.url = url


def test_yahoo_detail_tracks_next_data_strategy_with_meta_fallback(monkeypatch):
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
                                    "images": {"list": []},
                                }
                            }
                        }
                    }
                )
            )
        },
        css_map={
            "meta[name='description']": [MockElement(attrib={"content": "meta description"})],
            "meta[property='og:image']": [MockElement(attrib={"content": "https://img.example.com/yahoo-meta.jpg"})],
            "meta[property='og:title']": [MockElement(attrib={"content": "Yahoo Camera meta"})],
        },
    )

    monkeypatch.setattr("services.scraping_client.fetch_static", lambda url: page)

    result = yahoo_db.scrape_item_detail_light("https://store.shopping.yahoo.co.jp/test/item-1.html")

    assert result["title"] == "Yahoo Camera"
    assert result["description"] == "meta description"
    assert result["image_urls"] == ["https://img.example.com/yahoo-meta.jpg"]
    assert result["_scrape_meta"]["strategy"] == "next_data"
    assert result["_scrape_meta"]["field_sources"]["description"] == "meta"
    assert result["_scrape_meta"]["field_sources"]["images"] == "meta"


def test_yahuoku_detail_tracks_next_data_strategy_with_meta_image_fallback(monkeypatch):
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
        css_map={
            "meta[name='description']": [MockElement(attrib={"content": "auction meta description"})],
            "meta[property='og:image']": [MockElement(attrib={"content": "https://img.example.com/auction-meta.jpg"})],
            "meta[property='og:title']": [MockElement(attrib={"content": "Auction Item meta"})],
        },
        text="開催中",
    )

    monkeypatch.setattr("services.scraping_client.fetch_static", lambda url: page)

    result = yahuoku_db.scrape_item_detail_light("https://auctions.yahoo.co.jp/jp/auction/f123456789")

    assert result["title"] == "Auction Item"
    assert result["description"] == "auction meta description"
    assert result["image_urls"] == ["https://img.example.com/auction-meta.jpg"]
    assert result["_scrape_meta"]["strategy"] == "next_data"
    assert result["_scrape_meta"]["field_sources"]["images"] == "meta"


def test_offmall_detail_tracks_jsonld_strategy_with_css_description(monkeypatch):
    page = MockPage(
        css_map={
            "script[type='application/ld+json']": [
                MockElement(
                    text=json.dumps(
                        {
                            "@type": "Product",
                            "name": "Offmall Camera",
                            "description": "https://netmall.hardoff.co.jp/product/12345/",
                            "image": [],
                            "offers": {"price": "9800", "availability": "https://schema.org/InStock"},
                        }
                    )
                )
            ],
            "div.product-detail-point__box": [MockElement(text="Visible detail description")],
            "span.product-detail-price__main": [MockElement(text="9,800円")],
            "meta[property='og:image']": [MockElement(attrib={"content": "https://img.example.com/offmall-meta.jpg"})],
            "img[src*='hardoff']": [],
            ".item-condition, .condition, [class*='rank'], [class*='condition']": [],
        },
        text="カートに入れる",
    )

    monkeypatch.setattr("services.scraping_client.fetch_static", lambda url: page)

    result = offmall_db.scrape_item_detail_light("https://netmall.hardoff.co.jp/product/12345/")

    assert result["title"] == "Offmall Camera"
    assert result["description"] == "Visible detail description"
    assert result["price"] == 9800
    assert result["_scrape_meta"]["strategy"] == "json_ld"
    assert result["_scrape_meta"]["field_sources"]["description"] == "css"
    assert result["_scrape_meta"]["field_sources"]["price"] == "css"


def test_snkrdunk_prefers_next_data_over_jsonld_and_tracks_sources(monkeypatch):
    page = MockPage(
        find_map={
            "#__NEXT_DATA__": MockElement(
                text=json.dumps(
                    {
                        "props": {
                            "pageProps": {
                                "item": {
                                    "name": "Next Data Title",
                                    "price": 21111,
                                    "description": "",
                                    "images": [],
                                }
                            }
                        }
                    }
                )
            )
        },
        css_map={
            "script[type='application/ld+json']": [
                MockElement(
                    text=json.dumps(
                        {
                            "@context": "https://schema.org/",
                            "@type": "Product",
                            "name": "JSON-LD Title",
                            "description": "json ld description",
                            "offers": {"lowPrice": 35200, "availability": "https://schema.org/InStock"},
                            "image": "https://img.example.com/jsonld.jpg",
                        }
                    )
                )
            ],
            "meta[name='description']": [MockElement(attrib={"content": "meta desc"})],
            "meta[property='og:image']": [MockElement(attrib={"content": "https://img.example.com/meta.jpg"})],
            "meta[property='og:title']": [MockElement(attrib={"content": "Meta Title"})],
            "meta[name='twitter:title']": [],
            "title": [MockElement(text="Meta Title | スニダン")],
        },
        text="Next Data Title",
    )

    monkeypatch.setattr("services.scraping_client.fetch_static", lambda url: page)

    result = snkrdunk_db.scrape_item_detail_light("https://snkrdunk.com/products/CT8013-170")

    assert result["title"] == "Next Data Title"
    assert result["price"] == 21111
    assert result["description"] == "meta desc"
    assert result["image_urls"] == ["https://img.example.com/meta.jpg"]
    assert result["_scrape_meta"]["strategy"] == "next_data"
    assert result["_scrape_meta"]["field_sources"]["title"] == "next_data"
    assert result["_scrape_meta"]["field_sources"]["description"] == "meta"


def test_surugaya_detail_tracks_mixed_provenance_and_invalid_primary_fallback_with_fetch_patch(monkeypatch):
    html = """
    <html>
      <head>
        <meta property="og:title" content="Surugaya Meta Title" />
        <meta property="og:image" content="https://img.example.com/surugaya-meta.jpg" />
        <meta name="description" content="Surugaya meta description" />
        <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "Surugaya JSON Title",
            "image": ["https://www.suruga-ya.jp/no_photo.gif"],
            "offers": {
              "@type": "Offer",
              "price": "0",
              "availability": "https://schema.org/InStock"
            }
          }
        </script>
      </head>
      <body>
        <h1>Surugaya CSS Title</h1>
        <div class="price_group"><span class="text-price-detail">1,980円(税込)</span></div>
        <div class="btn_buy">カートに入れる</div>
        <div id="product_detail">Visible detail description</div>
        <img id="item_picture" src="https://www.suruga-ya.jp/no_photo.gif" />
      </body>
    </html>
    """

    monkeypatch.setattr(surugaya_db, "_fetch_with_retry", lambda session, url, timeout=30, max_attempts=3: (MockResponse(html, url), None))

    result = surugaya_db.scrape_item_detail(object(), "https://www.suruga-ya.jp/product/detail/1")

    assert result["title"] == "Surugaya JSON Title"
    assert result["price"] == 1980
    assert result["description"] == "Surugaya meta description"
    assert result["image_urls"] == ["https://img.example.com/surugaya-meta.jpg"]
    assert result["_scrape_meta"]["strategy"] == "json_ld"
    assert result["_scrape_meta"]["field_sources"]["title"] == "json_ld"
    assert result["_scrape_meta"]["field_sources"]["price"] == "css"
    assert result["_scrape_meta"]["field_sources"]["description"] == "meta"
    assert result["_scrape_meta"]["field_sources"]["images"] == "meta"


def test_surugaya_detail_marks_javascript_disabled_page_unknown_and_alerts(monkeypatch):
    html = """
    <html>
      <head><title>JavaScript is disabled</title></head>
      <body>
        <p>JavaScript is disabled</p>
        <p>Please enable JavaScript to continue.</p>
      </body>
    </html>
    """

    class FakeDispatcher:
        def __init__(self):
            self.events = []

        def notify_scrape_issue(self, **payload):
            self.events.append(payload)
            return True

    alerts = FakeDispatcher()
    monkeypatch.setattr("services.scrape_alerts.get_alert_dispatcher", lambda: alerts)
    monkeypatch.setattr(
        surugaya_db,
        "_fetch_with_retry",
        lambda session, url, timeout=30, max_attempts=3: (MockResponse(html, url), None),
    )

    result = surugaya_db.scrape_item_detail(object(), "https://www.suruga-ya.jp/product/detail/1")

    assert result["status"] == "unknown"
    assert result["price"] is None
    assert result["title"] == ""
    assert result["_scrape_meta"]["strategy"] == "degraded"
    assert "degraded-marker:javascript is disabled" in result["_scrape_meta"]["reasons"]
    assert alerts.events[-1]["event_type"] == "unknown_detail_result"
