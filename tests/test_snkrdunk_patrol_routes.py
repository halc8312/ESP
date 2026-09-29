"""Offline route/identity contracts for apparel and used apparel patrol.

The small payloads below are synthetic schema fixtures, not live-site captures.
"""
import json

import pytest

from services.html_page_adapter import HtmlPageAdapter
from services.patrol.snkrdunk_patrol import SnkrdunkPatrol
from services.scrape_request import classify_target_url
from services.scrape_safety import (
    ScrapeBlockedError,
    UnsafeScrapeUrlError,
    validate_marketplace_url,
)
from snkrdunk_db import _parse_detail_page
from utils import is_valid_detail_url


PARENT = "https://snkrdunk.com/apparels/123456"
USED = PARENT + "/used/987654"


@pytest.mark.parametrize("url", [
    "https://snkrdunk.com/products/CT8013-170",
    PARENT,
    PARENT + "/?slide=right",
    USED,
    USED.replace("snkrdunk.com", "www.snkrdunk.com") + "/",
])
def test_patrol_fetch_and_request_classification_agree_on_supported_routes(url):
    assert is_valid_detail_url(url, "snkrdunk")
    assert validate_marketplace_url(url, "snkrdunk", kind="detail")
    assert classify_target_url(url) == ("item", "snkrdunk")


@pytest.mark.parametrize("url", [
    "https://snkrdunk.com/apparels/",
    "https://snkrdunk.com/apparels/search",
    "https://snkrdunk.com/apparels/123/used/",
    "https://snkrdunk.com/apparels/123/used/987/checkout",
    "https://snkrdunk.com/apparels/123/../987",
    "https://snkrdunk.com/apparels/123%2fused%2f987",
    "https://snkrdunk.com/products/",
    "https://snkrdunk.com/products/example/related",
    "https://snkrdunk.com/search?keywords=apparel",
    "https://snkrdunk.com.evil.example/apparels/123",
    "https://evil.example/?next=https://snkrdunk.com/products/example",
    "https://user:pass@snkrdunk.com/apparels/123",
    "https://snkrdunk.com:8443/apparels/123",
    "http://snkrdunk.com/apparels/123",
])
def test_patrol_and_fetch_reject_non_product_or_unsafe_routes(url):
    assert not is_valid_detail_url(url, "snkrdunk")
    with pytest.raises(UnsafeScrapeUrlError):
        validate_marketplace_url(url, "snkrdunk", kind="detail")


def _product(url, *, price=6500, availability="InStock"):
    return {
        "@type": "Product", "url": url, "name": "Target apparel",
        "image": "https://cdn.snkrdunk.com/target.jpg",
        "offers": {"@type": "Offer", "price": price, "availability": f"https://schema.org/{availability}"},
    }


def _page(payload, *, url=USED, text=""):
    return HtmlPageAdapter(
        '<html><body><script type="application/ld+json">'
        + json.dumps(payload) + "</script>" + text + "</body></html>",
        url=url,
    )


@pytest.mark.parametrize("url", [PARENT, USED])
@pytest.mark.parametrize("availability,expected", [("InStock", "active"), ("OutOfStock", "sold")])
def test_patrol_accepts_target_apparel_offer(monkeypatch, url, availability, expected):
    page = _page(_product(url, availability=availability), url=url)
    monkeypatch.setattr("services.scraping_client.fetch_marketplace_static", lambda *a, **k: page)
    result = SnkrdunkPatrol().fetch(url)
    assert result.success
    assert result.price == 6500
    assert result.status == expected
    assert result.variants[0]["stock"] == (1 if expected == "active" else 0)


def test_used_listing_selects_matching_offer_and_never_parent_minimum():
    parent = _product(PARENT, price=1000)
    parent["offers"] = [
        {"@type": "Offer", "url": PARENT + "/used/111", "price": 1000, "availability": "https://schema.org/InStock"},
        {"@type": "Offer", "url": USED, "price": 6500, "availability": "https://schema.org/OutOfStock"},
    ]
    result = _parse_detail_page(_page(parent, text="関連商品 購入する"), USED)
    assert result["price"] == 6500
    assert result["status"] == "sold"


@pytest.mark.parametrize("as_list", [False, True])
def test_parent_patrol_rejects_child_listing_offer_even_when_product_url_matches(monkeypatch, as_list):
    product = _product(PARENT)
    child = {"@type": "Offer", "url": USED, "price": 1000, "availability": "https://schema.org/OutOfStock"}
    product["offers"] = [child] if as_list else child
    page = _page(product, url=PARENT)
    monkeypatch.setattr("services.scraping_client.fetch_marketplace_static", lambda *a, **k: page)
    result = SnkrdunkPatrol().fetch(PARENT)
    assert not result.success
    assert result.price is None
    assert result.status == "unknown"


def test_parent_selects_its_own_offer_after_unrelated_child_offer():
    product = _product(PARENT)
    product["offers"] = [
        {"@type": "Offer", "url": USED, "price": 1000, "availability": "https://schema.org/OutOfStock"},
        {"@type": "Offer", "url": PARENT, "price": 6500, "availability": "https://schema.org/InStock"},
    ]
    result = _parse_detail_page(_page(product, url=PARENT), PARENT)
    assert result["price"] == 6500
    assert result["status"] == "on_sale"


@pytest.mark.parametrize("availability,status", [(None, "unknown"), ("InStock", "on_sale")])
def test_parent_aggregate_uses_own_fields_and_never_inherits_nested_child_stock(availability, status):
    product = _product(PARENT)
    product["offers"] = {
        "@type": "AggregateOffer", "lowPrice": 6500,
        "offers": [{"@type": "Offer", "url": USED, "price": 1000, "availability": "https://schema.org/OutOfStock"}],
    }
    if availability:
        product["offers"]["availability"] = f"https://schema.org/{availability}"
    result = _parse_detail_page(_page(product, url=PARENT), PARENT)
    assert result["price"] == 6500
    assert result["status"] == status


def test_parent_aggregate_never_inherits_nested_child_price():
    product = _product(PARENT)
    product["offers"] = {
        "@type": "AggregateOffer", "availability": "https://schema.org/InStock",
        "offers": [{"@type": "Offer", "url": USED, "price": 1000, "availability": "https://schema.org/OutOfStock"}],
    }
    result = _parse_detail_page(_page(product, url=PARENT), PARENT)
    assert result["price"] is None
    assert result["status"] == "on_sale"


def test_matching_product_is_selected_after_unrelated_recommendation():
    payload = [_product(PARENT + "/used/111", price=1000), _product(USED)]
    result = _parse_detail_page(_page(payload), USED)
    assert result["price"] == 6500
    assert result["status"] == "on_sale"


@pytest.mark.parametrize("payload", [
    _product(PARENT, price=1000),
    _product(PARENT + "/used/111", price=1000),
    {**_product(USED), "offers": {"@type": "AggregateOffer", "lowPrice": 1000, "availability": "https://schema.org/InStock"}},
    {**_product(USED), "offers": {"@type": "Offer", "url": PARENT + "/used/111", "price": 1000, "availability": "https://schema.org/InStock"}},
    {key: value for key, value in _product(USED).items() if key != "url"},
])
def test_used_patrol_rejects_unverified_identity_and_aggregate_prices(monkeypatch, payload):
    page = _page(payload, text="関連商品 購入する ¥1,000 SOLD OUT")
    monkeypatch.setattr("services.scraping_client.fetch_marketplace_static", lambda *a, **k: page)
    result = SnkrdunkPatrol().fetch(USED)
    assert not result.success
    assert result.price is None
    assert result.status == "unknown"


def test_apparel_missing_inventory_never_infers_stock_from_other_page_content():
    product = _product(USED)
    product["offers"].pop("availability")
    result = _parse_detail_page(_page(product, text="おすすめ 購入する SOLD OUT"), USED)
    assert result["price"] == 6500
    assert result["status"] == "unknown"


@pytest.mark.parametrize("availability,success,status", [("OutOfStock", True, "sold"), ("InStock", False, "unknown")])
def test_used_offer_missing_price_is_only_safe_when_sold(monkeypatch, availability, success, status):
    product = _product(USED, availability=availability)
    product["offers"].pop("price")
    page = _page(product)
    monkeypatch.setattr("services.scraping_client.fetch_marketplace_static", lambda *a, **k: page)
    result = SnkrdunkPatrol().fetch(USED)
    assert result.success is success
    assert result.status == status
    assert result.price is None


@pytest.mark.parametrize("status", [404, 410])
def test_apparel_definitive_http_missing_is_deleted(monkeypatch, status):
    page = _page({}, text="not found")
    page.status = status
    monkeypatch.setattr("services.scraping_client.fetch_marketplace_static", lambda *a, **k: page)
    result = SnkrdunkPatrol().fetch(USED)
    assert result.success
    assert result.status == "deleted"
    assert result.price is None


def test_apparel_block_is_error_not_sold(monkeypatch):
    def blocked(*args, **kwargs):
        raise ScrapeBlockedError("HTTP 403", status_code=403)
    monkeypatch.setattr("services.scraping_client.fetch_marketplace_static", blocked)
    result = SnkrdunkPatrol().fetch(USED)
    assert not result.success
    assert result.status == "unknown"
    assert result.price is None


@pytest.mark.parametrize("target_url,expected_price", [(USED, 6500), (PARENT, None)])
def test_apparel_next_data_requires_exact_target_identity(target_url, expected_price):
    data = {"props": {"pageProps": {"item": {
        "url": target_url, "name": "Used target", "price": 6500, "status": "sold",
    }}}}
    page = HtmlPageAdapter('<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + "</script>", url=USED)
    result = _parse_detail_page(page, USED)
    assert result["price"] == expected_price
    assert result["status"] == ("sold" if expected_price is not None else "unknown")


@pytest.mark.parametrize("url", [PARENT, USED])
@pytest.mark.parametrize("next_item_fields", [{}, {"price": 6500}, {"status": "on_sale"}])
def test_incomplete_target_next_data_falls_back_to_matching_jsonld(monkeypatch, url, next_item_fields):
    next_data = {"props": {"pageProps": {"item": {"url": url, "name": "Partial target", **next_item_fields}}}}
    page = _page(_product(url), url=url, text='<script id="__NEXT_DATA__" type="application/json">' + json.dumps(next_data) + '</script>')
    monkeypatch.setattr("services.scraping_client.fetch_marketplace_static", lambda *a, **k: page)
    result = SnkrdunkPatrol().fetch(url)
    assert result.success
    assert result.price == 6500
    assert result.status == "active"


@pytest.mark.parametrize("other_url", [PARENT, PARENT + "/used/111"])
def test_partial_used_next_data_never_falls_back_to_unrelated_jsonld(other_url):
    next_data = {"props": {"pageProps": {"item": {"url": USED, "name": "Partial target"}}}}
    page = _page(_product(other_url, price=1000, availability="OutOfStock"), text='<script id="__NEXT_DATA__" type="application/json">' + json.dumps(next_data) + '</script>')
    result = _parse_detail_page(page, USED)
    assert result["title"] == "Partial target"
    assert result["price"] is None
    assert result["status"] == "unknown"


def test_complete_target_next_data_preserves_precedence_over_jsonld():
    next_data = {"props": {"pageProps": {"item": {"url": USED, "name": "Preferred target", "price": 8000, "status": "on_sale"}}}}
    page = _page(_product(USED, price=1000, availability="OutOfStock"), text='<script id="__NEXT_DATA__" type="application/json">' + json.dumps(next_data) + '</script>')
    result = _parse_detail_page(page, USED)
    assert result["title"] == "Preferred target"
    assert result["price"] == 8000
    assert result["status"] == "on_sale"
    assert result["_scrape_meta"]["strategy"] == "next_data"


def test_partial_target_sources_combine_verified_stock_and_price(monkeypatch):
    next_data = {"props": {"pageProps": {"item": {"url": USED, "name": "Target", "status": "on_sale"}}}}
    product = _product(USED)
    product["offers"].pop("availability")
    page = _page(product, text='<script id="__NEXT_DATA__" type="application/json">' + json.dumps(next_data) + '</script>')
    monkeypatch.setattr("services.scraping_client.fetch_marketplace_static", lambda *a, **k: page)
    result = SnkrdunkPatrol().fetch(USED)
    assert result.success
    assert result.price == 6500
    assert result.status == "active"
