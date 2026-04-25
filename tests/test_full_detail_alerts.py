import offmall_db
import snkrdunk_db
import yahoo_db
import yahuoku_db


def test_detail_wrappers_alert_when_light_scrape_falls_back_to_empty_result(monkeypatch):
    events = []

    class FakeDispatcher:
        def notify_scrape_issue(self, **payload):
            events.append(payload)
            return True

    monkeypatch.setattr("services.scrape_alerts.get_alert_dispatcher", lambda: FakeDispatcher())

    cases = [
        (yahoo_db, "yahoo", "https://store.shopping.yahoo.co.jp/test/item-1.html"),
        (offmall_db, "offmall", "https://netmall.hardoff.co.jp/product/12345/"),
        (yahuoku_db, "yahuoku", "https://page.auctions.yahoo.co.jp/jp/auction/f123456789"),
        (snkrdunk_db, "snkrdunk", "https://snkrdunk.com/products/CT8013-170"),
    ]

    for module, expected_site, url in cases:
        monkeypatch.setattr(module, "scrape_item_detail_light", lambda _url: {})
        result = module.scrape_item_detail(url)
        assert result["status"] == "error"
        assert events[-1]["event_type"] == "error_detail_result"
        assert events[-1]["site"] == expected_site
        assert events[-1]["details"]["url"] == url


def test_detail_wrapper_does_not_alert_on_complete_result(monkeypatch):
    events = []

    class FakeDispatcher:
        def notify_scrape_issue(self, **payload):
            events.append(payload)
            return True

    monkeypatch.setattr("services.scrape_alerts.get_alert_dispatcher", lambda: FakeDispatcher())
    monkeypatch.setattr(
        yahoo_db,
        "scrape_item_detail_light",
        lambda url: {
            "url": url,
            "title": "Yahoo Camera",
            "price": 12000,
            "status": "active",
            "description": "ok",
            "image_urls": ["https://img.example.com/yahoo.jpg"],
            "variants": [],
            "_scrape_meta": {"strategy": "next_data", "field_sources": {"title": "next_data", "price": "next_data"}},
        },
    )

    result = yahoo_db.scrape_item_detail("https://store.shopping.yahoo.co.jp/test/item-1.html")

    assert result["title"] == "Yahoo Camera"
    assert events == []
