from datetime import timedelta

import pytest

from database import SessionLocal
from jobs.scrape_tasks import execute_scrape_job
from models import ScrapeJob
from services.scrape_request import build_scrape_task_request
from services.scrape_job_runtime import run_tracked_job
from services.scrape_job_store import create_job_record, get_job_record, mark_job_running, maybe_mark_job_stalled


def test_run_tracked_job_marks_job_completed(app):
    create_job_record(
        job_id="runtime-job-1",
        site="mercari",
        context={"persist_to_db": False},
        request_payload={"site": "mercari", "persist_to_db": False},
        mode="preview",
    )

    result = run_tracked_job("runtime-job-1", lambda: {"items": [{"title": "ok"}], "persist_to_db": False})

    stored = get_job_record("runtime-job-1")
    assert result["items"][0]["title"] == "ok"
    assert stored["status"] == "completed"
    assert stored["result"]["items"][0]["title"] == "ok"


def test_run_tracked_job_marks_job_failed(app):
    create_job_record(
        job_id="runtime-job-2",
        site="mercari",
        context={"persist_to_db": False},
        request_payload={"site": "mercari", "persist_to_db": False},
        mode="preview",
    )

    with pytest.raises(RuntimeError, match="boom"):
        run_tracked_job("runtime-job-2", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    stored = get_job_record("runtime-job-2")
    assert stored["status"] == "failed"
    assert stored["error"] == "boom"


def test_run_tracked_scrape_failure_is_not_marked_completed(app, monkeypatch):
    create_job_record(
        job_id="runtime-job-scrape-failure",
        site="mercari",
        context={"persist_to_db": False},
        request_payload={"site": "mercari", "persist_to_db": False},
        mode="preview",
    )
    monkeypatch.setattr(
        "jobs.scrape_tasks.scrape_search_result",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("network unavailable")),
    )
    request_payload = build_scrape_task_request(
        site="mercari",
        target_url="",
        keyword="retry-me",
        price_min=None,
        price_max=None,
        sort="created_desc",
        category=None,
        limit=2,
        user_id=1,
        persist_to_db=False,
        shop_id=None,
    )

    with pytest.raises(RuntimeError, match="network unavailable"):
        run_tracked_job("runtime-job-scrape-failure", execute_scrape_job, request_payload)

    stored = get_job_record("runtime-job-scrape-failure")
    assert stored["status"] == "failed"
    assert stored["result"] is None
    assert stored["error"] == "network unavailable"


def test_maybe_mark_job_stalled_converts_old_running_job(app):
    create_job_record(
        job_id="runtime-job-3",
        site="mercari",
        context={"persist_to_db": False},
        request_payload={"site": "mercari", "persist_to_db": False},
        mode="preview",
    )
    mark_job_running("runtime-job-3")

    session = SessionLocal()
    try:
        record = session.query(ScrapeJob).filter_by(job_id="runtime-job-3").one()
        stale_time = record.updated_at - timedelta(seconds=1200)
        record.updated_at = stale_time
        record.started_at = stale_time
        session.commit()
    finally:
        session.close()

    assert maybe_mark_job_stalled("runtime-job-3", stall_timeout_seconds=60) is True

    stored = get_job_record("runtime-job-3")
    assert stored["status"] == "failed"
    assert stored["error_payload"]["kind"] == "job_stalled"
    assert "停止" in stored["error"]


def _recordcity_request(**overrides):
    return {
        "site": "recordcity", "target_url": "https://www.recordcity.jp/catalog?search=record",
        "limit": 3, "persist_to_db": False, **overrides,
    }


def _recordcity_item(number=1, price=1000):
    return {"url": f"https://www.recordcity.jp/catalog/{number}", "title": f"Record {number}",
            "price": price, "status": "on_sale", "image_urls": []}


def test_recordcity_partial_snapshot_survives_failure_without_product_registration(app, monkeypatch):
    request = _recordcity_request(persist_to_db=True, price_min=500)
    create_job_record("partial-job", "recordcity", request_payload=request)
    snapshots = []
    save_calls = []

    def scraper(**kwargs):
        kwargs["progress_callback"]([_recordcity_item(), _recordcity_item(), _recordcity_item(2, 100)],
                                     {"phase": "details", "pages_fetched": 1, "processed_count": 3})
        snapshots.append(get_job_record("partial-job"))
        raise RuntimeError("network failure")

    monkeypatch.setattr("jobs.scrape_tasks.recordcity_db.scrape_search_result", scraper)
    monkeypatch.setattr("jobs.scrape_tasks.save_scraped_items_to_db", lambda *a, **kw: save_calls.append(a))
    with pytest.raises(RuntimeError, match="network failure"):
        run_tracked_job("partial-job", execute_scrape_job, request)

    stored = get_job_record("partial-job")
    assert snapshots[0]["status"] == "running"
    assert stored["status"] == "failed"
    assert stored["result"]["partial"] is True
    assert stored["result"]["items"] == [_recordcity_item()]
    assert stored["context"]["progress"]["items_count"] == 1
    from datetime import datetime, timezone
    progress_time = stored["context"]["progress"]["updated_at"]
    assert progress_time.endswith("Z")
    assert datetime.fromisoformat(progress_time).tzinfo == timezone.utc
    assert stored["context"]["progress"]["requested_count"] == 3
    assert stored["result"]["excluded_count"] == 1
    quality = stored["result"]["search_quality"]
    assert (quality["requested_count"], quality["unique_count"], quality["valid_count"]) == (3, 2, 2)
    assert quality["duplicate_count"] == 1
    assert quality["excluded_count"] == quality["displayed_count"] == 1
    assert quality["completion_verified"] is False
    assert not save_calls


def test_recordcity_success_persists_once_after_checkpoints(app, monkeypatch):
    request = _recordcity_request(persist_to_db=True)
    create_job_record("partial-complete", "recordcity", request_payload=request)
    saves = []
    def scraper(**kwargs):
        kwargs["progress_callback"]([_recordcity_item()], {"phase": "details"})
        kwargs["progress_callback"]([_recordcity_item(), _recordcity_item(2)], {"phase": "details"})
        return [_recordcity_item(), _recordcity_item(2)]
    def save(items, **kwargs):
        saves.append(items)
        return (2, 0)
    monkeypatch.setattr("jobs.scrape_tasks.recordcity_db.scrape_search_result", scraper)
    monkeypatch.setattr("jobs.scrape_tasks.save_scraped_items_to_db", save)
    run_tracked_job("partial-complete", execute_scrape_job, request)
    assert len(saves) == 1
    stored = get_job_record("partial-complete")
    assert stored["status"] == "completed"
    assert not stored["result"].get("partial")
    assert stored["result"]["new_count"] == 2


def test_late_checkpoint_does_not_overwrite_terminal_failure_or_duplicate_observation(app, monkeypatch):
    from services.scrape_job_runtime import ScrapeJobAlreadyTerminated
    from services.scrape_job_store import mark_job_failed
    request = _recordcity_request()
    create_job_record("partial-fenced", "recordcity", request_payload=request)
    observations = []
    monkeypatch.setattr("services.scrape_observation.record_observation_safely", lambda **kw: observations.append(kw))
    def scraper(**kwargs):
        kwargs["progress_callback"]([_recordcity_item()], {"phase": "details"})
        mark_job_failed("partial-fenced", "watchdog failure")
        kwargs["progress_callback"]([_recordcity_item(), _recordcity_item(2)], {"phase": "details"})
        pytest.fail("terminated callback must stop further fetches")
    monkeypatch.setattr("jobs.scrape_tasks.recordcity_db.scrape_search_result", scraper)
    with pytest.raises(ScrapeJobAlreadyTerminated):
        run_tracked_job("partial-fenced", execute_scrape_job, request)
    stored = get_job_record("partial-fenced")
    assert stored["status"] == "failed"
    assert stored["error"] == "watchdog failure"
    assert len(stored["result"]["items"]) == 1
    assert observations == []


def test_heartbeat_retries_transient_storage_error_without_claiming_progress(monkeypatch):
    import services.scrape_job_runtime as runtime
    from unittest.mock import Mock
    callback = {}
    event = Mock()
    event.wait.side_effect = [False, False, True]
    thread = Mock()
    def make_thread(**kwargs):
        callback["target"] = kwargs["target"]
        return thread
    heartbeat = Mock(side_effect=[RuntimeError("database unavailable"), None])
    monkeypatch.setattr(runtime.threading, "Event", lambda: event)
    monkeypatch.setattr(runtime.threading, "Thread", make_thread)
    monkeypatch.setattr(runtime, "mark_job_heartbeat", heartbeat)
    runtime._start_heartbeat("retry-heartbeat")
    callback["target"]()
    assert heartbeat.call_count == 2


def test_late_scraper_return_does_not_register_or_reset_health(app, monkeypatch):
    from services.scrape_job_runtime import ScrapeJobAlreadyTerminated
    from services.scrape_job_store import mark_job_failed
    request = _recordcity_request(persist_to_db=True)
    create_job_record("late-return", "recordcity", request_payload=request)
    saves = []
    observations = []
    monkeypatch.setattr("services.scrape_observation.record_observation_safely", lambda **kw: observations.append(kw))
    monkeypatch.setattr("jobs.scrape_tasks.save_scraped_items_to_db", lambda *a, **kw: saves.append(a))
    def scraper(**kwargs):
        kwargs["progress_callback"]([_recordcity_item()], {"phase": "details"})
        mark_job_failed("late-return", "watchdog", observe_reason="job_stalled")
        return [_recordcity_item()]
    monkeypatch.setattr("jobs.scrape_tasks.recordcity_db.scrape_search_result", scraper)
    with pytest.raises(ScrapeJobAlreadyTerminated):
        run_tracked_job("late-return", execute_scrape_job, request)
    assert saves == []
    assert [o["reason"] for o in observations] == ["job_stalled"]
    assert get_job_record("late-return")["result"]["partial"] is True


def test_tracked_success_observation_only_after_durable_completion(app, monkeypatch):
    request = _recordcity_request()
    create_job_record("observed-completion", "recordcity", request_payload=request)
    statuses = []
    def observe(**kwargs):
        statuses.append(get_job_record("observed-completion")["status"])
    monkeypatch.setattr("services.scrape_observation.record_observation_safely", observe)
    monkeypatch.setattr("jobs.scrape_tasks.recordcity_db.scrape_search_result", lambda **kw: [_recordcity_item()])
    run_tracked_job("observed-completion", execute_scrape_job, request)
    assert statuses == ["completed"]


def test_late_scraper_error_does_not_duplicate_watchdog_failure(app, monkeypatch):
    from services.scrape_job_store import mark_job_failed
    request = _recordcity_request()
    create_job_record("late-error", "recordcity", request_payload=request)
    observations = []
    monkeypatch.setattr("services.scrape_observation.record_observation_safely", lambda **kw: observations.append(kw))
    def scraper(**kwargs):
        mark_job_failed("late-error", "watchdog", observe_reason="job_stalled")
        raise RuntimeError("late fetch failure")
    monkeypatch.setattr("jobs.scrape_tasks.recordcity_db.scrape_search_result", scraper)
    with pytest.raises(RuntimeError, match="late fetch failure"):
        run_tracked_job("late-error", execute_scrape_job, request)
    assert [o["reason"] for o in observations] == ["job_stalled"]
    assert get_job_record("late-error")["error"] == "watchdog"
