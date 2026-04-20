import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from mercari_db import scrape_item_detail
from services.html_page_adapter import HtmlPageAdapter
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


def test_parse_mercari_network_payload_collects_images_from_full_payload_when_best_candidate_is_partial():
    payload = {
        "data": {
            "item": {
                "id": "m123456789",
                "name": "Payload Sneakers",
                "price": "2980",
                "description": "Payload description from API",
                "status": "on_sale",
            },
            "gallery": {
                "photos": [
                    {"url": "https://static.mercdn.net/item/detail/orig/photos/m123456789_1.jpg"},
                    {"url": "https://static.mercdn.net/item/detail/orig/photos/m123456789_2.jpg"},
                ]
            },
        }
    }

    item, meta = parse_mercari_network_payload(payload, "https://jp.mercari.com/item/m123456789")

    assert item["title"] == "Payload Sneakers"
    assert item["price"] == 2980
    assert item["image_urls"] == [
        "https://static.mercdn.net/item/detail/orig/photos/m123456789_1.jpg",
        "https://static.mercdn.net/item/detail/orig/photos/m123456789_2.jpg",
    ]
    assert meta["field_sources"]["image_urls"] == "payload"


def test_parse_mercari_network_payload_ignores_different_item_in_response():
    """A payload whose only item candidate identifies itself as a different
    Mercari item must not leak that item's title / price / photos into the
    parsed result for the requested URL."""
    payload = {
        "items": [
            {
                "id": "m999999999",
                "name": "Totally different product",
                "price": "4980",
                "description": "Seller's other listing",
                "status": "on_sale",
                "photos": [
                    {"url": "https://static.mercdn.net/item/detail/orig/photos/m999999999_1.jpg"},
                    {"url": "https://static.mercdn.net/item/detail/orig/photos/m999999999_2.jpg"},
                ],
            }
        ]
    }

    item, _meta = parse_mercari_network_payload(payload, "https://jp.mercari.com/item/m123456789")

    assert item["title"] == ""
    assert item["price"] is None
    assert item["image_urls"] == []


def test_parse_mercari_network_payload_prefers_candidate_with_matching_id():
    """When multiple item-like dicts are present, the one whose id matches
    the requested URL must be preferred even if another scores higher."""
    payload = {
        "related": [
            {
                "id": "m999999999",
                "name": "Related with lots of detail",
                "price": "8888",
                "description": "This one has everything and would normally win the score race",
                "status": "on_sale",
                "photos": [
                    {"url": "https://static.mercdn.net/item/detail/orig/photos/m999999999_1.jpg"},
                ],
            }
        ],
        "data": {
            "id": "m123456789",
            "name": "Actual Target Item",
            "price": "2980",
            "status": "on_sale",
            "photos": [
                {"url": "https://static.mercdn.net/item/detail/orig/photos/m123456789_1.jpg"},
                {"url": "https://static.mercdn.net/item/detail/orig/photos/m123456789_2.jpg"},
            ],
        },
    }

    item, meta = parse_mercari_network_payload(payload, "https://jp.mercari.com/item/m123456789")

    assert item["title"] == "Actual Target Item"
    assert item["price"] == 2980
    assert item["image_urls"] == [
        "https://static.mercdn.net/item/detail/orig/photos/m123456789_1.jpg",
        "https://static.mercdn.net/item/detail/orig/photos/m123456789_2.jpg",
    ]
    assert meta.get("candidate_id_matches_target") is True


def test_select_best_mercari_payload_ignores_similar_and_related_items():
    """Regression: Mercari's production API responses include similar/related/
    seller-other-items alongside the primary item.  The payload selector must
    not pick one of those as the source of truth — otherwise photos from a
    completely different product show up on the scraped item."""
    from mercari_db import _select_best_mercari_payload

    url = "https://jp.mercari.com/item/m123456789"

    captured_payloads = [
        # Rich "similar items" response with a title, price, description and
        # photos — all for some other product.  Under the old scoring this
        # used to be able to win.
        {
            "url": "https://api.mercari.jp/v2/relateditems/list-similar-items",
            "payload": {
                "items": [
                    {
                        "id": "m999999999",
                        "name": "Totally different product",
                        "price": "8888",
                        "description": "Unrelated description full of detail",
                        "status": "on_sale",
                        "photos": [
                            {"url": "https://static.mercdn.net/item/detail/orig/photos/m999999999_1.jpg"},
                            {"url": "https://static.mercdn.net/item/detail/orig/photos/m999999999_2.jpg"},
                        ],
                    }
                ]
            },
        },
        # Seller's other listings: also rich, also wrong.
        {
            "url": "https://api.mercari.jp/items/get_items?seller_id=1&status=on_sale",
            "payload": {
                "items": [
                    {
                        "id": "m888888888",
                        "name": "Seller's other item",
                        "price": "7777",
                        "photos": [
                            {"url": "https://static.mercdn.net/item/detail/orig/photos/m888888888_1.jpg"},
                        ],
                    }
                ]
            },
        },
        # The real item — but responding only with title/price (no photos) to
        # simulate the production regression where photos live in a split
        # response.
        {
            "url": "https://api.mercari.jp/items/get?id=m123456789&include_item_attributes=true",
            "payload": {
                "data": {
                    "id": "m123456789",
                    "name": "Actual Target Item",
                    "price": "2980",
                    "status": "on_sale",
                    "photos": [
                        {"url": "https://static.mercdn.net/item/detail/orig/photos/m123456789_1.jpg"},
                        {"url": "https://static.mercdn.net/item/detail/orig/photos/m123456789_2.jpg"},
                        {"url": "https://static.mercdn.net/item/detail/orig/photos/m123456789_3.jpg"},
                    ],
                }
            },
        },
    ]

    best = _select_best_mercari_payload(captured_payloads, url)
    best_item = best["item"]

    assert best_item["title"] == "Actual Target Item"
    assert best_item["price"] == 2980
    assert best_item["image_urls"] == [
        "https://static.mercdn.net/item/detail/orig/photos/m123456789_1.jpg",
        "https://static.mercdn.net/item/detail/orig/photos/m123456789_2.jpg",
        "https://static.mercdn.net/item/detail/orig/photos/m123456789_3.jpg",
    ]
    assert best["response_url"].startswith(
        "https://api.mercari.jp/items/get?id=m123456789"
    )


def test_collect_mercari_photo_urls_for_item_unions_across_blobs():
    """The url-regex photo union must assemble the full photo list across
    every captured response plus the page HTML, deduplicate, and return
    photos in ascending photo-index order regardless of source ordering."""
    from services.mercari_item_parser import collect_mercari_photo_urls_for_item

    # Simulate three separate captured API response payloads plus some page
    # HTML, where the target photos are scattered across them and mixed in
    # with photos from unrelated (related/similar/seller-other) items.
    blobs = [
        {
            "data": {
                "id": "m123456789",
                "photos": [
                    {"url": "https://static.mercdn.net/item/detail/orig/photos/m123456789_3.jpg?111"},
                ],
            }
        },
        {
            "items": [
                {
                    "id": "m999999999",
                    "photos": [
                        {"url": "https://static.mercdn.net/item/detail/orig/photos/m999999999_1.jpg"},
                    ],
                }
            ]
        },
        {
            "data": {
                "id": "m123456789",
                "photos": [
                    {"url": "https://static.mercdn.net/item/detail/orig/photos/m123456789_1.jpg?222"},
                    {"url": "https://static.mercdn.net/item/detail/orig/photos/m123456789_2.jpg?222"},
                    {"url": "https://static.mercdn.net/item/detail/orig/photos/m123456789_5.jpg?222"},
                ],
            }
        },
        '<html><img src="https://static.mercdn.net/item/detail/thumb/photos/m123456789_4.jpg?333"></html>',
    ]

    urls = collect_mercari_photo_urls_for_item(blobs, "m123456789")

    # Photos are returned in ascending index order, photos for other items
    # are filtered out, and each photo appears exactly once.
    assert [u.rsplit("/", 1)[-1].split("?")[0] for u in urls] == [
        "m123456789_1.jpg",
        "m123456789_2.jpg",
        "m123456789_3.jpg",
        "m123456789_4.jpg",
        "m123456789_5.jpg",
    ]


def test_collect_mercari_photo_urls_for_item_returns_empty_when_no_target_id():
    from services.mercari_item_parser import collect_mercari_photo_urls_for_item

    blobs = [
        {"photos": [{"url": "https://static.mercdn.net/item/detail/orig/photos/m1_1.jpg"}]},
    ]
    assert collect_mercari_photo_urls_for_item(blobs, "") == []
    assert collect_mercari_photo_urls_for_item(blobs, None) == []


def test_parse_mercari_network_payload_filters_photos_with_different_item_id():
    """Even if a candidate lacks an explicit id but happens to contain photos
    for a different Mercari item, those photos must be filtered out."""
    payload = {
        "data": {
            "name": "Actual Target Item",
            "price": "2980",
            "status": "on_sale",
            "photos": [
                "https://static.mercdn.net/item/detail/orig/photos/m123456789_1.jpg",
                "https://static.mercdn.net/item/detail/orig/photos/m999999999_1.jpg",
                "https://static.mercdn.net/item/detail/orig/photos/m123456789_2.jpg",
            ],
        }
    }

    item, _meta = parse_mercari_network_payload(payload, "https://jp.mercari.com/item/m123456789")

    assert item["image_urls"] == [
        "https://static.mercdn.net/item/detail/orig/photos/m123456789_1.jpg",
        "https://static.mercdn.net/item/detail/orig/photos/m123456789_2.jpg",
    ]


def test_parse_mercari_network_payload_maps_item_status_trading_to_sold():
    payload = {
        "items": [
            {
                "id": "m123456789",
                "name": "Payload Sneakers",
                "price": "2980",
                "status": "ITEM_STATUS_TRADING",
            }
        ]
    }

    item, meta = parse_mercari_network_payload(payload, "https://jp.mercari.com/item/m123456789")

    assert item["status"] == "sold"
    assert "payload-status:item_status_trading" in meta["reasons"]


def test_parse_mercari_network_payload_maps_item_status_stop_to_deleted():
    payload = {
        "data": {
            "item": {
                "id": "m123456789",
                "name": "Payload Sneakers",
                "price": "2980",
                "status": "ITEM_STATUS_STOP",
            }
        }
    }

    item, meta = parse_mercari_network_payload(payload, "https://jp.mercari.com/item/m123456789")

    assert item["status"] == "deleted"
    assert "payload-status:item_status_stop" in meta["reasons"]


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


def test_scrape_item_detail_prefers_richer_dom_image_list_when_payload_only_has_first_image(monkeypatch):
    url = "https://jp.mercari.com/item/m123456789"
    monkeypatch.setenv("MERCARI_USE_NETWORK_PAYLOAD", "true")

    payload_bundle = {
        "item": {
            "url": url,
            "title": "Payload Title",
            "price": 3200,
            "status": "on_sale",
            "description": "Payload description",
            "image_urls": ["https://example.com/dom-1.jpg"],
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
    dom_item = _dom_item(url)
    dom_item["image_urls"] = [
        "https://example.com/dom-1.jpg",
        "https://example.com/dom-2.jpg",
    ]

    with patch("mercari_db._capture_mercari_network_payload", return_value=payload_bundle), patch(
        "mercari_db.fetch_dynamic", return_value=MagicMock()
    ), patch("mercari_db.parse_mercari_item_page", return_value=(dom_item, _dom_meta())):
        data = scrape_item_detail(url)

    assert data["image_urls"] == [
        "https://example.com/dom-1.jpg",
        "https://example.com/dom-2.jpg",
    ]
    assert data["_scrape_meta"]["field_sources"]["image_urls"] == "dom"


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


def test_scrape_item_detail_non_browser_pool_path_unions_photos_from_captured_payloads(monkeypatch):
    """Regression for the Devin Review finding on PR #64.

    When ``MERCARI_CAPTURE_NETWORK_PAYLOAD`` is enabled but
    ``MERCARI_USE_BROWSER_POOL_DETAIL`` is NOT, the non-browser-pool capture
    path must still feed every captured API response into the photo URL
    union post-pass.  Before the fix, ``_capture_mercari_network_payload``
    discarded all but the single winning candidate, so items whose photos
    were split across multiple responses (or whose winner carried a
    sparse photo list) returned only 1 image in production.
    """
    url = "https://jp.mercari.com/item/m123456789"
    monkeypatch.setenv("MERCARI_CAPTURE_NETWORK_PAYLOAD", "true")
    monkeypatch.setenv("MERCARI_USE_NETWORK_PAYLOAD", "true")
    monkeypatch.delenv("MERCARI_USE_BROWSER_POOL_DETAIL", raising=False)

    # Winner carries a single photo; the rest live in other captured
    # responses (think /items/get_items?seller_id=… returning the full
    # photo list of the target item alongside other seller items).
    winner_photo = "https://static.mercdn.net/item/detail/orig/photos/m123456789_1.jpg?aaa"
    other_photos = [
        "https://static.mercdn.net/item/detail/orig/photos/m123456789_2.jpg?bbb",
        "https://static.mercdn.net/item/detail/orig/photos/m123456789_3.jpg?ccc",
        "https://static.mercdn.net/item/detail/orig/photos/m123456789_4.jpg?ddd",
    ]

    payload_bundle = {
        "item": {
            "url": url,
            "title": "Payload Title",
            "price": 3200,
            "status": "on_sale",
            "description": "Payload description",
            "image_urls": [winner_photo],
            "variants": [],
        },
        "meta": {
            "strategy": "payload",
            "field_sources": {"image_urls": "payload"},
        },
        "response_url": "https://api.mercari.example/items/m123456789",
        "responses_seen": 2,
        "captured_payloads": [
            {
                "url": "https://api.mercari.jp/items/get?id=m123456789",
                "payload": {
                    "data": {
                        "id": "m123456789",
                        "photos": [{"url": winner_photo}],
                    },
                },
            },
            {
                "url": "https://api.mercari.jp/v2/relateditems/list-similar-items",
                "payload": {
                    "items": [
                        {
                            "id": "m123456789",
                            "photos": [{"url": u} for u in other_photos],
                        },
                        {
                            "id": "m999999999",
                            "photos": [
                                {"url": "https://static.mercdn.net/item/detail/orig/photos/m999999999_1.jpg"},
                            ],
                        },
                    ],
                },
            },
        ],
    }

    with patch("mercari_db._capture_mercari_network_payload", return_value=payload_bundle), patch(
        "mercari_db.fetch_dynamic", return_value=MagicMock()
    ), patch("mercari_db.parse_mercari_item_page", return_value=(_dom_item(url), _dom_meta())):
        data = scrape_item_detail(url)

    image_urls = data["image_urls"]
    # All four target photos must be present, ordered by photo index,
    # deduplicated, and with no photos belonging to the unrelated
    # m999999999 item.
    assert image_urls[:4] == [winner_photo] + other_photos
    assert all("m999999999" not in u for u in image_urls)


def test_scrape_item_detail_can_use_browser_pool_dom_fetch(monkeypatch):
    url = "https://jp.mercari.com/item/m123456789"
    monkeypatch.setenv("MERCARI_USE_BROWSER_POOL_DETAIL", "true")
    monkeypatch.delenv("MERCARI_USE_NETWORK_PAYLOAD", raising=False)

    with patch(
        "mercari_db.fetch_mercari_page_and_payloads_via_browser_pool_sync",
        return_value=(MagicMock(), []),
    ) as mock_pool_fetch, patch(
        "mercari_db.fetch_dynamic"
    ) as mock_fetch_dynamic, patch(
        "mercari_db.parse_mercari_item_page", return_value=(_dom_item(url), _dom_meta())
    ):
        data = scrape_item_detail(url)

    mock_pool_fetch.assert_called_once_with(url, network_idle=True)
    mock_fetch_dynamic.assert_not_called()
    assert data["title"] == "DOM Title"


def test_scrape_item_detail_browser_pool_capture_uses_payload_first(monkeypatch):
    url = "https://jp.mercari.com/item/m123456789"
    monkeypatch.setenv("MERCARI_USE_BROWSER_POOL_DETAIL", "true")
    monkeypatch.setenv("MERCARI_USE_NETWORK_PAYLOAD", "true")

    payloads = [
        {
            "url": "https://api.mercari.jp/items/get?id=m123456789",
            "payload": {
                "data": {
                    "item": {
                        "id": "m123456789",
                        "name": "Payload Title",
                        "price": "3200",
                        "status": "on_sale",
                        "photos": [
                            {"url": "https://static.mercdn.net/item/detail/orig/photos/m123456789_1.jpg"},
                            {"url": "https://static.mercdn.net/item/detail/orig/photos/m123456789_2.jpg"},
                        ],
                    }
                }
            },
        }
    ]
    dom_item = _dom_item(url)
    dom_item["description"] = "DOM description"
    dom_item["image_urls"] = ["https://static.mercdn.net/item/detail/orig/photos/m123456789_1.jpg"]

    with patch(
        "mercari_db.fetch_mercari_page_and_payloads_via_browser_pool_sync",
        return_value=(MagicMock(), payloads),
    ) as mock_pool_capture, patch("mercari_db.fetch_dynamic") as mock_fetch_dynamic, patch(
        "mercari_db._capture_mercari_network_payload"
    ) as mock_capture, patch(
        "mercari_db.parse_mercari_item_page", return_value=(dom_item, _dom_meta())
    ):
        data = scrape_item_detail(url)

    mock_pool_capture.assert_called_once_with(url, network_idle=True)
    mock_fetch_dynamic.assert_not_called()
    mock_capture.assert_not_called()
    assert data["title"] == "Payload Title"
    assert data["price"] == 3200
    assert data["status"] == "on_sale"
    assert data["image_urls"] == [
        "https://static.mercdn.net/item/detail/orig/photos/m123456789_1.jpg",
        "https://static.mercdn.net/item/detail/orig/photos/m123456789_2.jpg",
    ]
    assert data["description"] == "DOM description"
    assert data["_scrape_meta"]["network_capture"]["captured"] is True
    assert data["_scrape_meta"]["network_capture"]["used_payload"] is True
    assert data["_scrape_meta"]["network_capture"]["response_url"] == "https://api.mercari.jp/items/get?id=m123456789"


def test_scrape_item_detail_browser_pool_capture_ignores_zero_score_payloads(monkeypatch):
    url = "https://jp.mercari.com/item/m123456789"
    monkeypatch.setenv("MERCARI_USE_BROWSER_POOL_DETAIL", "true")
    monkeypatch.setenv("MERCARI_USE_NETWORK_PAYLOAD", "true")

    payloads = [
        {
            "url": "https://api.mercari.jp/client_events/v2/event",
            "payload": {"data": {}},
        }
    ]

    with patch(
        "mercari_db.fetch_mercari_page_and_payloads_via_browser_pool_sync",
        return_value=(MagicMock(), payloads),
    ), patch("mercari_db.parse_mercari_item_page", return_value=(_dom_item(url), _dom_meta())):
        data = scrape_item_detail(url)

    assert data["title"] == "DOM Title"
    assert data["_scrape_meta"]["network_capture"]["captured"] is False
    assert data["_scrape_meta"]["network_capture"]["response_url"] == ""
    assert data["_scrape_meta"]["network_capture"]["used_payload"] is False


def test_scrape_item_detail_refetches_via_browser_pool_when_initial_dom_has_only_one_image(monkeypatch):
    url = "https://jp.mercari.com/item/m123456789"
    monkeypatch.delenv("MERCARI_USE_BROWSER_POOL_DETAIL", raising=False)
    monkeypatch.delenv("MERCARI_CAPTURE_NETWORK_PAYLOAD", raising=False)
    monkeypatch.delenv("MERCARI_USE_NETWORK_PAYLOAD", raising=False)

    initial_item = _dom_item(url)
    initial_item["status"] = "unknown"
    initial_item["image_urls"] = ["https://example.com/dom-1.jpg"]
    initial_meta = {
        "strategy": "meta",
        "page_type": "unknown_detail",
        "field_sources": {
            "title": "dom",
            "price": "meta",
            "status": "dom",
            "description": "dom",
            "image_urls": "html",
            "variants": "dom",
        },
    }

    refetched_item = _dom_item(url)
    refetched_item["image_urls"] = [
        "https://example.com/dom-1.jpg",
        "https://example.com/dom-2.jpg",
    ]
    refetched_meta = {
        "strategy": "meta",
        "page_type": "active_detail",
        "field_sources": {
            "title": "jsonld",
            "price": "meta",
            "status": "jsonld",
            "description": "dom",
            "image_urls": "dom+jsonld",
            "variants": "dom",
        },
    }

    with patch("mercari_db.fetch_dynamic", return_value=MagicMock()) as mock_fetch_dynamic, patch(
        "mercari_db.fetch_mercari_page_and_payloads_via_browser_pool_sync", return_value=(MagicMock(), [])
    ) as mock_pool_fetch, patch(
        "mercari_db.parse_mercari_item_page",
        side_effect=[(initial_item, initial_meta), (refetched_item, refetched_meta)],
    ):
        data = scrape_item_detail(url)

    mock_fetch_dynamic.assert_called_once_with(url, headless=True, network_idle=True)
    mock_pool_fetch.assert_called_once_with(url, network_idle=True)
    assert data["image_urls"] == [
        "https://example.com/dom-1.jpg",
        "https://example.com/dom-2.jpg",
    ]
    assert data["status"] == "on_sale"
    assert data["_scrape_meta"]["dom_refetch"] == "browser_pool"


def test_scrape_item_detail_normalizes_dynamic_page_to_html_adapter_before_parse(monkeypatch):
    url = "https://jp.mercari.com/item/m123456789"
    monkeypatch.delenv("MERCARI_CAPTURE_NETWORK_PAYLOAD", raising=False)
    monkeypatch.delenv("MERCARI_USE_NETWORK_PAYLOAD", raising=False)

    html = """
    <html>
      <head>
        <meta name="product:price:amount" content="2980" />
        <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "Payload Sneakers",
            "image": [
              "https://static.mercdn.net/item/detail/orig/photos/m123456789_1.jpg",
              "https://static.mercdn.net/item/detail/orig/photos/m123456789_2.jpg"
            ],
            "offers": {"availability": "https://schema.org/InStock"}
          }
        </script>
      </head>
      <body>
        <h1>Payload Sneakers</h1>
      </body>
    </html>
    """

    class FakeDynamicPage:
        def __init__(self):
            self.url = url
            self.status = 200
            self.body = html

        def css(self, selector):
            if selector == "h1":
                node = MagicMock()
                node.text = "Payload Sneakers"
                node.attrib = {}
                return [node]
            return []

    seen = {}

    def _capturing_parse(page, parse_url):
        seen["page"] = page
        return _dom_item(parse_url), _dom_meta()

    with patch("mercari_db.fetch_dynamic", return_value=FakeDynamicPage()), patch(
        "mercari_db.parse_mercari_item_page", side_effect=_capturing_parse
    ):
        scrape_item_detail(url)

    assert isinstance(seen["page"], HtmlPageAdapter)
    assert seen["page"].url == url


def test_scrape_item_detail_browser_pool_merges_payload_images(monkeypatch):
    """When browser pool captures network payloads, their images are merged into the result."""
    url = "https://jp.mercari.com/item/m123456789"
    monkeypatch.setenv("MERCARI_USE_BROWSER_POOL_DETAIL", "true")
    monkeypatch.delenv("MERCARI_CAPTURE_NETWORK_PAYLOAD", raising=False)
    monkeypatch.delenv("MERCARI_USE_NETWORK_PAYLOAD", raising=False)

    dom_item = _dom_item(url)
    dom_item["image_urls"] = ["https://static.mercdn.net/item/detail/orig/photos/m123456789_1.jpg"]
    dom_meta_result = _dom_meta()
    dom_meta_result["field_sources"]["image_urls"] = "dom"

    # Simulate network payload captured by browser pool with more images
    bp_payloads = [
        {
            "url": "https://api.mercari.jp/items/get?id=m123456789",
            "payload": {
                "data": {
                    "id": "m123456789",
                    "name": "DOM Title",
                    "price": 2500,
                    "status": "on_sale",
                    "description": "DOM description",
                    "photos": [
                        "https://static.mercdn.net/item/detail/orig/photos/m123456789_1.jpg",
                        "https://static.mercdn.net/item/detail/orig/photos/m123456789_2.jpg",
                        "https://static.mercdn.net/item/detail/orig/photos/m123456789_3.jpg",
                    ],
                }
            },
        }
    ]

    with patch(
        "mercari_db.fetch_mercari_page_and_payloads_via_browser_pool_sync",
        return_value=(MagicMock(), bp_payloads),
    ), patch(
        "mercari_db.parse_mercari_item_page",
        return_value=(dom_item, dom_meta_result),
    ):
        data = scrape_item_detail(url)

    # The payload images should be merged into the result
    assert len(data["image_urls"]) >= 3
    assert "https://static.mercdn.net/item/detail/orig/photos/m123456789_1.jpg" in data["image_urls"]
    assert "https://static.mercdn.net/item/detail/orig/photos/m123456789_2.jpg" in data["image_urls"]
    assert "https://static.mercdn.net/item/detail/orig/photos/m123456789_3.jpg" in data["image_urls"]
    assert data["_scrape_meta"]["network_capture"]["used_payload"] is True
