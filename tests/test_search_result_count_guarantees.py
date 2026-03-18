import pytest

import offmall_db
import routes.scrape as scrape_routes
import snkrdunk_db
import surugaya_db
import yahoo_db
import yahuoku_db


class MockElement:
    def __init__(self, text="", attrib=None):
        self.text = text
        self.attrib = attrib or {}


class MockPage:
    def __init__(self, *, css_map=None, find_map=None, text=""):
        self._css_map = css_map or {}
        self._find_map = find_map or {}
        self._text = text

    def css(self, selector):
        return list(self._css_map.get(selector, []))

    def find(self, selector):
        return self._find_map.get(selector)

    def get_all_text(self):
        return self._text


class MockResponse:
    def __init__(self, html, url):
        self.content = html.encode("utf-8")
        self.text = html
        self.status_code = 200
        self.url = url


def _build_items(prefix: str, count: int) -> list:
    return [
        {
            "url": f"https://example.com/{prefix}/{idx}",
            "title": f"{prefix}-{idx}",
            "price": 1000 + idx,
            "status": "on_sale",
            "description": "",
            "image_urls": [],
            "variants": [],
        }
        for idx in range(count)
    ]


def test_build_scrape_task_buffers_large_requests_without_changing_visible_limit(monkeypatch):
    captured = {}
    scraped_items = _build_items("yahoo", 70)

    def fake_search_result(search_url, max_items, max_scroll, headless):
        captured["search_url"] = search_url
        captured["max_items"] = max_items
        captured["max_scroll"] = max_scroll
        return list(scraped_items)

    monkeypatch.setattr(scrape_routes.yahoo_db, "scrape_search_result", fake_search_result)
    monkeypatch.setattr(scrape_routes, "filter_excluded_items", lambda items, user_id: (list(items), 12))

    task = scrape_routes._build_scrape_task(
        site="yahoo",
        target_url="",
        keyword="sneaker",
        price_min=None,
        price_max=None,
        sort="created_desc",
        category=None,
        limit=50,
        user_id=1,
        persist_to_db=False,
    )

    result = task()

    assert captured["max_items"] > 50
    assert captured["max_scroll"] > 3
    assert len(result["items"]) == 50
    assert result["excluded_count"] == 12
    assert result["limit"] == 50
    assert "p=sneaker" in captured["search_url"]


@pytest.mark.parametrize(
    ("site", "module_attr", "expected_fragments"),
    [
        ("yahoo", "yahoo_db", ("p=camera", "pf=1000", "pt=5000")),
        ("rakuma", "rakuma_db", ("query=camera", "min=1000", "max=5000")),
        ("surugaya", "surugaya_db", ("search_word=camera", "price=%5B1000%2C5000%5D")),
        ("offmall", "offmall_db", ("q=camera", "min=1000", "max=5000")),
        ("yahuoku", "yahuoku_db", ("p=camera", "va=camera", "aucminprice=1000", "aucmaxprice=5000")),
        ("snkrdunk", "snkrdunk_db", ("keywords=camera", "minPrice=1000", "maxPrice=5000")),
    ],
)
def test_build_scrape_task_includes_native_price_params_for_supported_sites(
    monkeypatch,
    site,
    module_attr,
    expected_fragments,
):
    captured = {}

    def fake_search_result(search_url, max_items, max_scroll, headless):
        captured["search_url"] = search_url
        return []

    monkeypatch.setattr(getattr(scrape_routes, module_attr), "scrape_search_result", fake_search_result)
    monkeypatch.setattr(scrape_routes, "filter_excluded_items", lambda items, user_id: (list(items), 0))

    task = scrape_routes._build_scrape_task(
        site=site,
        target_url="",
        keyword="camera",
        price_min="1000",
        price_max="5000",
        sort="created_desc",
        category=None,
        limit=10,
        user_id=1,
        persist_to_db=False,
    )

    task()

    for fragment in expected_fragments:
        assert fragment in captured["search_url"]


def test_build_scrape_task_applies_post_scrape_price_filter_even_with_native_price_url_support(monkeypatch):
    captured = {}
    scraped_items = [
        {
            "url": "https://example.com/offmall/low",
            "title": "too cheap",
            "price": 500,
            "status": "active",
            "description": "",
            "image_urls": [],
            "variants": [],
        },
        {
            "url": "https://example.com/offmall/ok",
            "title": "within range",
            "price": 2500,
            "status": "active",
            "description": "",
            "image_urls": [],
            "variants": [],
        },
        {
            "url": "https://example.com/offmall/high",
            "title": "too expensive",
            "price": 8000,
            "status": "active",
            "description": "",
            "image_urls": [],
            "variants": [],
        },
        {
            "url": "https://example.com/offmall/unknown",
            "title": "unknown price",
            "price": None,
            "status": "active",
            "description": "",
            "image_urls": [],
            "variants": [],
        },
    ]

    def fake_search_result(search_url, max_items, max_scroll, headless):
        captured["search_url"] = search_url
        return list(scraped_items)

    monkeypatch.setattr(scrape_routes.offmall_db, "scrape_search_result", fake_search_result)
    monkeypatch.setattr(scrape_routes, "filter_excluded_items", lambda items, user_id: (list(items), 0))

    task = scrape_routes._build_scrape_task(
        site="offmall",
        target_url="",
        keyword="camera",
        price_min="1000",
        price_max="5000",
        sort="created_desc",
        category=None,
        limit=10,
        user_id=1,
        persist_to_db=False,
    )

    result = task()

    assert captured["search_url"] == "https://netmall.hardoff.co.jp/search/?q=camera&min=1000&max=5000"
    assert [item["title"] for item in result["items"]] == ["within range"]
    assert result["excluded_count"] == 3
    assert result["price_min"] == 1000
    assert result["price_max"] == 5000


def test_yahoo_search_result_uses_extra_candidates_to_fill_requested_count(monkeypatch):
    urls = [f"https://store.shopping.yahoo.co.jp/test/item-{idx}.html" for idx in range(5)]
    search_page = MockPage(
        css_map={
            "a[class*='SearchResult_SearchResultItem__detailLink']": [
                MockElement(attrib={"href": url}) for url in urls
            ]
        }
    )
    detail_map = {
        urls[0]: {"title": "", "status": "error", "url": urls[0]},
        urls[1]: {"title": "", "status": "error", "url": urls[1]},
        urls[2]: {"title": "Yahoo 3", "status": "on_sale", "url": urls[2]},
        urls[3]: {"title": "Yahoo 4", "status": "on_sale", "url": urls[3]},
        urls[4]: {"title": "Yahoo 5", "status": "on_sale", "url": urls[4]},
    }

    monkeypatch.setattr("services.scraping_client.fetch_static", lambda url: search_page)
    monkeypatch.setattr(yahoo_db, "scrape_item_detail", lambda url: detail_map[url])
    monkeypatch.setattr(yahoo_db, "log_scrape_result", lambda *args, **kwargs: True)

    results = yahoo_db.scrape_search_result("https://shopping.yahoo.co.jp/search?p=sneaker", max_items=3, max_scroll=1)

    assert len(results) == 3
    assert [item["title"] for item in results] == ["Yahoo 3", "Yahoo 4", "Yahoo 5"]


def test_offmall_search_result_uses_extra_candidates_to_fill_requested_count(monkeypatch):
    urls = [f"https://netmall.hardoff.co.jp/product/{idx}/" for idx in range(5)]
    search_page = MockPage(
        css_map={
            "a[href*='/product/']": [MockElement(attrib={"href": url}) for url in urls]
        }
    )
    detail_map = {
        urls[0]: {"title": "", "status": "error", "url": urls[0]},
        urls[1]: {"title": "", "status": "error", "url": urls[1]},
        urls[2]: {"title": "Offmall 3", "status": "active", "url": urls[2]},
        urls[3]: {"title": "Offmall 4", "status": "active", "url": urls[3]},
        urls[4]: {"title": "Offmall 5", "status": "active", "url": urls[4]},
    }

    monkeypatch.setattr("services.scraping_client.fetch_static", lambda url: search_page)
    monkeypatch.setattr(offmall_db, "scrape_item_detail", lambda url: detail_map[url])

    results = offmall_db.scrape_search_result("https://netmall.hardoff.co.jp/search?q=camera", max_items=3, max_scroll=1)

    assert len(results) == 3
    assert [item["title"] for item in results] == ["Offmall 3", "Offmall 4", "Offmall 5"]


def test_yahuoku_search_result_uses_extra_candidates_to_fill_requested_count(monkeypatch):
    urls = [f"https://page.auctions.yahoo.co.jp/auction/g12345678{idx}" for idx in range(5)]
    search_page = MockPage(
        css_map={
            ".Product__titleLink": [MockElement(attrib={"href": url}) for url in urls]
        }
    )
    detail_map = {
        urls[0]: {"title": "", "status": "error", "url": urls[0]},
        urls[1]: {"title": "", "status": "error", "url": urls[1]},
        urls[2]: {"title": "Auction 3", "status": "active", "url": urls[2]},
        urls[3]: {"title": "Auction 4", "status": "active", "url": urls[3]},
        urls[4]: {"title": "Auction 5", "status": "active", "url": urls[4]},
    }

    monkeypatch.setattr("services.scraping_client.fetch_static", lambda url: search_page)
    monkeypatch.setattr(yahuoku_db, "scrape_item_detail", lambda url: detail_map[url])

    results = yahuoku_db.scrape_search_result("https://auctions.yahoo.co.jp/search/search?p=console", max_items=3, max_scroll=1)

    assert len(results) == 3
    assert [item["title"] for item in results] == ["Auction 3", "Auction 4", "Auction 5"]


def test_snkrdunk_search_result_uses_extra_candidates_to_fill_requested_count(monkeypatch):
    urls = [f"https://snkrdunk.com/products/CT8013-17{idx}" for idx in range(5)]
    search_page = MockPage(
        css_map={
            "a[class*='productTile']": [MockElement(attrib={"href": url}) for url in urls]
        }
    )
    detail_map = {
        urls[0]: {"title": "", "status": "error", "url": urls[0]},
        urls[1]: {"title": "", "status": "error", "url": urls[1]},
        urls[2]: {"title": "SNKRDUNK 3", "status": "on_sale", "url": urls[2]},
        urls[3]: {"title": "SNKRDUNK 4", "status": "on_sale", "url": urls[3]},
        urls[4]: {"title": "SNKRDUNK 5", "status": "on_sale", "url": urls[4]},
    }

    monkeypatch.setattr("services.scraping_client.fetch_dynamic", lambda *args, **kwargs: search_page)
    monkeypatch.setattr(snkrdunk_db, "scrape_item_detail", lambda url: detail_map[url])
    monkeypatch.setattr(snkrdunk_db, "log_scrape_result", lambda *args, **kwargs: True)

    results = snkrdunk_db.scrape_search_result("https://snkrdunk.com/search?keywords=jordan", max_items=3, max_scroll=1)

    assert len(results) == 3
    assert [item["title"] for item in results] == ["SNKRDUNK 3", "SNKRDUNK 4", "SNKRDUNK 5"]


def test_snkrdunk_next_page_helper_prefers_explicit_next_link():
    page = MockPage(
        css_map={
            "a[href]": [
                MockElement(text="1", attrib={"href": "/search?keywords=jordan&page=1"}),
                MockElement(text="Next", attrib={"href": "/search?keywords=jordan&page=2", "rel": "next"}),
            ]
        }
    )

    next_url = snkrdunk_db._find_next_page_url(page, "https://snkrdunk.com/search?keywords=jordan")

    assert next_url == "https://snkrdunk.com/search?keywords=jordan&page=2"


def test_surugaya_search_result_uses_extra_candidates_to_fill_requested_count(monkeypatch):
    urls = [f"https://www.suruga-ya.jp/product/detail/{idx}" for idx in range(5)]
    html = "<html><head><title>Surugaya Search</title></head><body></body></html>"
    detail_map = {
        urls[0]: {"title": "", "status": "error", "url": urls[0]},
        urls[1]: {"title": "", "status": "error", "url": urls[1]},
        urls[2]: {"title": "Surugaya 3", "status": "on_sale", "url": urls[2]},
        urls[3]: {"title": "Surugaya 4", "status": "on_sale", "url": urls[3]},
        urls[4]: {"title": "Surugaya 5", "status": "on_sale", "url": urls[4]},
    }

    monkeypatch.setattr(surugaya_db, "get_session", lambda: object())
    monkeypatch.setattr(
        surugaya_db,
        "_fetch_with_retry",
        lambda session, url, timeout=30, max_attempts=3: (MockResponse(html, url), None),
    )
    monkeypatch.setattr(surugaya_db, "_extract_product_urls", lambda soup, base_url: list(urls))
    monkeypatch.setattr(surugaya_db, "_looks_like_challenge_soup", lambda soup: False)
    monkeypatch.setattr(surugaya_db, "_should_use_yahoo_search_fallback", lambda: False)
    monkeypatch.setattr(surugaya_db, "scrape_item_detail", lambda session, url, headless=True: detail_map[url])

    results = surugaya_db.scrape_search_result("https://www.suruga-ya.jp/search?search_word=game", max_items=3, max_scroll=1)

    assert len(results) == 3
    assert [item["title"] for item in results] == ["Surugaya 3", "Surugaya 4", "Surugaya 5"]
