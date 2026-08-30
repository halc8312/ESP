"""
Reading Record City (recordcity.jp).

The site publishes schema.org Product JSON-LD on every catalog page, so the
reader takes its facts from there. The sample below is the shape the client
sent from a live page.

Both page kinds sit behind an AWS WAF challenge — a plain HTTP request gets a
JavaScript puzzle instead of the page — so the fetches go through the browser.
That part cannot be exercised here; these tests cover the parsing, the URL
rules and the item-count ceiling, and the fetch itself is stubbed.
"""
import json

import pytest

import recordcity_db
from services import recordcity_browser_fetch
from services.scrape_request import InvalidTargetUrl, classify_target_url
from services.scrape_safety import (
    ScrapeFailure,
    UnsafeScrapeUrlError,
    validate_marketplace_url,
)


LIVE_SAMPLE = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Sunrise / Son of Pin Head (完全生産限定盤)",
    "image": ["https://files.recordcity.jp/public/images/masters/original/M10231435.JPG"],
    "sku": 4936480,
    "brand": {"@type": "Brand", "name": "VICTOR"},
    "offers": {
        "price": 2420,
        "priceCurrency": "JPY",
        "availability": "https://schema.org/InStock",
        "itemCondition": "https://schema.org/NewCondition",
    },
}


class _FakeElement:
    def __init__(self, text="", attrib=None):
        self.text = text
        self.attrib = attrib or {}


class _FakePage:
    """Stands in for the fetched page object, which exposes .css()."""

    def __init__(self, *, json_ld=None, anchors=(), text=""):
        self._scripts = [
            _FakeElement(text=json.dumps(block, ensure_ascii=False))
            for block in (json_ld or [])
        ]
        self._anchors = list(anchors)
        self._text = text

    def css(self, selector):
        if "ld+json" in selector:
            return self._scripts
        if selector.startswith("a["):
            return self._anchors
        return []

    def get_all_text(self):
        return self._text


def _product_page(product=None):
    return _FakePage(json_ld=[product if product is not None else LIVE_SAMPLE])


def _stub_fetch(monkeypatch, pages):
    """Serve prepared pages in place of the browser fetch."""
    calls = []

    def _fake(url, kind):
        calls.append((url, kind))
        page = pages(url) if callable(pages) else pages
        if page is None:
            raise AssertionError(f"no page prepared for {url}")
        return page

    monkeypatch.setattr(recordcity_db, "_fetch_page", _fake)
    return calls


class TestUrlRules:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.recordcity.jp/catalog/4936480",
            "https://www.recordcity.jp/catalog/4936480/",
            "https://recordcity.jp/catalog/4936480",
            # The site redirects the plain form to a language-prefixed one.
            "https://www.recordcity.jp/ja/catalog/4936480",
        ],
    )
    def test_a_product_url_is_recognised(self, url):
        assert classify_target_url(url) == ("item", "recordcity")

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.recordcity.jp/catalog?narrow_down_3=3",
            "https://www.recordcity.jp/catalog",
            "https://www.recordcity.jp/catalog?narrow_down_3=3&page=2",
        ],
    )
    def test_a_listing_url_is_recognised(self, url):
        assert classify_target_url(url) == ("search", "recordcity")

    @pytest.mark.parametrize(
        "url",
        [
            "https://recordcity.jp.evil.example/catalog/1",
            "https://www.recordcity.jp.attacker.test/catalog/1",
            "http://www.recordcity.jp/catalog/4936480",
            "https://www.recordcity.jp:8443/catalog/4936480",
        ],
    )
    def test_a_lookalike_or_unsafe_url_is_refused(self, url):
        with pytest.raises((InvalidTargetUrl, UnsafeScrapeUrlError)):
            classify_target_url(url)

    def test_a_catalog_path_is_not_mistaken_for_a_product(self):
        # Only digits name a record; anything else is a listing.
        with pytest.raises(UnsafeScrapeUrlError):
            validate_marketplace_url(
                "https://www.recordcity.jp/catalog/new-arrivals",
                "recordcity",
                kind="detail",
            )


def test_fetch_page_uses_recordcity_browser_adapter_and_wait_contract(monkeypatch):
    captured = {}
    page = _product_page()
    detail_url = "https://www.recordcity.jp/catalog/4936480"

    def fake_fetch(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return page

    monkeypatch.setattr(
        recordcity_browser_fetch,
        "fetch_recordcity_page_via_browser_pool_sync",
        fake_fetch,
    )

    result = recordcity_db._fetch_page(detail_url, "detail")

    assert result is page
    assert captured == {
        "url": detail_url,
        "kind": "detail",
        "network_idle": True,
        "timeout": 45000,
        "wait_selector": "script[type='application/ld+json']",
        "wait_selector_timeout": 20000,
    }


class TestReadingOneRecord:
    def test_every_field_the_page_publishes_is_kept(self, monkeypatch):
        _stub_fetch(monkeypatch, _product_page())

        result = recordcity_db.scrape_item_detail(
            "https://www.recordcity.jp/catalog/4936480"
        )

        assert result["title"] == "Sunrise / Son of Pin Head (完全生産限定盤)"
        assert result["price"] == 2420
        assert result["status"] == "on_sale"
        assert result["brand"] == "VICTOR"
        assert result["sku"] == "4936480"
        assert result["condition"] == "NewCondition"
        assert result["image_urls"] == [
            "https://files.recordcity.jp/public/images/masters/original/M10231435.JPG"
        ]

    @pytest.mark.parametrize(
        "availability,expected",
        [
            ("https://schema.org/InStock", "on_sale"),
            ("https://schema.org/OutOfStock", "sold_out"),
            ("https://schema.org/SoldOut", "sold_out"),
            ("https://schema.org/PreOrder", "on_sale"),
            ("https://schema.org/Discontinued", "sold_out"),
            ("", "unknown"),
        ],
    )
    def test_availability_decides_the_status(self, monkeypatch, availability, expected):
        product = json.loads(json.dumps(LIVE_SAMPLE))
        product["offers"]["availability"] = availability
        _stub_fetch(monkeypatch, _product_page(product))

        result = recordcity_db.scrape_item_detail(
            "https://www.recordcity.jp/catalog/4936480"
        )

        assert result["status"] == expected

    def test_a_price_in_another_currency_is_not_stored_as_yen(self, monkeypatch):
        product = json.loads(json.dumps(LIVE_SAMPLE))
        product["offers"]["price"] = 19.99
        product["offers"]["priceCurrency"] = "USD"
        _stub_fetch(monkeypatch, _product_page(product))

        result = recordcity_db.scrape_item_detail(
            "https://www.recordcity.jp/catalog/4936480"
        )

        # Saving 19 yen for a $19.99 record would be worse than saving nothing.
        assert result["price"] is None
        assert result["title"] == LIVE_SAMPLE["name"]

    def test_the_condition_is_read_from_either_place(self, monkeypatch):
        # schema.org allows it on the product too, and other sites put it there.
        product = json.loads(json.dumps(LIVE_SAMPLE))
        del product["offers"]["itemCondition"]
        product["itemCondition"] = "https://schema.org/UsedCondition"
        _stub_fetch(monkeypatch, _product_page(product))

        result = recordcity_db.scrape_item_detail(
            "https://www.recordcity.jp/catalog/4936480"
        )

        assert result["condition"] == "UsedCondition"

    def test_the_product_is_found_inside_a_graph(self, monkeypatch):
        wrapped = {
            "@context": "https://schema.org",
            "@graph": [{"@type": "BreadcrumbList"}, LIVE_SAMPLE],
        }
        _stub_fetch(monkeypatch, _FakePage(json_ld=[wrapped]))

        result = recordcity_db.scrape_item_detail(
            "https://www.recordcity.jp/catalog/4936480"
        )

        assert result["price"] == 2420

    def test_a_page_without_product_data_says_so(self, monkeypatch):
        # What the WAF challenge page looks like: no structured data at all.
        _stub_fetch(monkeypatch, _FakePage(json_ld=[], text="challenge"))

        with pytest.raises(ScrapeFailure) as failure:
            recordcity_db.scrape_item_detail(
                "https://www.recordcity.jp/catalog/4936480"
            )

        # "Could not be read" alone leaves the operator nothing to act on.
        assert "ボット判定" in str(failure.value)

    def test_unparsable_structured_data_names_the_parse_error(self, monkeypatch):
        broken = _FakePage()
        broken._scripts = [_FakeElement(text="{not json")]
        _stub_fetch(monkeypatch, broken)

        with pytest.raises(ScrapeFailure) as failure:
            recordcity_db.scrape_item_detail(
                "https://www.recordcity.jp/catalog/4936480"
            )

        message = str(failure.value)
        # Data present but unreadable is a different problem from data absent,
        # and blaming the bot challenge here would send everyone looking in
        # the wrong place.
        assert "構造化データ" in message
        assert "ボット判定" not in message

    def test_a_second_readable_block_still_wins(self, monkeypatch):
        # One malformed block should not hide a good one beside it.
        page = _FakePage(json_ld=[LIVE_SAMPLE])
        page._scripts = [_FakeElement(text="{not json")] + page._scripts
        _stub_fetch(monkeypatch, page)

        result = recordcity_db.scrape_item_detail(
            "https://www.recordcity.jp/catalog/4936480"
        )

        assert result["price"] == 2420

    def test_the_reason_a_fetch_failed_travels_with_the_failure(self, monkeypatch):
        def _explode(url, kind):
            raise RuntimeError("browser pool exhausted")

        monkeypatch.setattr(recordcity_db, "_fetch_page", _explode)

        with pytest.raises(ScrapeFailure) as failure:
            recordcity_db.scrape_item_detail(
                "https://www.recordcity.jp/catalog/4936480"
            )

        assert "browser pool exhausted" in str(failure.value)


def _listing_page(count, *, start=1):
    anchors = [
        _FakeElement(attrib={"href": f"/catalog/{start + index}"}) for index in range(count)
    ]
    return _FakePage(anchors=anchors, text=f"{count}件")


class TestReadingAListing:
    def test_only_the_requested_number_of_records_is_opened(self, monkeypatch):
        listing = _listing_page(100)

        def _pages(url):
            return listing if "?" in url or url.endswith("/catalog") else _product_page()

        calls = _stub_fetch(monkeypatch, _pages)

        results = recordcity_db.scrape_search_result(
            "https://www.recordcity.jp/catalog?narrow_down_3=3", max_items=5
        )

        assert len(results) == 5
        # A category can hold six figures of records; the count asked for is
        # what bounds the crawl, not the size of the listing.
        detail_calls = [url for url, kind in calls if kind == "detail"]
        assert len(detail_calls) == 5

    def test_links_off_the_site_are_not_followed(self, monkeypatch):
        listing = _FakePage(
            anchors=[
                _FakeElement(attrib={"href": "https://evil.example/catalog/1"}),
                _FakeElement(attrib={"href": "https://www.recordcity.jp.evil.test/catalog/2"}),
                _FakeElement(attrib={"href": "/catalog/4936480"}),
            ],
            text="1件",
        )

        def _pages(url):
            return listing if "?" in url else _product_page()

        calls = _stub_fetch(monkeypatch, _pages)

        results = recordcity_db.scrape_search_result(
            "https://www.recordcity.jp/catalog?narrow_down_3=3", max_items=10
        )

        assert len(results) == 1
        detail_calls = [url for url, kind in calls if kind == "detail"]
        assert detail_calls == ["https://www.recordcity.jp/catalog/4936480"]

    def test_one_unreadable_record_does_not_lose_the_rest(self, monkeypatch):
        # Detail failures now raise rather than return an empty result, so the
        # listing has to keep going past one of them.
        listing = _FakePage(
            anchors=[
                _FakeElement(attrib={"href": "/catalog/1"}),
                _FakeElement(attrib={"href": "/catalog/2"}),
            ],
            text="2件",
        )

        def _pages(url):
            if "?" in url:
                return listing
            if url.endswith("/catalog/1"):
                return _FakePage(json_ld=[], text="challenge")
            return _product_page()

        _stub_fetch(monkeypatch, _pages)

        results = recordcity_db.scrape_search_result(
            "https://www.recordcity.jp/catalog?narrow_down_3=3", max_items=10
        )

        assert len(results) == 1
        assert results[0]["price"] == 2420

    def test_the_same_record_listed_twice_is_read_once(self, monkeypatch):
        listing = _FakePage(
            anchors=[
                _FakeElement(attrib={"href": "/catalog/4936480"}),
                _FakeElement(attrib={"href": "/catalog/4936480"}),
                _FakeElement(attrib={"href": "/catalog/4936481"}),
            ],
            text="2件",
        )

        def _pages(url):
            return listing if "?" in url else _product_page()

        calls = _stub_fetch(monkeypatch, _pages)

        recordcity_db.scrape_search_result(
            "https://www.recordcity.jp/catalog?narrow_down_3=3", max_items=10
        )

        detail_calls = [url for url, kind in calls if kind == "detail"]
        assert len(detail_calls) == len(set(detail_calls)) == 2
