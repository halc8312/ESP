"""Observed apparel Flight contracts; all tests are offline.

The fixtures retain only the parser-relevant structure of two browser captures.
Product names, image URLs and variant IDs are anonymized; ads, tracking, brand
descriptions and unrelated page content are omitted. See the provenance file.
"""
import json
from pathlib import Path

import pytest

from services.html_page_adapter import HtmlPageAdapter
from services.patrol.snkrdunk_patrol import SnkrdunkPatrol
from snkrdunk_db import _iter_keyed_next_flight_records, _parse_detail_page


FIXTURES = Path(__file__).parent / "fixtures" / "html"
CASES = [
    ("300058", "snkrdunk_apparel_inline_flight.html", 48700, 89),
    ("721913", "snkrdunk_apparel_referenced_flight.html", 12777, 750),
]


def _capture(pid="721913"):
    filename = next(case[1] for case in CASES if case[0] == pid)
    return HtmlPageAdapter(
        (FIXTURES / filename).read_text(), url=f"https://snkrdunk.com/apparels/{pid}"
    )


def _payloads(pid="721913"):
    return dict(_iter_keyed_next_flight_records(_capture(pid)))


def _page(records, *, pid="721913", canonical=None, extra=""):
    canonical = canonical if canonical is not None else f"https://snkrdunk.com/apparels/{pid}"
    link = f'<link rel="canonical" href="{canonical}">' if canonical else ""
    lines = "\n".join(key + ":" + json.dumps(record, ensure_ascii=False) for key, record in records.items())
    script = "self.__next_f.push(" + json.dumps([1, lines], ensure_ascii=False) + ")"
    return HtmlPageAdapter(
        f"<html><head>{link}</head><body><script>{script}</script>{extra}</body></html>",
        url=f"https://snkrdunk.com/apparels/{pid}",
    )


def _parse(records, **kwargs):
    page = _page(records, **kwargs)
    return _parse_detail_page(page, page.url)


@pytest.mark.parametrize("pid,filename,price,count", CASES)
def test_minimized_observed_captures_extract_target_new_single_price(pid, filename, price, count):
    page = _capture(pid)
    result = _parse_detail_page(page, page.url)
    assert result["title"] == f"Captured apparel {pid}"
    assert result["price"] == price
    assert result["status"] == "on_sale"
    assert result["image_urls"] == ["https://cdn.snkrdunk.com/fixture-apparel.webp"]
    assert result["description"] == "Anonymous target description"
    assert result["_scrape_meta"]["field_sources"]["price"] == "app_router"
    # The capture's count is a number of listings, never an inventory quantity.
    listings = next(record[3]["listings"] for record in _payloads(pid).values() if "listings" in record[3])
    assert listings[0]["newListingItemCount"] == count


@pytest.mark.parametrize("pid,filename,price,count", CASES)
def test_patrol_uses_availability_boolean_not_number_of_listings(monkeypatch, pid, filename, price, count):
    page = _capture(pid)
    monkeypatch.setattr("services.scraping_client.fetch_marketplace_static", lambda *args, **kwargs: page)
    result = SnkrdunkPatrol().fetch(page.url)
    assert result.success and result.status == "active" and result.price == price
    assert result.variants == [{"name": "Default Title", "stock": 1, "price": price}]


def test_single_item_price_is_not_parent_minimum_bundle_bid_or_discount_price():
    records = _payloads()
    apparel = records["4e"][3]["apparelData"]
    apparel.update(minPrice=2, minPriceOfNewListing=3, usedMinPrice=4, regularPrice=5)
    listings = records["51"][3]["listings"]
    listings[0].update(maxOfferPrice=1, minUsedListingPrice=6, minNewListingCouponDiscountedPrice=7)
    listings[1]["minNewListingPrice"] = 8
    # Every apparel.sizes.quantity is 1, including the misleading 2個 entry.
    assert all(size["quantity"] == 1 for size in apparel["sizes"])
    listings.reverse()
    result = _parse(records, extra="おすすめ ¥1 購入する SOLD OUT")
    assert result["price"] == 12777 and result["status"] == "on_sale"


@pytest.mark.parametrize("key,value", [("filterSizeID", "quantity_2"), ("sizeName", "2個")])
def test_both_single_quantity_identifiers_are_required(key, value):
    records = _payloads()
    records["51"][3]["listings"][0]["variant"][key] = value
    result = _parse(records)
    assert result["price"] is None and result["status"] == "unknown"


def test_duplicate_single_variant_is_ambiguous():
    records = _payloads()
    records["51"][3]["listings"].append(records["51"][3]["listings"][0].copy())
    assert _parse(records)["status"] == "unknown"


@pytest.mark.parametrize("count", [None, 0, -1, True, 0.5, "99+", "750"])
def test_missing_or_invalid_single_stock_never_infers_sold_or_uses_other_bundles(count):
    records = _payloads()
    records["51"][3]["listings"][0]["newListingItemCount"] = count
    result = _parse(records, extra="購入する 売り切れ ¥12777")
    assert result["title"]
    assert result["price"] is None and result["status"] == "unknown"


@pytest.mark.parametrize("price", [None, 0, -1, True, 12777.5, "12777"])
def test_invalid_new_single_sell_price_never_falls_back_to_bid_or_parent_price(price):
    records = _payloads()
    records["51"][3]["listings"][0]["minNewListingPrice"] = price
    result = _parse(records)
    assert result["price"] is None and result["status"] == "unknown"


@pytest.mark.parametrize("parent_count", [None, 0, 749, True, "1568"])
def test_parent_listing_count_must_confirm_single_count(parent_count):
    records = _payloads()
    records["4e"][3]["apparelData"]["listingCount"] = parent_count
    assert _parse(records)["status"] == "unknown"


def test_no_single_variant_is_not_replaced_by_available_two_item_bundle():
    records = _payloads()
    del records["51"][3]["listings"][0]
    result = _parse(records)
    assert result["price"] is None and result["status"] == "unknown"


def test_zero_new_stock_with_used_stock_and_buy_offers_stays_unknown():
    records = _payloads()
    records["51"][3]["listings"][0].update(newListingItemCount=0, usedListingItemCount=9, offeringItemCount=30)
    records["4e"][3]["apparelData"].update(listingCount=0, usedListingCount=9)
    assert _parse(records)["status"] == "unknown"


@pytest.mark.parametrize("canonical", ["", "https://snkrdunk.com/apparels/300058", "https://evil.example/apparels/721913", "https://snkrdunk.com/apparels/721913/used/123"])
def test_target_parent_canonical_is_required(canonical):
    result = _parse(_payloads(), canonical=canonical)
    assert not result["title"] and result["price"] is None and result["status"] == "unknown"


@pytest.mark.parametrize("record,key", [("51", "apparelId"), ("4e", "apparelId"), ("4e", "id")])
def test_wrapper_reference_and_product_id_all_bind_to_requested_parent(record, key):
    records = _payloads()
    node = records[record][3]["apparelData"] if key == "id" else records[record][3]
    node[key] = 300058
    result = _parse(records)
    assert not result["title"] and result["price"] is None


@pytest.mark.parametrize("reference", ["$ff:props:apparelData", "$4e", "$4e:props:apparelData:listingCount", "$51:props:apparelData", "$4e:__proto__:apparelData"])
def test_unobserved_missing_recursive_or_arbitrary_reference_paths_are_rejected(reference):
    records = _payloads()
    records["51"][3]["apparelData"] = reference
    assert not _parse(records)["title"]


def test_unrelated_recommendation_cannot_supply_target_price_or_name():
    records = _payloads()
    records["51"][3]["apparelId"] = 300058
    records["4e"][3]["apparelData"]["id"] = 300058
    records["4e"][3]["apparelId"] = 300058
    result = _parse(records, extra="Captured apparel 721913 おすすめ購入 ¥1")
    assert not result["title"] and result["price"] is None and result["status"] == "unknown"


def test_conflicting_target_contexts_are_rejected_instead_of_taking_first():
    records = _payloads()
    duplicate = json.loads(json.dumps(records["51"]))
    duplicate[3]["listings"][0]["minNewListingPrice"] = 1
    records["52"] = duplicate
    assert not _parse(records)["title"]


def test_identical_target_contexts_are_safe_duplicates():
    records = _payloads()
    records["52"] = records["51"]
    assert _parse(records)["price"] == 12777


def test_parent_flight_never_repairs_or_overwrites_used_listing():
    page = _capture()
    result = _parse_detail_page(page, page.url + "/used/123")
    assert not result["title"] and result["price"] is None and result["status"] == "unknown"


def test_verified_legacy_jsonld_still_has_precedence():
    product = {
        "@type": "Product", "url": "https://snkrdunk.com/apparels/721913",
        "name": "Existing target source", "offers": {
            "@type": "Offer", "price": 24000, "availability": "https://schema.org/OutOfStock",
        },
    }
    result = _parse(_payloads(), extra='<script type="application/ld+json">' + json.dumps(product) + "</script>")
    assert result["title"] == "Existing target source"
    assert result["price"] == 24000 and result["status"] == "sold"


def test_requested_parent_query_does_not_change_product_identity():
    page = _capture()
    result = _parse_detail_page(page, page.url + "/?slide=right")
    assert result["price"] == 12777 and result["status"] == "on_sale"
