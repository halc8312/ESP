import pytest

from jobs.scrape_tasks import execute_scrape_job
from services.scrape_request import (
    build_scrape_job_context,
    build_scrape_task_request,
    classify_target_url,
)


ITEM_URLS = [
    ("https://jp.mercari.com/item/m12345678901", "mercari"),
    ("https://jp.mercari.com/shops/product/xhF79ibHz3KwucSXev32C5", "mercari"),
    ("https://item.fril.jp/abcdef1234567890", "rakuma"),
    ("https://store.shopping.yahoo.co.jp/somestore/item123.html", "yahoo"),
    ("https://www.suruga-ya.jp/product/detail/128003655", "surugaya"),
    ("https://netmall.hardoff.co.jp/product/123456/", "offmall"),
    ("https://page.auctions.yahoo.co.jp/jp/auction/x1234567890", "yahuoku"),
    ("https://snkrdunk.com/products/DZ5485-612", "snkrdunk"),
]

SEARCH_URLS = [
    ("https://jp.mercari.com/search?keyword=nike&price_min=1000", "mercari"),
    ("https://fril.jp/s?query=nike&min=1000", "rakuma"),
    ("https://shopping.yahoo.co.jp/search?p=nike&pf=1000", "yahoo"),
    ("https://www.suruga-ya.jp/search?search_word=nike&is_stock=1", "surugaya"),
    ("https://netmall.hardoff.co.jp/search/?q=nike", "offmall"),
    ("https://auctions.yahoo.co.jp/search/search?p=nike&va=nike", "yahuoku"),
    ("https://snkrdunk.com/search?keywords=jordan", "snkrdunk"),
]


@pytest.mark.parametrize("url,expected_site", ITEM_URLS)
def test_classify_target_url_item(url, expected_site):
    kind, site = classify_target_url(url)
    assert kind == "item"
    assert site == expected_site


@pytest.mark.parametrize("url,expected_site", SEARCH_URLS)
def test_classify_target_url_search(url, expected_site):
    kind, site = classify_target_url(url)
    assert kind == "search"
    assert site == expected_site


def test_classify_target_url_unknown_domain_falls_back_to_item():
    assert classify_target_url("https://example.com/whatever") == ("item", "mercari")
    assert classify_target_url("") == ("item", "mercari")


def test_build_scrape_job_context_item_url_keeps_limit_one():
    context = build_scrape_job_context(
        site="mercari",
        target_url="https://jp.mercari.com/item/m12345678901",
        keyword="",
        limit=50,
        persist_to_db=False,
    )
    assert context["limit"] == 1
    assert context["limit_label"] == "1件"


def test_build_scrape_job_context_search_url_uses_requested_limit():
    context = build_scrape_job_context(
        site="mercari",
        target_url="https://jp.mercari.com/search?keyword=nike",
        keyword="",
        limit=50,
        persist_to_db=False,
    )
    assert context["limit"] == 50
    assert context["limit_label"] == "50件"
    assert context["site_label"] == "検索結果URLから抽出"


def test_execute_scrape_job_routes_search_url_to_search_scraper(monkeypatch):
    calls = {}

    def fake_search(search_url, max_items, max_scroll, headless):
        calls["search_url"] = search_url
        calls["max_items"] = max_items
        return [
            {
                "url": "https://jp.mercari.com/item/m-1",
                "title": "Search URL Item",
                "price": 500,
                "image_urls": [],
            }
        ]

    def fail_single(*args, **kwargs):
        raise AssertionError("scrape_single_item should not be called for search URLs")

    monkeypatch.setattr("jobs.scrape_tasks.scrape_search_result", fake_search)
    monkeypatch.setattr("jobs.scrape_tasks.scrape_single_item", fail_single)
    monkeypatch.setattr("jobs.scrape_tasks.filter_excluded_items", lambda items, user_id: (items, 0))
    monkeypatch.setattr(
        "jobs.scrape_tasks.filter_items_by_price",
        lambda items, price_min, price_max: (items, 0),
    )

    target_url = "https://jp.mercari.com/search?keyword=nike&price_min=1000"
    request_payload = build_scrape_task_request(
        site="mercari",
        target_url=target_url,
        keyword="",
        price_min=None,
        price_max=None,
        sort="",
        category=None,
        limit=20,
        user_id=1,
        persist_to_db=False,
        shop_id=None,
    )

    result = execute_scrape_job(request_payload)

    assert calls["search_url"] == target_url
    assert calls["max_items"] >= 20
    assert result["error_msg"] == ""
    assert result["search_url"] == target_url
    assert result["items"][0]["title"] == "Search URL Item"


def test_execute_scrape_job_routes_item_url_to_single_scraper(monkeypatch):
    def fake_single(url, headless):
        return [
            {
                "url": url,
                "title": "Single Item",
                "price": 800,
                "image_urls": [],
            }
        ]

    def fail_search(*args, **kwargs):
        raise AssertionError("scrape_search_result should not be called for item URLs")

    monkeypatch.setattr("jobs.scrape_tasks.scrape_single_item", fake_single)
    monkeypatch.setattr("jobs.scrape_tasks.scrape_search_result", fail_search)
    monkeypatch.setattr("jobs.scrape_tasks.filter_excluded_items", lambda items, user_id: (items, 0))
    monkeypatch.setattr(
        "jobs.scrape_tasks.filter_items_by_price",
        lambda items, price_min, price_max: (items, 0),
    )

    request_payload = build_scrape_task_request(
        site="mercari",
        target_url="https://jp.mercari.com/item/m12345678901",
        keyword="",
        price_min=None,
        price_max=None,
        sort="",
        category=None,
        limit=10,
        user_id=1,
        persist_to_db=False,
        shop_id=None,
    )

    result = execute_scrape_job(request_payload)

    assert result["error_msg"] == ""
    assert result["items"][0]["title"] == "Single Item"
