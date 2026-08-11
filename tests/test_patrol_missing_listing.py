"""Patrol outcomes for listings that no longer exist.

A removed listing is a product state, not a scrape failure. These tests pin
down that patrol records it as ``deleted``/``sold`` instead of raising the
``patrol_error`` alert that used to fire on every cycle for such items.
"""
import json
from unittest.mock import patch

from services.patrol.base_patrol import (
    PatrolResult,
    deleted_http_error_result,
    deleted_http_result,
)
from services.patrol.mercari_patrol import MercariPatrol
from services.patrol.yahuoku_patrol import YahuokuPatrol
from services.scrape_safety import ScrapeBlockedError, ScrapeHttpError


class _Page:
    """Minimal stand-in for a fetched page with an optional NEXT_DATA payload."""

    def __init__(self, *, next_data=None, text="", status=200):
        self._next_data = next_data
        self._text = text
        self.status = status

    def find(self, selector):
        if selector == "#__NEXT_DATA__" and self._next_data is not None:
            return _Node(json.dumps(self._next_data))
        return None

    def css(self, selector):
        return []

    def get_all_text(self):
        return self._text


class _Node:
    def __init__(self, text):
        self.text = text
        self.attrib = {}


def _no_alerts(monkeypatch):
    events = []

    class FakeDispatcher:
        def notify_scrape_issue(self, **payload):
            events.append(payload)
            return True

    monkeypatch.setattr("services.scrape_alerts.get_alert_dispatcher", lambda: FakeDispatcher())
    return events


def test_patrol_result_error_is_not_high_confidence():
    result = PatrolResult(error="boom")

    assert result.confidence == "low"
    assert PatrolResult(status="sold").confidence == "high"
    assert PatrolResult(error="boom", confidence="high").confidence == "high"


def test_deleted_http_error_result_maps_missing_statuses():
    for status in (404, 410):
        result = deleted_http_error_result(ScrapeHttpError("gone", status_code=status))
        assert result is not None
        assert result.status == "deleted"
        assert result.confidence == "high"
        assert result.reason == f"http-{status}"

    assert deleted_http_error_result(ScrapeHttpError("boom", status_code=500)) is None
    assert deleted_http_error_result(ScrapeBlockedError("blocked", status_code=403)) is None
    assert deleted_http_error_result(RuntimeError("transport failure")) is None


def test_deleted_http_result_still_reads_response_status():
    assert deleted_http_result(_Page(status=404)).status == "deleted"
    assert deleted_http_result(_Page(status=200)) is None


def test_mercari_patrol_reports_http_404_as_deleted(monkeypatch):
    events = _no_alerts(monkeypatch)
    error = ScrapeHttpError("mercariの取得に失敗しました（HTTP 404）。", status_code=404)

    with patch("services.patrol.mercari_patrol.should_use_mercari_browser_pool_patrol", return_value=False), \
            patch("services.patrol.mercari_patrol.fetch_dynamic", side_effect=error):
        result = MercariPatrol().fetch("https://jp.mercari.com/item/m97000191013")

    assert result.status == "deleted"
    assert result.success is True
    assert result.confidence == "high"
    assert events == []


def test_mercari_patrol_still_alerts_on_real_scrape_failures(monkeypatch):
    events = _no_alerts(monkeypatch)
    error = ScrapeBlockedError("mercariへのアクセスが制限されました（HTTP 403）。", status_code=403)

    with patch("services.patrol.mercari_patrol.should_use_mercari_browser_pool_patrol", return_value=False), \
            patch("services.patrol.mercari_patrol.fetch_dynamic", side_effect=error):
        result = MercariPatrol().fetch("https://jp.mercari.com/item/m97000191013")

    assert result.success is False
    assert [event["event_type"] for event in events] == ["patrol_error"]


def test_yahuoku_patrol_reads_closed_banner_without_next_data(monkeypatch):
    events = _no_alerts(monkeypatch)
    page = _Page(text="このオークションは終了しています シャープペンシルほか")

    with patch("services.scraping_client.fetch_marketplace_static", return_value=page):
        result = YahuokuPatrol().fetch("https://auctions.yahoo.co.jp/jp/auction/v1232554299")

    assert result.status == "sold"
    assert result.success is True
    assert result.reason == "page-text:sold"
    assert events == []


def test_yahuoku_patrol_reads_missing_page_without_next_data(monkeypatch):
    events = _no_alerts(monkeypatch)
    page = _Page(text="指定されたオークションは、終了しているか、存在しません。")

    with patch("services.scraping_client.fetch_marketplace_static", return_value=page):
        result = YahuokuPatrol().fetch("https://auctions.yahoo.co.jp/jp/auction/v1232554299")

    assert result.status == "deleted"
    assert result.success is True
    assert events == []


def test_yahuoku_patrol_still_alerts_when_page_text_is_inconclusive(monkeypatch):
    events = _no_alerts(monkeypatch)
    page = _Page(text="Yahoo!オークション トップ カテゴリ一覧")

    with patch("services.scraping_client.fetch_marketplace_static", return_value=page):
        result = YahuokuPatrol().fetch("https://auctions.yahoo.co.jp/jp/auction/v1232554299")

    assert result.success is False
    assert result.error == "No auction item data found"
    assert result.confidence == "low"
    assert [event["event_type"] for event in events] == ["patrol_error"]


def test_yahuoku_patrol_prefers_next_data_over_page_text(monkeypatch):
    _no_alerts(monkeypatch)
    page = _Page(
        next_data={
            "props": {
                "pageProps": {
                    "initialState": {
                        "item": {"detail": {"item": {"status": "closed", "price": 22500}}}
                    }
                }
            }
        },
        text="このオークションは終了しています",
    )

    with patch("services.scraping_client.fetch_marketplace_static", return_value=page):
        result = YahuokuPatrol().fetch("https://auctions.yahoo.co.jp/jp/auction/v1232554299")

    assert result.status == "sold"
    assert result.price == 22500
