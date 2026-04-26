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
