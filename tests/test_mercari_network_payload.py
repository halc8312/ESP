import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from mercari_db import scrape_item_detail
from services.mercari_item_parser import parse_mercari_network_payload


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mercari"


def _dom_item(url: str) -> dict:
    return {
        "url": url,
        "title": "DOM Title",
        "price": 2500,
        "status": "on_sale",
        "description": "DOM description",
        "image_urls": ["https://example.com/dom-1.jpg"],
        "variants": [{"option1_value": "M", "price": 2500, "inventory_qty": 1}],
    }


def _dom_meta() -> dict:
    return {
        "strategy": "dom",
        "field_sources": {
            "title": "dom",
            "price": "meta",
            "status": "dom",
            "description": "dom",
            "image_urls": "dom",
            "variants": "dom",
        },
    }


def test_parse_mercari_network_payload_extracts_expected_fields():
    payload = json.loads((FIXTURE_DIR / "network_payload_item.json").read_text(encoding="utf-8"))

    item, meta = parse_mercari_network_payload(payload, "https://jp.mercari.com/item/m123456789")

    assert item["title"] == "Payload Sneakers"
    assert item["price"] == 2980
    assert item["description"] == "Payload description from API"
    assert item["status"] == "on_sale"
    assert len(item["image_urls"]) == 2
    assert item["_scrape_meta"]["strategy"] == "payload"
    assert meta["field_sources"]["price"] == "payload"


def test_scrape_item_detail_capture_only_keeps_dom_result_but_records_shadow_compare(monkeypatch):
    url = "https://jp.mercari.com/item/m123456789"
    monkeypatch.setenv("MERCARI_CAPTURE_NETWORK_PAYLOAD", "true")
    monkeypatch.delenv("MERCARI_USE_NETWORK_PAYLOAD", raising=False)

    payload_bundle = {
        "item": {
            "url": url,
            "title": "Payload Title",
            "price": 3200,
            "status": "sold",
            "description": "Payload description",
            "image_urls": ["https://example.com/payload-1.jpg"],
            "variants": [],
        },
        "meta": {
            "strategy": "payload",
            "field_sources": {
                "title": "payload",
                "price": "payload",
                "status": "payload",
                "description": "payload",
                "image_urls": "payload",
            },
        },
        "response_url": "https://api.mercari.example/items/m123456789",
        "responses_seen": 2,
    }

    with patch("mercari_db._capture_mercari_network_payload", return_value=payload_bundle), patch(
        "mercari_db.fetch_dynamic", return_value=MagicMock()
    ), patch("mercari_db.parse_mercari_item_page", return_value=(_dom_item(url), _dom_meta())):
        data = scrape_item_detail(url)

    assert data["title"] == "DOM Title"
    assert data["price"] == 2500
    assert data["description"] == "DOM description"
    assert data["_scrape_meta"]["network_capture"]["enabled"] is True
    assert data["_scrape_meta"]["network_capture"]["captured"] is True
    assert data["_scrape_meta"]["network_capture"]["used_payload"] is False
    assert data["_scrape_meta"]["shadow_compare"]["mismatch_fields"] == [
        "title",
        "price",
        "description",
        "image_urls",
        "status",
    ]


def test_scrape_item_detail_dom_only_skips_payload_capture_when_flags_disabled(monkeypatch):
    url = "https://jp.mercari.com/item/m123456789"
    monkeypatch.delenv("MERCARI_CAPTURE_NETWORK_PAYLOAD", raising=False)
    monkeypatch.delenv("MERCARI_USE_NETWORK_PAYLOAD", raising=False)

    with patch("mercari_db._capture_mercari_network_payload") as mock_capture, patch(
        "mercari_db.fetch_dynamic", return_value=MagicMock()
    ), patch("mercari_db.parse_mercari_item_page", return_value=(_dom_item(url), _dom_meta())):
        data = scrape_item_detail(url)

    mock_capture.assert_not_called()
    assert data["title"] == "DOM Title"
    assert data["_scrape_meta"]["network_capture"]["enabled"] is False
    assert data["_scrape_meta"]["network_capture"]["used_payload"] is False


def test_scrape_item_detail_uses_payload_first_with_dom_field_fallback(monkeypatch):
    url = "https://jp.mercari.com/item/m123456789"
    monkeypatch.setenv("MERCARI_USE_NETWORK_PAYLOAD", "true")

    payload_bundle = {
        "item": {
            "url": url,
            "title": "Payload Title",
            "price": 3200,
            "status": "sold",
            "description": "",
            "image_urls": [],
            "variants": [],
        },
        "meta": {
            "strategy": "payload",
            "field_sources": {
                "title": "payload",
                "price": "payload",
                "status": "payload",
            },
        },
        "response_url": "https://api.mercari.example/items/m123456789",
        "responses_seen": 1,
    }

    with patch("mercari_db._capture_mercari_network_payload", return_value=payload_bundle), patch(
        "mercari_db.fetch_dynamic", return_value=MagicMock()
    ), patch("mercari_db.parse_mercari_item_page", return_value=(_dom_item(url), _dom_meta())):
        data = scrape_item_detail(url)

    assert data["title"] == "Payload Title"
    assert data["price"] == 3200
    assert data["status"] == "sold"
    assert data["description"] == "DOM description"
    assert data["image_urls"] == ["https://example.com/dom-1.jpg"]
    assert data["variants"] == [{"option1_value": "M", "price": 2500, "inventory_qty": 1}]
    assert data["_scrape_meta"]["strategy"] == "payload"
    assert data["_scrape_meta"]["field_sources"]["title"] == "payload"
    assert data["_scrape_meta"]["field_sources"]["description"] == "dom"
    assert data["_scrape_meta"]["network_capture"]["enabled"] is True
    assert data["_scrape_meta"]["network_capture"]["used_payload"] is True


def test_scrape_item_detail_falls_back_to_dom_when_payload_missing(monkeypatch):
    url = "https://jp.mercari.com/item/m123456789"
    monkeypatch.setenv("MERCARI_USE_NETWORK_PAYLOAD", "true")

    payload_bundle = {
        "item": {
            "url": url,
            "title": "",
            "price": None,
            "status": "unknown",
            "description": "",
            "image_urls": [],
            "variants": [],
        },
        "meta": {"strategy": "payload", "field_sources": {}},
        "response_url": "",
        "responses_seen": 1,
    }

    with patch("mercari_db._capture_mercari_network_payload", return_value=payload_bundle), patch(
        "mercari_db.fetch_dynamic", return_value=MagicMock()
    ), patch("mercari_db.parse_mercari_item_page", return_value=(_dom_item(url), _dom_meta())):
        data = scrape_item_detail(url)

    assert data["title"] == "DOM Title"
    assert data["price"] == 2500
    assert data["_scrape_meta"]["network_capture"]["captured"] is False
    assert data["_scrape_meta"]["network_capture"]["used_payload"] is False


def test_scrape_item_detail_handles_payload_capture_failure_without_crash(monkeypatch):
    url = "https://jp.mercari.com/item/m123456789"
    monkeypatch.setenv("MERCARI_CAPTURE_NETWORK_PAYLOAD", "true")
    monkeypatch.delenv("MERCARI_USE_NETWORK_PAYLOAD", raising=False)

    with patch("mercari_db._capture_mercari_network_payload", side_effect=RuntimeError("capture failed")), patch(
        "mercari_db.fetch_dynamic", return_value=MagicMock()
    ), patch("mercari_db.parse_mercari_item_page", return_value=(_dom_item(url), _dom_meta())):
        data = scrape_item_detail(url)

    assert data["title"] == "DOM Title"
    assert data["_scrape_meta"]["network_capture"]["enabled"] is True
    assert data["_scrape_meta"]["network_capture"]["captured"] is False
    assert data["_scrape_meta"]["network_capture"]["capture_error"] == "capture failed"
    assert "shadow_compare" in data["_scrape_meta"]


def test_scrape_item_detail_can_return_payload_result_when_dom_fetch_fails(monkeypatch):
    url = "https://jp.mercari.com/item/m123456789"
    monkeypatch.setenv("MERCARI_USE_NETWORK_PAYLOAD", "true")

    payload_bundle = {
        "item": {
            "url": url,
            "title": "Payload Title",
            "price": 3200,
            "status": "on_sale",
            "description": "Payload description",
            "image_urls": ["https://example.com/payload-1.jpg"],
            "variants": [],
        },
        "meta": {
            "strategy": "payload",
            "field_sources": {
                "title": "payload",
                "price": "payload",
                "status": "payload",
                "description": "payload",
                "image_urls": "payload",
            },
        },
        "response_url": "https://api.mercari.example/items/m123456789",
        "responses_seen": 1,
    }

    with patch("mercari_db._capture_mercari_network_payload", return_value=payload_bundle), patch(
        "mercari_db.fetch_dynamic", side_effect=RuntimeError("dom failed")
    ):
        data = scrape_item_detail(url)

    assert data["title"] == "Payload Title"
    assert data["price"] == 3200
    assert data["description"] == "Payload description"
    assert data["_scrape_meta"]["fallback_mode"] == "payload_without_dom"
    assert data["_scrape_meta"]["network_capture"]["used_payload"] is True


def test_scrape_item_detail_can_use_browser_pool_dom_fetch(monkeypatch):
    url = "https://jp.mercari.com/item/m123456789"
    monkeypatch.setenv("MERCARI_USE_BROWSER_POOL_DETAIL", "true")
    monkeypatch.delenv("MERCARI_USE_NETWORK_PAYLOAD", raising=False)

    with patch("mercari_db.fetch_mercari_page_via_browser_pool_sync", return_value=MagicMock()) as mock_pool_fetch, patch(
        "mercari_db.fetch_dynamic"
    ) as mock_fetch_dynamic, patch(
        "mercari_db.parse_mercari_item_page", return_value=(_dom_item(url), _dom_meta())
    ):
        data = scrape_item_detail(url)

    mock_pool_fetch.assert_called_once_with(url, network_idle=True)
    mock_fetch_dynamic.assert_not_called()
    assert data["title"] == "DOM Title"
