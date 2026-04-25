from types import SimpleNamespace

from services.scrape_alerts import report_detail_result, report_patrol_result


def test_report_patrol_result_alerts_on_active_without_price(monkeypatch):
    events = []

    class FakeDispatcher:
        def notify_scrape_issue(self, **payload):
            events.append(payload)
            return True

    monkeypatch.setattr("services.scrape_alerts.get_alert_dispatcher", lambda: FakeDispatcher())

    delivered = report_patrol_result(
        "mercari",
        "https://jp.mercari.com/item/test",
        SimpleNamespace(
            status="active",
            price=None,
            error=None,
            reason="active-without-price",
            confidence="low",
            price_source="none",
        ),
    )

    assert delivered is True
    assert events[0]["event_type"] == "patrol_active_without_price"
    assert events[0]["details"]["reason"] == "active-without-price"


def test_report_detail_result_alerts_on_unknown_status(monkeypatch):
    events = []

    class FakeDispatcher:
        def notify_scrape_issue(self, **payload):
            events.append(payload)
            return True

    monkeypatch.setattr("services.scrape_alerts.get_alert_dispatcher", lambda: FakeDispatcher())

    delivered = report_detail_result(
        "surugaya",
        "https://www.suruga-ya.jp/product/detail/1",
        {"status": "unknown", "price": None, "title": ""},
        {"confidence": "low", "reasons": ["degraded-marker:javascript is disabled"], "strategy": "degraded"},
    )

    assert delivered is True
    assert events[0]["event_type"] == "unknown_detail_result"
    assert "degraded-marker:javascript is disabled" in events[0]["details"]["reasons"]
