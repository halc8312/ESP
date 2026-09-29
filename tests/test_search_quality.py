"""Search quantity must describe unique acquisition before user filtering."""

import pytest

from services.search_result_quality import (
    build_search_quality,
    deduplicate_search_items,
    inspect_search_quality,
    search_item_identity,
)


def _item(catalog_id=1001, **overrides):
    return {
        "title": "Fixture record",
        "url": f"https://www.recordcity.jp/ja/catalog/{catalog_id}",
        "price": 1000,
        "status": "on_sale",
        **overrides,
    }


def test_recordcity_identity_preserves_first_occurrence_and_distinct_titles():
    first = _item(1001)
    second = _item(1002)  # A shared title does not identify the same product.
    duplicate = _item(
        1001,
        url="https://recordcity.jp/en/catalog/1001/?tracking=fixture#details",
        title="Later title must not replace the first record",
        price=2000,
    )
    third = _item(1003)
    items = [first, second, duplicate, third]

    unique, duplicate_count = deduplicate_search_items(items, site="recordcity")

    assert unique == [first, second, third]
    assert unique[0] is first
    assert duplicate_count == 1
    assert items == [first, second, duplicate, third]


@pytest.mark.parametrize(
    "alias",
    [
        "https://www.recordcity.jp/ja/catalog/1001/",
        "https://www.recordcity.jp/en/catalog/1001",
        "https://recordcity.jp/catalog/1001",
        "https://recordcity.jp/en/catalog/1001?condition=new#item",
    ],
)
def test_recordcity_catalog_id_is_shared_across_url_spellings(alias):
    first = _item()
    unique, duplicate_count = deduplicate_search_items(
        [first, _item(url=alias)], site="recordcity"
    )
    assert unique == [first]
    assert duplicate_count == 1


def test_other_supported_site_ignores_query_fragment_and_trailing_slash():
    first = _item(url="https://jp.mercari.com/item/m12345678901")
    duplicate = _item(
        url="https://jp.mercari.com/item/m12345678901/?source=search#details"
    )
    distinct = _item(url="https://jp.mercari.com/item/m12345678902")

    unique, duplicate_count = deduplicate_search_items(
        [first, duplicate, distinct], site="mercari"
    )

    assert unique == [first, distinct]
    assert duplicate_count == 1


@pytest.mark.parametrize(
    "site,host,path",
    [
        ("snkrdunk", "snkrdunk.com", "/products/DD1391-100"),
        ("snkrdunk", "snkrdunk.com", "/apparels/123"),
        ("snkrdunk", "snkrdunk.com", "/apparels/123/used/456"),
        ("surugaya", "suruga-ya.jp", "/product/detail/123456789"),
    ],
)
@pytest.mark.parametrize("first_prefix", ["", "www."])
def test_known_host_aliases_preserve_the_first_item(site, host, path, first_prefix):
    second_prefix = "" if first_prefix else "www."
    first = _item(url=f"https://{first_prefix}{host}{path}", title="First title")
    duplicate = _item(
        url=f"https://{second_prefix}{host}{path}/?source=listing#details",
        title="Later title",
        price=2000,
    )

    unique, duplicate_count = deduplicate_search_items([first, duplicate], site=site)

    assert unique == [first]
    assert unique[0] is first
    assert duplicate_count == 1
    assert search_item_identity(first) == search_item_identity(duplicate)


def test_snkrdunk_parent_and_individual_used_listings_remain_distinct():
    paths = [
        "/apparels/123",
        "/apparels/123/used/456",
        "/apparels/123/used/457",
        "/apparels/124/used/456",
        "/products/123",
    ]
    originals = [_item(url=f"https://snkrdunk.com{path}") for path in paths]
    alias = _item(url="https://www.snkrdunk.com/apparels/123/used/456/?source=search")

    unique, duplicate_count = deduplicate_search_items(
        [*originals[:2], alias, *originals[2:]], site="snkrdunk"
    )

    assert unique == originals
    assert duplicate_count == 1


def test_surugaya_product_ids_remain_distinct_across_host_aliases():
    first = _item(url="https://suruga-ya.jp/product/detail/123456789")
    second = _item(url="https://www.suruga-ya.jp/product/detail/987654321")
    alias = _item(url="https://www.suruga-ya.jp/product/detail/123456789/?source=search")

    unique, duplicate_count = deduplicate_search_items(
        [first, second, alias], site="surugaya"
    )

    assert unique == [first, second]
    assert duplicate_count == 1


@pytest.mark.parametrize(
    "site,host,path",
    [
        ("snkrdunk", "snkrdunk.com", "/apparels/123/used/456"),
        ("surugaya", "suruga-ya.jp", "/product/detail/123456789"),
    ],
)
@pytest.mark.parametrize("invalid_host", ["www.{host}.evil.example", "store.{host}"])
def test_host_aliases_do_not_accept_lookalikes_or_other_subdomains(site, host, path, invalid_host):
    valid = _item(url=f"https://{host}{path}")
    invalid = _item(url=f"https://{invalid_host.format(host=host)}{path}")

    unique, duplicate_count = deduplicate_search_items([valid, invalid], site=site)

    assert unique == [valid, invalid]
    assert duplicate_count == 0
    assert search_item_identity(invalid, site=site) is None


def test_other_sites_do_not_globally_strip_www_from_invalid_product_hosts():
    valid = _item(url="https://jp.mercari.com/item/m12345678901")
    invalid = _item(url="https://www.jp.mercari.com/item/m12345678901")

    unique, duplicate_count = deduplicate_search_items([valid, invalid], site="mercari")

    assert unique == [valid, invalid]
    assert duplicate_count == 0
    assert search_item_identity(invalid, site="mercari") is None


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "not a URL",
        "https://www.recordcity.jp/ja/catalog",
        "https://www.recordcity.jp/ja/catalog/not-a-product-id",
        "https://untrusted.example/ja/catalog/1001",
        "https://www.recordcity.jp.untrusted.example/ja/catalog/1001",
    ],
)
def test_missing_or_invalid_product_urls_do_not_collapse_unrelated_items(url):
    first = _item(url=url, title="First unidentifiable item")
    second = _item(url=url, title="Second unidentifiable item")

    unique, duplicate_count = deduplicate_search_items(
        [first, second], site="recordcity"
    )

    assert unique == [first, second]
    assert duplicate_count == 0


def test_a_spoofed_catalog_host_cannot_merge_with_a_valid_product():
    valid = _item()
    spoofed = _item(url="https://www.recordcity.jp.untrusted.example/ja/catalog/1001")
    unique, duplicate_count = deduplicate_search_items(
        [valid, spoofed], site="recordcity"
    )
    assert unique == [valid, spoofed]
    assert duplicate_count == 0


def test_duplicate_quality_preserves_first_data_instead_of_selecting_success():
    invalid_first = _item(price=None)
    valid_later = _item(url="https://recordcity.jp/en/catalog/1001")
    unique, duplicates = deduplicate_search_items(
        [invalid_first, valid_later], site="recordcity"
    )
    quality = build_search_quality(unique, requested_count=1, duplicate_count=duplicates)

    observed = inspect_search_quality(unique, quality)

    assert unique == [invalid_first]
    assert quality["duplicate_count"] == 1
    assert observed == {
        "outcome": "failure",
        "reason": "missing_price",
        "success_count": 0,
        "error_count": 1,
    }


def test_duplicate_results_are_not_counted_toward_requested_quantity():
    unique, duplicates = deduplicate_search_items(
        [_item(), _item(url="https://recordcity.jp/catalog/1001/")],
        site="recordcity",
    )
    quality = build_search_quality(unique, requested_count=10, duplicate_count=duplicates)

    assert quality["requested_count"] == 10
    assert quality["unique_count"] == 1
    assert quality["duplicate_count"] == 1
    assert quality["acquisition_rate"] == pytest.approx(0.1)
    assert quality["end_reason"] == "unknown"
    observed = inspect_search_quality(unique, quality)
    assert observed["outcome"] == "failure"
    assert observed["reason"] == "incomplete_results"
    assert observed["success_count"] == 1
    assert observed["error_count"] >= 1


def test_user_exclusions_do_not_change_acquisition_denominator_or_health():
    items = [_item(1001), _item(1002)]
    quality = build_search_quality(
        items,
        requested_count=2,
        progress={"requested_count": 150, "candidate_count": 150},
        excluded_count=2,
        displayed_count=0,
    )

    assert quality["requested_count"] == 2
    assert quality["unique_count"] == 2
    assert quality["excluded_count"] == 2
    assert quality["displayed_count"] == 0
    assert quality["acquisition_rate"] == 1.0
    assert quality["end_reason"] == "requested_reached"
    assert inspect_search_quality(items, quality) == {
        "outcome": "success",
        "reason": None,
        "success_count": 2,
        "error_count": 0,
    }


def test_acquisition_rate_is_capped_when_supplemental_results_exceed_request():
    quality = build_search_quality([_item(1001), _item(1002)], requested_count=1)
    assert quality["unique_count"] == 2
    assert quality["displayed_count"] == 2
    assert quality["acquisition_rate"] == 1.0
    assert quality["end_reason"] == "requested_reached"


@pytest.mark.parametrize(
    "end_reason", ["page_limit", "candidate_limit", "pagination_loop", "unknown"]
)
def test_short_results_with_unproven_completion_do_not_report_success(end_reason):
    items = [_item()]
    quality = build_search_quality(
        items, requested_count=10, progress={"end_reason": end_reason}
    )
    assert quality["end_reason"] == end_reason
    observed = inspect_search_quality(items, quality)
    assert observed["outcome"] == "failure"
    assert observed["reason"] == "incomplete_results"
    assert observed["success_count"] == 1
    assert observed["error_count"] >= 1


def test_confirmed_listing_end_allows_a_legitimate_short_result():
    items = [_item(1001), _item(1002)]
    quality = build_search_quality(
        items, requested_count=10, progress={"end_reason": "listing_exhausted"}
    )
    assert quality["end_reason"] == "listing_exhausted"
    assert quality["acquisition_rate"] == pytest.approx(0.2)
    assert inspect_search_quality(items, quality) == {
        "outcome": "success",
        "reason": None,
        "success_count": 2,
        "error_count": 0,
    }


def test_explicit_empty_is_not_a_successful_recovery_or_a_site_failure():
    quality = build_search_quality(
        [], requested_count=10, progress={"end_reason": "explicit_empty"}
    )
    assert quality["acquisition_rate"] == 0.0
    assert inspect_search_quality([], quality) == {
        "outcome": "no_observations",
        "reason": "empty_result",
        "success_count": 0,
        "error_count": 0,
    }


def test_empty_without_completion_evidence_is_incomplete():
    quality = build_search_quality([], requested_count=10)
    observed = inspect_search_quality([], quality)
    assert observed["outcome"] == "failure"
    assert observed["reason"] == "incomplete_results"
    assert observed["success_count"] == 0
    assert observed["error_count"] >= 1


def test_detail_failures_prevent_a_short_listing_from_being_healthy():
    items = [_item()]
    quality = build_search_quality(
        items,
        requested_count=10,
        progress={"end_reason": "listing_exhausted", "detail_error_count": 2},
    )
    assert quality["detail_error_count"] == 2
    observed = inspect_search_quality(items, quality)
    assert observed["outcome"] == "failure"
    assert observed["success_count"] == 1
    assert observed["error_count"] >= 2


def test_enough_results_still_observe_invalid_individual_products():
    items = [_item(1001), _item(1002, status="unknown")]
    quality = build_search_quality(items, requested_count=2)
    assert quality["end_reason"] == "unknown"
    assert quality["unique_count"] == 2
    assert quality["valid_count"] == 1
    assert quality["acquisition_rate"] == .5
    assert inspect_search_quality(items, quality) == {
        "outcome": "failure",
        "reason": "unknown_status",
        "success_count": 1,
        "error_count": 1,
    }


def test_hostile_progress_cannot_override_counts_or_leak_source_information():
    quality = build_search_quality(
        [_item()],
        requested_count=10,
        duplicate_count=1,
        progress={
            "end_reason": "https://private.example/?token=secret-value",
            "requested_count": 1,
            "unique_count": 999,
            "duplicate_count": 0,
            "acquisition_rate": 1.0,
            "detail_error_count": "https://private.example/?token=secret-value",
            "source_url": "https://private.example/?token=secret-value",
            "keyword": "private search phrase",
            "message": "private detail failure",
            "items": [{"url": "https://private.example/"}],
        },
    )

    assert quality["requested_count"] == 10
    assert quality["unique_count"] == 1
    assert quality["duplicate_count"] == 1
    assert quality["acquisition_rate"] == pytest.approx(0.1)
    assert quality["end_reason"] == "unknown"
    assert "source_url" not in quality
    assert "keyword" not in quality
    assert "message" not in quality
    assert "items" not in quality
    assert "secret-value" not in repr(quality)
    assert "private" not in repr(quality)
    assert all(
        isinstance(value, (int, float))
        for key, value in quality.items()
        if key not in {"end_reason", "candidates_count", "pages_fetched"}
    )
    assert quality["candidates_count"] is quality["pages_fetched"] is None


def test_claimed_requested_reached_cannot_hide_actual_shortage():
    quality = build_search_quality(
        [_item()], requested_count=10, progress={"end_reason": "requested_reached"}
    )
    assert quality["end_reason"] == "unknown"
    assert inspect_search_quality([_item()], quality)["outcome"] == "failure"
