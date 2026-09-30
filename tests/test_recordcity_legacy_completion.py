"""The legacy detail crawl stops at verified, filtered user demand, not overfetch."""
import pytest

import recordcity_db
from jobs.scrape_tasks import execute_scrape_job
from services import marketplace_access as access
from services.html_page_adapter import HtmlPageAdapter
from services.scrape_safety import ScrapeFailure


SEARCH = "https://www.recordcity.jp/ja/catalog?keyword=record"


def item(number, **changes):
    return {
        "url": f"https://www.recordcity.jp/ja/catalog/{number}",
        "title": f"Record {number}", "price": 2000, "status": "on_sale",
        "image_urls": [], **changes,
    }


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.delenv("RECORDCITY_LISTING_ENABLED", raising=False)
    checkpoints, observations, details = [], [], []
    listing = HtmlPageAdapter('<html>' + ''.join(
        f'<a href="/ja/catalog/{number}">Record {number}</a>' for number in range(1, 181)
    ) + '</html>')

    def fetch(url, kind):
        assert kind == "search"
        access._check_active(consume=True)
        return listing

    def detail(url):
        access._check_active(consume=True)
        number = int(url.rsplit("/", 1)[-1])
        details.append(number)
        return item(number)

    monkeypatch.setattr(recordcity_db, "_fetch_page", fetch)
    monkeypatch.setattr(recordcity_db, "scrape_item_detail", detail)
    monkeypatch.setattr("jobs.scrape_tasks.filter_excluded_items", lambda items, user_id: (items, 0))
    monkeypatch.setattr("jobs.scrape_tasks.checkpoint_current_job", lambda result, state: checkpoints.append((result, state)))
    monkeypatch.setattr("jobs.scrape_tasks._record_task_observation", lambda **kw: observations.append(kw))
    return checkpoints, observations, details, detail


def request(limit, **changes):
    return {"site": "recordcity", "target_url": SEARCH, "limit": limit,
            "persist_to_db": False, **changes}


def test_user_100_finishes_before_surplus_fetch_could_expire_budget(monkeypatch, offline):
    checkpoints, observations, details, base_detail = offline

    def detail(url):
        result = base_detail(url)
        if len(details) == 101:
            # An unnecessary slow 101st detail previously expired the job even
            # though its checkpoint already held all 100 requested products.
            access._budget.get().started_at -= 901
            access._check_active()
        return result

    monkeypatch.setattr(recordcity_db, "scrape_item_detail", detail)
    with access.request_budget(max_requests=120, max_seconds=900):
        result = execute_scrape_job(request(100))
    assert len(details) == len(result["items"]) == 100
    assert result["search_quality"]["completion_verified"] is True
    assert checkpoints[-1][1]["end_reason"] == "requested_reached"
    assert observations[-1]["outcome"] == "success"


def test_completion_counts_after_exclusion_and_price_filters(monkeypatch, offline):
    checkpoints, _, details, base_detail = offline

    def exclude(items, user_id):
        kept = [entry for entry in items if int(entry["url"].rsplit("/", 1)[-1]) > 2]
        return kept, len(items) - len(kept)

    def detail(url):
        result = base_detail(url)
        if int(url.rsplit("/", 1)[-1]) in {3, 4, 5}:
            result["price"] = 500
        return result

    monkeypatch.setattr("jobs.scrape_tasks.filter_excluded_items", exclude)
    monkeypatch.setattr(recordcity_db, "scrape_item_detail", detail)
    result = execute_scrape_job(request(20, price_min=1000))
    assert len(details) == 25
    assert len(result["items"]) == 20
    assert result["excluded_count"] == 5
    assert result["search_quality"]["completion_verified"] is True
    assert checkpoints[-1][1]["end_reason"] == "requested_reached"


@pytest.mark.parametrize("changes", [
    {"status": "unknown"}, {"price": None},
    {"url": "https://foreign.example/catalog/1"},
])
def test_invalid_displayed_row_cannot_stop_internal_overfetch(monkeypatch, offline, changes):
    checkpoints, observations, details, base_detail = offline

    def detail(url):
        result = base_detail(url)
        if len(details) == 1:
            result.update(changes)
        return result

    monkeypatch.setattr(recordcity_db, "scrape_item_detail", detail)
    result = execute_scrape_job(request(20))
    assert len(details) == 30  # Original internal limit, not the user's 20.
    assert result["search_quality"]["completion_verified"] is False
    assert observations[-1]["outcome"] == "failure"
    assert checkpoints[-1][1]["end_reason"] != "requested_reached"


def test_schema_failure_does_not_count_toward_user_completion(monkeypatch, offline):
    checkpoints, _, details, base_detail = offline

    def detail(url):
        result = base_detail(url)
        if len(details) == 1:
            raise ScrapeFailure("fixture schema unavailable")
        return result

    monkeypatch.setattr(recordcity_db, "scrape_item_detail", detail)
    result = execute_scrape_job(request(20))
    assert len(details) == 21 and len(result["items"]) == 20
    assert checkpoints[-1][1]["detail_error_count"] == 1
    assert checkpoints[-1][1]["end_reason"] == "requested_reached"


def test_expiry_before_desired_count_remains_failure_with_partial(monkeypatch, offline):
    checkpoints, observations, details, base_detail = offline

    def detail(url):
        result = base_detail(url)
        if len(details) == 6:
            access._budget.get().started_at -= 901
            access._check_active()
        return result

    monkeypatch.setattr(recordcity_db, "scrape_item_detail", detail)
    with access.request_budget(max_requests=120, max_seconds=900):
        with pytest.raises(access.AccessBudgetExceeded):
            execute_scrape_job(request(100))
    assert len(details) == 6
    assert len(checkpoints[-1][0]["items"]) == 5
    assert checkpoints[-1][0]["search_quality"]["completion_verified"] is False
    assert observations[-1]["outcome"] == "failure"


@pytest.mark.parametrize("callback_result", [None, 1, {"complete": True}])
def test_direct_adapter_only_stops_for_explicit_true(monkeypatch, offline, callback_result):
    _, _, details, _ = offline
    result = recordcity_db.scrape_search_result(SEARCH, max_items=30,
        progress_callback=lambda *args: callback_result)
    assert len(details) == len(result) == 30
