"""Exercise each real search adapter through the worker with local listing HTML."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import mercari_db
from jobs import scrape_tasks
from services import scrape_health, scraping_client
from services.html_page_adapter import HtmlPageAdapter
from services.scrape_safety import (
    ScrapeBlockedError, ScrapeSelectorDriftError, SearchResult, require_search_outcome,
)


SEARCH_URLS = {
    "mercari": "https://jp.mercari.com/search?keyword=fixture",
    "yahoo": "https://shopping.yahoo.co.jp/search?p=fixture",
    "rakuma": "https://fril.jp/s?query=fixture",
    "surugaya": "https://www.suruga-ya.jp/search?search_word=fixture",
    "offmall": "https://netmall.hardoff.co.jp/search/?q=fixture",
    "yahuoku": "https://auctions.yahoo.co.jp/search/search?p=fixture",
    "snkrdunk": "https://snkrdunk.com/search?keywords=fixture",
    "recordcity": "https://www.recordcity.jp/ja/catalog?keyword=fixture",
}


def stub_listing(monkeypatch, site, text):
    """Stub only transport/browser methods; keep adapter parsing and validation."""
    calls = []
    url = SEARCH_URLS[site]
    html = f'<html><head><meta charset="utf-8"><title>Search</title></head><body>{text}</body></html>'

    def fetch(target_url, **kwargs):
        assert kwargs.get("kind", "search") == "search", "empty results must not fetch details"
        calls.append(target_url)
        return HtmlPageAdapter(html, url=target_url)

    monkeypatch.setattr(scraping_client, "fetch_marketplace_static", fetch)
    monkeypatch.setattr(scraping_client, "fetch_dynamic", fetch)
    monkeypatch.setattr(scrape_tasks.snkrdunk_db, "should_use_snkrdunk_browser_pool_dynamic", lambda: False)
    monkeypatch.setattr(scrape_tasks.recordcity_db, "_fetch_page", fetch)

    def surugaya_fetch(session, target_url, **kwargs):
        calls.append(target_url)
        return SimpleNamespace(text=html, content=html.encode(), status_code=200, url=target_url), None

    monkeypatch.setattr(scrape_tasks.surugaya_db, "get_session", lambda: object())
    monkeypatch.setattr(scrape_tasks.surugaya_db, "_fetch_with_retry", surugaya_fetch)
    monkeypatch.setattr(scrape_tasks.surugaya_db, "_should_use_yahoo_search_fallback", lambda: False)

    page = SimpleNamespace(
        url=url, title=AsyncMock(return_value="Search"),
        query_selector_all=AsyncMock(return_value=[]),
        evaluate=AsyncMock(), wait_for_selector=AsyncMock(), wait_for_timeout=AsyncMock(),
        locator=lambda selector: SimpleNamespace(inner_text=AsyncMock(return_value=text)),
    )

    async def goto(target_url, **kwargs):
        return fetch(target_url)

    page.goto = goto
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), route=AsyncMock())

    async def run_page_task(site, factory, **kwargs):
        return await factory(page, context)

    monkeypatch.setattr(mercari_db, "run_browser_page_task", run_page_task)
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=playwright)
    manager.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: manager)

    def detail_forbidden(*args, **kwargs):
        pytest.fail("empty results must not request product details")

    for name in SEARCH_URLS:
        module = mercari_db if name == "mercari" else getattr(scrape_tasks, name + "_db")
        monkeypatch.setattr(module, "scrape_item_detail", detail_forbidden)
        if hasattr(module, "_scrape_item_detail_async"):
            monkeypatch.setattr(module, "_scrape_item_detail_async", detail_forbidden)
    monkeypatch.setattr(scrape_tasks, "filter_excluded_items", lambda items, user_id: (items, 0))
    return calls


@pytest.mark.parametrize("site", SEARCH_URLS)
def test_verified_empty_reaches_task_without_inventing_success_or_failure(monkeypatch, site):
    calls = stub_listing(monkeypatch, site, "検索結果がありません")
    observed = []
    monkeypatch.setattr(scrape_tasks, "record_observation_safely", lambda **kw: observed.append(kw))
    result = scrape_tasks.execute_scrape_job({
        "site": site, "target_url": SEARCH_URLS[site], "limit": 10, "persist_to_db": False,
    })
    assert calls
    assert result["items"] == []
    assert result["search_quality"]["end_reason"] == "explicit_empty"
    assert result["search_quality"]["completion_verified"] is True
    assert result["search_quality"]["valid_count"] == 0
    assert observed == [dict(site=site, route="search", outcome="no_observations",
                             reason="empty_result", success_count=0, error_count=0)]


@pytest.mark.parametrize("site", SEARCH_URLS)
@pytest.mark.parametrize("text,error", [
    ("Search catalog", ScrapeSelectorDriftError),
    ("verify you are human 検索結果がありません", ScrapeBlockedError),
])
def test_unproven_or_blocked_listing_is_not_reported_as_verified_empty(monkeypatch, site, text, error):
    stub_listing(monkeypatch, site, text)
    monkeypatch.setattr(scrape_tasks, "record_observation_safely", lambda **kw: None)
    with pytest.raises(error):
        scrape_tasks.execute_scrape_job({
            "site": site, "target_url": SEARCH_URLS[site], "persist_to_db": False,
        })


@pytest.mark.parametrize("site", SEARCH_URLS)
def test_bare_empty_adapter_results_stay_unverified(monkeypatch, site):
    if site == "mercari":
        monkeypatch.setattr(scrape_tasks, "scrape_search_result", lambda **kw: [])
    else:
        monkeypatch.setattr(getattr(scrape_tasks, site + "_db"), "scrape_search_result", lambda **kw: [])
    monkeypatch.setattr(scrape_tasks, "filter_excluded_items", lambda items, user_id: (items, 0))
    observed = []
    monkeypatch.setattr(scrape_tasks, "record_observation_safely", lambda **kw: observed.append(kw))
    result = scrape_tasks.execute_scrape_job({"site": site, "keyword": "fixture", "persist_to_db": False})
    assert result["search_quality"]["end_reason"] == "unknown"
    assert observed[0]["outcome"] == "failure"
    assert observed[0]["reason"] == "incomplete_results"


def test_verified_empty_does_not_open_or_resolve_incident(app, monkeypatch):
    stub_listing(monkeypatch, "yahoo", "検索結果がありません")
    request = {"site": "yahoo", "target_url": SEARCH_URLS["yahoo"], "persist_to_db": False}

    def state():
        return next(row for row in scrape_health.list_scrape_health()
                    if row["site"] == "yahoo" and row["route"] == "search")

    for _ in range(2):
        scrape_tasks.execute_scrape_job(request)
    assert state()["consecutive_failures"] == 0
    assert state()["incident_open"] is False
    assert state()["last_success_at"] is None
    for _ in range(2):
        scrape_health.record_scrape_observation(site="yahoo", route="search", outcome="failure", reason="fetch_error")
    scrape_tasks.execute_scrape_job(request)
    assert state()["consecutive_failures"] == 2
    assert state()["incident_open"] is True
    assert state()["last_success_at"] is None


def test_search_result_metadata_is_list_compatible_and_only_claims_empty_for_empty_items():
    reason = require_search_outcome("yahoo", candidate_count=0, text="検索結果がありません")
    items = SearchResult([], end_reason=reason)
    assert isinstance(items, list) and items == []
    assert items.end_reason == "explicit_empty"
    assert SearchResult([], end_reason="https://example.com/private").end_reason == "unknown"
    assert SearchResult([{"title": "result"}], end_reason=reason).end_reason == "unknown"
    assert require_search_outcome("yahoo", candidate_count=1, text="検索結果がありません") == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["mercari", "rakuma"])
async def test_verified_empty_survives_sync_wrapper_worker_thread(monkeypatch, site):
    # Calling a sync scraper while this loop runs exercises run_coro_sync's
    # worker-thread transport, not just its ordinary asyncio.run path.
    stub_listing(monkeypatch, site, "検索結果がありません")
    observed = []
    monkeypatch.setattr(scrape_tasks, "record_observation_safely", lambda **kw: observed.append(kw))
    result = scrape_tasks.execute_scrape_job({
        "site": site, "target_url": SEARCH_URLS[site], "persist_to_db": False,
    })
    assert result["search_quality"]["end_reason"] == "explicit_empty"
    assert observed[0]["outcome"] == "no_observations"
