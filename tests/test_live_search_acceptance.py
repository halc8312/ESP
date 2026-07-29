from scripts import live_search_acceptance


SEARCH_URL = "https://shopping.yahoo.co.jp/search?p=camera"
DETAIL_URL = "https://store.shopping.yahoo.co.jp/shop/camera.html"


def test_live_acceptance_requires_usable_status_and_allowlisted_detail_url(
    monkeypatch,
):
    monkeypatch.setitem(
        live_search_acceptance.SEARCH_SCRAPERS,
        "yahoo",
        lambda **_kwargs: [
            {
                "url": DETAIL_URL,
                "title": "Camera",
                "status": "on_sale",
            }
        ],
    )

    result = live_search_acceptance.run_target(
        "yahoo",
        SEARCH_URL,
        limit=1,
    )

    assert result.status == "passed"
    assert result.item_count == 1


def test_live_acceptance_rejects_blocked_item_even_with_title(monkeypatch):
    monkeypatch.setitem(
        live_search_acceptance.SEARCH_SCRAPERS,
        "yahoo",
        lambda **_kwargs: [
            {
                "url": DETAIL_URL,
                "title": "Challenge shell",
                "status": "blocked",
            }
        ],
    )

    result = live_search_acceptance.run_target(
        "yahoo",
        SEARCH_URL,
        limit=1,
    )

    assert result.status == "failed"
    assert "status or detail-URL validation" in result.error


def test_live_acceptance_rejects_cross_domain_item_url(monkeypatch):
    monkeypatch.setitem(
        live_search_acceptance.SEARCH_SCRAPERS,
        "yahoo",
        lambda **_kwargs: [
            {
                "url": "https://evil.example/camera.html",
                "title": "Camera",
                "status": "on_sale",
            }
        ],
    )

    result = live_search_acceptance.run_target(
        "yahoo",
        SEARCH_URL,
        limit=1,
    )

    assert result.status == "failed"
    assert "status or detail-URL validation" in result.error
