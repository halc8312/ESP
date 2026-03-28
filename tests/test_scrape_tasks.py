from jobs.scrape_tasks import execute_scrape_job
from services.scrape_request import build_scrape_task_request


def test_execute_scrape_job_returns_filtered_result(monkeypatch):
    def fake_scrape_search_result(search_url, max_items, max_scroll, headless):
        assert "keyword=preview" in search_url
        assert max_items >= 2
        assert max_scroll >= 3
        assert headless is True
        return [
            {
                "url": "https://jp.mercari.com/item/m-1",
                "title": "Preview Item",
                "price": 1200,
                "image_urls": [],
            }
        ]

    monkeypatch.setattr("jobs.scrape_tasks.scrape_search_result", fake_scrape_search_result)
    monkeypatch.setattr("jobs.scrape_tasks.filter_excluded_items", lambda items, user_id: (items, 0))
    monkeypatch.setattr("jobs.scrape_tasks.filter_items_by_price", lambda items, price_min, price_max: (items, 0))
    monkeypatch.setattr("jobs.scrape_tasks.save_scraped_items_to_db", lambda *args, **kwargs: (0, 0))

    request_payload = build_scrape_task_request(
        site="mercari",
        target_url="",
        keyword="preview",
        price_min=None,
        price_max=None,
        sort="created_desc",
        category=None,
        limit=2,
        user_id=1,
        persist_to_db=False,
        shop_id=None,
    )

    result = execute_scrape_job(request_payload)

    assert result["error_msg"] == ""
    assert result["persist_to_db"] is False
    assert result["items"][0]["title"] == "Preview Item"
    assert result["search_url"].startswith("https://jp.mercari.com/search?")


def test_execute_scrape_job_converts_exceptions_to_error_payload(monkeypatch):
    monkeypatch.setattr(
        "jobs.scrape_tasks.scrape_search_result",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr("jobs.scrape_tasks.filter_excluded_items", lambda items, user_id: (items, 0))
    monkeypatch.setattr("jobs.scrape_tasks.filter_items_by_price", lambda items, price_min, price_max: (items, 0))

    request_payload = build_scrape_task_request(
        site="mercari",
        target_url="",
        keyword="preview",
        price_min=None,
        price_max=None,
        sort="created_desc",
        category=None,
        limit=2,
        user_id=1,
        persist_to_db=False,
        shop_id=None,
    )

    result = execute_scrape_job(request_payload)

    assert result["items"] == []
    assert result["new_count"] == 0
    assert result["updated_count"] == 0
    assert result["error_msg"] == "boom"


def test_execute_scrape_job_uses_internal_smoke_payload(monkeypatch):
    save_calls = []

    monkeypatch.setattr("jobs.scrape_tasks.filter_excluded_items", lambda items, user_id: (items, 0))
    monkeypatch.setattr("jobs.scrape_tasks.filter_items_by_price", lambda items, price_min, price_max: (items, 0))
    monkeypatch.setattr(
        "jobs.scrape_tasks.save_scraped_items_to_db",
        lambda items, site, user_id, shop_id: save_calls.append((items, site, user_id, shop_id)) or (1, 0),
    )

    request_payload = build_scrape_task_request(
        site="mercari",
        target_url="",
        keyword="preview",
        price_min=None,
        price_max=None,
        sort="created_desc",
        category=None,
        limit=2,
        user_id=1,
        persist_to_db=True,
        shop_id=3,
    )
    request_payload["__smoke_result"] = {
        "site": "mercari",
        "items": [
            {
                "url": "https://jp.mercari.com/item/m-stack-smoke-1",
                "title": "Stack Smoke Item",
                "price": 2222,
                "image_urls": [],
            }
        ],
        "search_url": "internal://stack-smoke/test",
    }

    result = execute_scrape_job(request_payload)

    assert result["error_msg"] == ""
    assert result["items"][0]["title"] == "Stack Smoke Item"
    assert result["search_url"] == "internal://stack-smoke/test"
    assert result["new_count"] == 1
    assert save_calls[0][1:] == ("mercari", 1, 3)
