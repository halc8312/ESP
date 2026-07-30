import json
from pathlib import Path

from services.html_page_adapter import HtmlPageAdapter

import snkrdunk_db


def test_parse_snkrdunk_detail_dump_fixture_preserves_strategy_and_fields():
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "html" / "snkrdunk_active_detail.html"
    page_url = "https://snkrdunk.com/products/HM4740-001"
    html = fixture_path.read_text(encoding="utf-8")
    page = HtmlPageAdapter(html, url=page_url)

    item = snkrdunk_db._parse_detail_page(page, page_url)

    assert item["status"] == "on_sale"
    assert item["price"] == 29044
    assert item["title"].startswith('Nike Air Max 95 OG Big Bubble "Neon Yellow"')
    assert item["image_urls"]
    assert item["_scrape_meta"]["strategy"] == "json_ld"
    assert item["_scrape_meta"]["field_sources"]["price"] == "json_ld"
    assert item["_scrape_meta"]["field_sources"]["images"] == "json_ld"


def test_parse_snkrdunk_app_router_flight_payload():
    page_url = "https://snkrdunk.com/products/CT8013-170"
    sneaker_data = {
        "sneaker": {
            "id": "CT8013-170",
            "name": 'Nike Air Jordan 12 "Royalty"',
            "localizedName": "Air Jordan 12 Royalty",
            "imageUrl": "https://cdn.snkrdunk.com/product.webp",
            "metaDescription": "Authenticated marketplace listing",
        },
        "sneakerSummary": {
            "minPrice": 32500,
            "usedMinPrice": 20000,
            "listingCount": 28,
            "usedListingCount": 3,
        },
    }
    record = ["$", "$L79", None, {"sneakerData": sneaker_data}]
    flight_data = f"57:{json.dumps(record, ensure_ascii=False)}"
    flight_script = f"self.__next_f.push({json.dumps([1, flight_data], ensure_ascii=False)})"
    page = HtmlPageAdapter(
        f"<html><body><script>{flight_script}</script></body></html>",
        url=page_url,
    )

    item = snkrdunk_db._parse_detail_page(page, page_url)

    assert item["title"] == 'Nike Air Jordan 12 "Royalty"'
    assert item["price"] == 32500
    assert item["status"] == "on_sale"
    assert item["image_urls"] == ["https://cdn.snkrdunk.com/product.webp"]
    assert item["_scrape_meta"]["strategy"] == "app_router"
    assert item["_scrape_meta"]["field_sources"]["price"] == "app_router"
