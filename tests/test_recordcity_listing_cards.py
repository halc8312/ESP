"""Listing-card contract tests; these do not claim a verified live DOM shape."""
from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from services.listing_cards import validate_listing_card
from services.html_page_adapter import HtmlPageAdapter
from services.scrape_safety import ScrapeBlockedError, ScrapeFailure, ScrapeSelectorDriftError
import recordcity_db


def card(**changes):
    result = {
        "_listing_card": True,
        "url": "https://www.recordcity.jp/ja/catalog/4936480",
        "source_id": "4936480",
        "title": "Sunrise / Son of Pin Head",
        "price": 2420,
        "currency": "JPY",
        "status": "unknown",
        "description": "",
        "image_urls": ["https://files.recordcity.jp/public/images/masters/original/M10231435.JPG"],
    }
    result.update(changes)
    return result


def test_card_validation_accepts_display_fields_without_inventing_stock():
    item = card()
    before = deepcopy(item)
    assert validate_listing_card(item, "recordcity") is True
    assert item == before
    assert item["status"] == "unknown"


@pytest.mark.parametrize("changes", [
    {"_listing_card": False}, {"_listing_card": 1},
    {"title": " "}, {"title": 42},
    {"price": 0}, {"price": -1}, {"price": True}, {"price": "2420"},
    {"price": 24.2}, {"price": float("nan")}, {"price": float("inf")},
    {"price": 2147483648},
    {"currency": "USD"}, {"currency": None},
    {"source_id": "111"}, {"source_id": None},
    {"url": "https://www.recordcity.jp/ja/catalog?search=4936480"},
    {"url": "https://www.recordcity.jp.evil.test/ja/catalog/4936480"},
    {"url": "https://snkrdunk.com/products/4936480"},
    {"status": "error"}, {"status": "blocked"}, {"status": None},
    {"description": "Unverified details"},
    {"image_urls": []}, {"image_urls": "https://files.recordcity.jp/a.jpg"},
    {"image_urls": ["http://files.recordcity.jp/a.jpg"]},
    {"image_urls": ["https://files.recordcity.jp.evil.test/a.jpg"]},
    {"image_urls": ["https://files.recordcity.jp@evil.test/a.jpg"]},
    {"image_urls": ["https://127.0.0.1/a.jpg"]},
    {"image_urls": ["https://files.recordcity.jp:8443/a.jpg"]},
    {"image_urls": ["https://files.recordcity.jp/a.jpg", "https://files.recordcity.jp/b.jpg"]},
])
def test_card_validation_fails_closed_on_incomplete_or_unsafe_data(changes):
    assert validate_listing_card(card(**changes), "recordcity") is False


@pytest.mark.parametrize("value", [None, [], "not a card", 1])
def test_non_mapping_is_not_a_card(value):
    assert validate_listing_card(value) is False


def test_card_validation_does_not_enable_other_sites():
    assert validate_listing_card(card(), "mercari") is False


@pytest.mark.parametrize("url", [
    "https://recordcity.jp/catalog/4936480/?tracking=1#cover",
    "https://www.recordcity.jp/catalog/4936480",
])
def test_card_identity_accepts_validated_recordcity_aliases(url):
    assert validate_listing_card(card(url=url)) is True


SEARCH = "https://www.recordcity.jp/ja/catalog?narrow_down_17=5000-11000&condition=new&sort=price"


def product(product_id, **changes):
    item = {
        "@type": "Product",
        "url": f"https://www.recordcity.jp/ja/catalog/{product_id}",
        "sku": str(product_id),
        "name": f"Record {product_id}",
        "offers": {"@type": "Offer", "price": "5,500", "priceCurrency": "JPY"},
        "image": "https://files.recordcity.jp/public/images/masters/original/M10231435.JPG",
    }
    item.update(changes)
    return item


def listing(products, *, next_url="", text="", list_type="ItemList"):
    data = {
        "@context": "https://schema.org",
        "@type": list_type,
        "itemListElement": [
            {"@type": "ListItem", "position": n, "item": item}
            for n, item in enumerate(products, 1)
        ],
    }
    html = f'<html><meta charset="utf-8"><script type="application/ld+json">{json.dumps(data)}</script>{text}'
    if next_url:
        html += f'<a rel="next" href="{next_url}">Next</a>'
    return HtmlPageAdapter(html + "</html>")


@pytest.fixture(autouse=True)
def forbid_live_access(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No live page or product detail requests in fixture tests")
    monkeypatch.setattr(recordcity_db, "_fetch_page", forbidden)
    monkeypatch.setattr(recordcity_db, "scrape_item_detail", forbidden)


def test_explicit_itemlist_fixture_extracts_cards_without_detail_fetch(monkeypatch):
    fixture = Path(__file__).parent / "fixtures/html/recordcity_itemlist_synthetic.html"
    page = HtmlPageAdapter(fixture.read_text(encoding="utf-8"))
    calls, progress = [], []
    def fetch(url, kind):
        calls.append((url, kind))
        return page
    monkeypatch.setattr(recordcity_db, "_fetch_page", fetch)
    results = recordcity_db.scrape_listing_result(SEARCH, max_items=2,
        progress_callback=lambda items, state: progress.append((items, state)))
    assert [item["source_id"] for item in results] == ["4936480", "4936481"]
    assert [item["status"] for item in results] == ["unknown", "on_sale"]
    assert all(validate_listing_card(item) for item in results)
    assert calls == [(SEARCH, "search")]
    assert progress[-1][1]["end_reason"] == "requested_reached"
    assert progress[-1][1]["detail_error_count"] == 0


def test_pages_preserve_filters_order_and_deduplicate_catalog_aliases(monkeypatch):
    calls, progress = [], []
    def fetch(url, kind):
        calls.append(url)
        if len(calls) == 1:
            return listing([product(1), product(2)], next_url="?page=2")
        assert parse_qs(urlparse(url).query) == {
            "narrow_down_17": ["5000-11000"], "condition": ["new"],
            "sort": ["price"], "page": ["2"],
        }
        return listing([product(2, url="https://recordcity.jp/catalog/2/?ref=dup"), product(3)])
    monkeypatch.setattr(recordcity_db, "_fetch_page", fetch)
    items = recordcity_db.scrape_listing_result(SEARCH, max_items=3,
        progress_callback=lambda items, state: progress.append((items, state)))
    assert [item["source_id"] for item in items] == ["1", "2", "3"]
    assert len(calls) == 2
    assert progress[-1][1]["duplicate_count"] == 1


@pytest.mark.parametrize("next_url", [
    "?condition=used&page=2", "?sort=recent&page=2", "?unexpected=1&page=2",
    "?page=2&page=3", "?page=bad", "/catalog?page=2",
])
def test_changed_filters_or_ambiguous_page_do_not_trigger_more_requests(monkeypatch, next_url):
    calls, checkpoints = [], []
    def fetch(url, kind):
        calls.append(url)
        return listing([product(1)], next_url=next_url)
    monkeypatch.setattr(recordcity_db, "_fetch_page", fetch)
    with pytest.raises(ScrapeSelectorDriftError):
        recordcity_db.scrape_listing_result(SEARCH, max_items=2,
            progress_callback=lambda items, state: checkpoints.append(items))
    assert calls == [SEARCH]
    assert checkpoints[-1][0]["source_id"] == "1"


def test_absent_next_link_does_not_prove_exhaustion(monkeypatch):
    monkeypatch.setattr(recordcity_db, "_fetch_page", lambda *args, **kwargs: listing([product(1)]))
    progress = []
    result = recordcity_db.scrape_listing_result(SEARCH, max_items=500,
        progress_callback=lambda items, state: progress.append(state))
    assert len(result) == 1
    assert progress[-1]["end_reason"] == "unknown"


def test_redirect_that_loses_filters_is_not_accepted(monkeypatch):
    page = listing([product(1)])
    page.url = "https://www.recordcity.jp/ja/catalog"
    monkeypatch.setattr(recordcity_db, "_fetch_page", lambda *args, **kwargs: page)
    with pytest.raises(ScrapeSelectorDriftError, match="検索条件"):
        recordcity_db.scrape_listing_result(SEARCH, max_items=1)


def test_empty_page_needs_explicit_evidence(monkeypatch):
    monkeypatch.setattr(recordcity_db, "_fetch_page", lambda *args, **kwargs: listing([], text="検索結果は0件です"))
    result = recordcity_db.scrape_listing_result(SEARCH)
    assert result == []
    assert result.end_reason == "explicit_empty"
    monkeypatch.setattr(recordcity_db, "_fetch_page", lambda *args, **kwargs: listing([]))
    with pytest.raises(ScrapeSelectorDriftError):
        recordcity_db.scrape_listing_result(SEARCH)


@pytest.mark.parametrize("text", ["検索結果100件", "検索結果10件", "検索結果1,000件", "カート 0件"])
def test_positive_result_count_is_never_misread_as_zero(monkeypatch, text):
    monkeypatch.setattr(recordcity_db, "_fetch_page",
        lambda *args, **kwargs: HtmlPageAdapter(f"<html>{text}</html>"))
    with pytest.raises(ScrapeSelectorDriftError):
        recordcity_db.scrape_listing_result(SEARCH)


def test_product_link_contradicts_unsupported_empty_page(monkeypatch):
    page = HtmlPageAdapter('<html>検索結果0件<a href="/ja/catalog/1">Record</a></html>')
    monkeypatch.setattr(recordcity_db, "_fetch_page", lambda *args, **kwargs: page)
    with pytest.raises(ScrapeSelectorDriftError):
        recordcity_db.scrape_listing_result(SEARCH)


def test_block_on_later_page_preserves_prior_checkpoint_and_stops(monkeypatch):
    calls, checkpoints = [], []
    def fetch(url, kind):
        calls.append(url)
        if len(calls) == 2:
            raise ScrapeBlockedError("blocked", status_code=403)
        return listing([product(1)], next_url="?page=2")
    monkeypatch.setattr(recordcity_db, "_fetch_page", fetch)
    with pytest.raises(ScrapeBlockedError):
        recordcity_db.scrape_listing_result(SEARCH, max_items=500,
            progress_callback=lambda items, state: checkpoints.append(items))
    assert len(calls) == 2
    assert checkpoints[-1][0]["source_id"] == "1"


def test_page_and_request_budget_caps_untrusted_pagination(monkeypatch):
    calls, progress = [], []
    def fetch(url, kind):
        calls.append(url)
        return listing([product(len(calls))], next_url=f"?page={len(calls) + 1}")
    monkeypatch.setattr(recordcity_db, "_fetch_page", fetch)
    result = recordcity_db.scrape_listing_result(SEARCH, max_items=500, max_pages=1000,
        progress_callback=lambda items, state: progress.append(state))
    assert len(result) == len(calls) == 10
    assert progress[-1]["end_reason"] == "page_limit"


def test_item_budget_caps_oversized_caller_limit(monkeypatch):
    monkeypatch.setattr(recordcity_db, "_fetch_page",
        lambda *args, **kwargs: listing([product(n) for n in range(1, 601)]))
    result = recordcity_db.scrape_listing_result(SEARCH, max_items=100000)
    assert len(result) == 500
    assert result[-1]["source_id"] == "500"


def test_repeated_page_is_not_refetched(monkeypatch):
    calls, progress = [], []
    def fetch(url, kind):
        calls.append(url)
        return listing([product(len(calls))], next_url="?page=2")
    monkeypatch.setattr(recordcity_db, "_fetch_page", fetch)
    recordcity_db.scrape_listing_result(SEARCH, max_items=10,
        progress_callback=lambda items, state: progress.append(state))
    assert len(calls) == 2
    assert progress[-1]["end_reason"] == "pagination_loop"


def test_elapsed_time_budget_stops_before_another_fetch(monkeypatch):
    times = iter([0, 0, 181])
    monkeypatch.setattr(recordcity_db.time, "monotonic", lambda: next(times))
    calls, checkpoints = [], []
    def fetch(url, kind):
        calls.append(url)
        return listing([product(1)], next_url="?page=2")
    monkeypatch.setattr(recordcity_db, "_fetch_page", fetch)
    with pytest.raises(ScrapeFailure, match="制限時間"):
        recordcity_db.scrape_listing_result(SEARCH, max_items=500,
            progress_callback=lambda items, state: checkpoints.append(items))
    assert len(calls) == 1
    assert checkpoints[-1][0]["source_id"] == "1"


@pytest.mark.parametrize("changes", [
    {"name": ""}, {"sku": "different"}, {"image": []},
    {"offers": {"price": 5000, "priceCurrency": "USD"}},
    {"offers": {"price": "from 5000", "priceCurrency": "JPY"}},
    {"offers": {"price": "5,50", "priceCurrency": "JPY"}},
    {"offers": {"price": 5000, "priceCurrency": "JPY", "@type": "AggregateOffer"}},
    {"offers": [{"price": 5000, "priceCurrency": "JPY"}, {"price": 6000, "priceCurrency": "JPY"}]},
])
def test_invalid_product_data_is_not_silently_successful_or_empty(monkeypatch, changes):
    monkeypatch.setattr(recordcity_db, "_fetch_page",
        lambda *args, **kwargs: listing([product(1, **changes)], text="検索結果は0件です"))
    with pytest.raises(ScrapeSelectorDriftError):
        recordcity_db.scrape_listing_result(SEARCH)


def test_mixed_invalid_cards_are_counted_without_hiding_valid_cards(monkeypatch):
    monkeypatch.setattr(recordcity_db, "_fetch_page",
        lambda *args, **kwargs: listing([product(1), product(2, name="")]))
    progress = []
    result = recordcity_db.scrape_listing_result(SEARCH, max_items=1,
        progress_callback=lambda items, state: progress.append(state))
    assert len(result) == 1
    assert progress[-1]["invalid_card_count"] == 1


def test_malformed_product_url_does_not_discard_other_cards(monkeypatch):
    monkeypatch.setattr(recordcity_db, "_fetch_page",
        lambda *args, **kwargs: listing([product(1), product(2, url="https://[broken")]))
    progress = []
    result = recordcity_db.scrape_listing_result(SEARCH, max_items=1,
        progress_callback=lambda items, state: progress.append((items, state)))
    assert len(result) == 1
    assert progress[-1][0][0]["source_id"] == "1"
    assert progress[-1][1]["invalid_card_count"] == 1


def test_unverified_dom_and_standalone_detail_product_fail_closed(monkeypatch):
    for html in [
        '<div><a href="/ja/catalog/1">Record one</a><img src="https://files.recordcity.jp/a.jpg">5,500円</div>',
        f'<script type="application/ld+json">{json.dumps(product(1))}</script>',
    ]:
        monkeypatch.setattr(recordcity_db, "_fetch_page", lambda *args, **kwargs: HtmlPageAdapter(html))
        with pytest.raises(ScrapeSelectorDriftError):
            recordcity_db.scrape_listing_result(SEARCH)
