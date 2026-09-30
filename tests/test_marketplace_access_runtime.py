"""Admission waits and job limits must preserve recoverable partial products."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from database import SessionLocal
from models import Product, ScrapeJobEvent, User
from services import marketplace_access as access
from services import scrape_job_runtime as runtime
from services.scrape_job_store import (
    create_job_record,
    get_job_record,
    mark_job_failed,
    mark_job_progress,
    mark_job_running,
    mark_job_wait,
)


def create(job_id):
    create_job_record(job_id, "recordcity", context={"shop_id": 7},
                      request_payload={"site": "recordcity", "persist_to_db": False})


def partial(job_id):
    result = {"items": [{"url": "https://www.recordcity.jp/catalog/123", "title": "Record"}],
              "persist_to_db": False, "new_count": 0, "updated_count": 0}
    progress = {"phase": "listing", "items_count": 1, "processed_count": 3,
                "requested_count": 500, "pages_fetched": 1}
    assert mark_job_progress(job_id, result, progress)


def test_wait_without_products_is_only_a_heartbeat(app):
    create("waiting-empty")
    assert mark_job_wait("waiting-empty", "site_busy", 1) is False
    assert mark_job_running("waiting-empty")
    before = get_job_record("waiting-empty")
    assert mark_job_wait("waiting-empty", "site_busy", 1.2)
    after = get_job_record("waiting-empty")
    assert after["status"] == "running"
    assert after["result"] is before["result"] is None
    assert after["result_summary"] is None
    assert after["context"] == {
        "shop_id": 7, "progress": {"wait_reason": "site_busy", "retry_after_seconds": 2}}
    assert after["updated_at"] >= before["updated_at"]


def test_wait_and_clear_preserve_partial_counters_timestamp_and_events(app):
    create("waiting-partial")
    mark_job_running("waiting-partial")
    partial("waiting-partial")
    before = deepcopy(get_job_record("waiting-partial"))
    session = SessionLocal()
    try:
        event_count = session.query(ScrapeJobEvent).filter_by(job_id="waiting-partial").count()
    finally:
        session.close()

    for reason, delay in (("site_cooldown", 61.1), ("site_interval", 2), ("", 0)):
        assert mark_job_wait("waiting-partial", reason, delay)
        after = get_job_record("waiting-partial")
        assert after["result"] == before["result"]
        assert after["result_summary"] == before["result_summary"]
        assert after["context"]["shop_id"] == 7
        for key, value in before["context"]["progress"].items():
            assert after["context"]["progress"][key] == value
        assert after["context"]["progress"]["wait_reason"] == reason
    session = SessionLocal()
    try:
        assert session.query(ScrapeJobEvent).filter_by(job_id="waiting-partial").count() == event_count
        assert session.query(Product).count() == 0
    finally:
        session.close()


def test_terminal_job_rejects_late_wait_and_keeps_partial(app):
    create("waiting-terminal")
    mark_job_running("waiting-terminal")
    partial("waiting-terminal")
    mark_job_failed("waiting-terminal", "watchdog stopped the job")
    before = deepcopy(get_job_record("waiting-terminal"))
    assert mark_job_wait("waiting-terminal", "site_busy", 10) is False
    assert get_job_record("waiting-terminal") == before
    with pytest.raises(ValueError):
        mark_job_wait("waiting-terminal", "arbitrary value", 10)
    assert get_job_record("waiting-terminal") == before


def test_wait_snapshot_remains_private_to_requested_user(app):
    session = SessionLocal()
    try:
        owner = User(username="waiting-owner")
        stranger = User(username="waiting-stranger")
        owner.set_password("test-wait-owner")
        stranger.set_password("test-wait-stranger")
        session.add_all([owner, stranger])
        session.commit()
        owner_id, stranger_id = owner.id, stranger.id
    finally:
        session.close()
    create_job_record("private-wait", "recordcity", user_id=owner_id)
    mark_job_running("private-wait")
    partial("private-wait")
    mark_job_wait("private-wait", "site_cooldown", 90)
    assert get_job_record("private-wait", user_id=owner_id)["result"]["partial"] is True
    assert get_job_record("private-wait", user_id=stranger_id) is None


def test_wait_reporting_throttles_same_reason_but_immediately_clears(app, monkeypatch):
    create("reporting-wait")
    mark_job_running("reporting-wait")
    partial("reporting-wait")
    clock = [10.0]
    calls = []
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def save_wait(*args):
        calls.append(args)
        return mark_job_wait(*args)

    monkeypatch.setattr(runtime, "mark_job_wait", save_wait)
    job_token = runtime._current_job_id.set("reporting-wait")
    wait_token = runtime._last_wait_report.set(None)
    try:
        runtime.report_current_job_wait("site_busy", 1)
        clock[0] += 1
        runtime.report_current_job_wait("site_busy", 1)
        runtime.report_current_job_wait("site_cooldown", 60)
        runtime.report_current_job_wait("", 0)
        assert [args[1] for args in calls] == ["site_busy", "site_cooldown", ""]
        assert get_job_record("reporting-wait")["result"]["partial"] is True
        mark_job_failed("reporting-wait", "stopped")
        clock[0] += 3
        with pytest.raises(runtime.ScrapeJobAlreadyTerminated):
            runtime.report_current_job_wait("", 0)
    finally:
        runtime._last_wait_report.reset(wait_token)
        runtime._current_job_id.reset(job_token)


@pytest.mark.parametrize("limit", ["requests", "time"])
def test_tracked_budget_failure_keeps_partial_and_prevents_next_side_effect(app, limit):
    job_id = f"budget-{limit}"
    create(job_id)
    effects = []

    def task():
        partial(job_id)
        budget = access._budget.get()
        assert (budget.max_requests, budget.max_seconds) == (120, 900)
        if limit == "requests":
            budget.requests = 120
        else:
            budget.started_at -= 901
        with access.marketplace_access("recordcity"):
            effects.append("request")
        return {"items": []}

    with pytest.raises(access.AccessBudgetExceeded):
        runtime.run_tracked_job(job_id, task)
    after = get_job_record(job_id)
    assert after["status"] == "failed"
    assert after["result"]["partial"] is True
    assert len(after["result"]["items"]) == 1
    assert effects == []
    assert access._budget.get() is None
    assert runtime._current_job_id.get() is None


def test_finalization_guard_checks_elapsed_budget_before_persistence(app):
    create("finalize-expired")
    effects = []

    def task():
        partial("finalize-expired")
        access._budget.get().started_at -= 901
        runtime.assert_current_job_active()
        effects.append("persisted")
        return {"items": []}

    with pytest.raises(access.AccessBudgetExceeded):
        runtime.run_tracked_job("finalize-expired", task)
    assert effects == []
    assert get_job_record("finalize-expired")["result"]["partial"] is True


def test_expiry_during_product_image_work_rolls_back_before_commit(app, monkeypatch):
    from jobs.scrape_tasks import execute_scrape_job
    from services import product_service

    session = SessionLocal()
    try:
        user = User(username="save-budget-owner")
        user.set_password("test-budget-owner")
        session.add(user)
        session.commit()
        user_id = user.id
    finally:
        session.close()
    request = {
        "site": "recordcity", "target_url": "https://www.recordcity.jp/ja/catalog?search=record",
        "limit": 1, "persist_to_db": True, "user_id": user_id,
    }
    item = {"url": "https://www.recordcity.jp/ja/catalog/12345", "title": "Record",
            "price": 2000, "status": "on_sale", "image_urls": []}
    create_job_record("save-expired", "recordcity", user_id=user_id, request_payload=request)

    def scrape(**kwargs):
        kwargs["progress_callback"]([item], {"phase": "details", "processed_count": 1})
        return [item]

    def slow_image_work(urls, product_id):
        # Simulate normal image IO crossing the budget after finalize's initial
        # check, while the product row is still inside its DB transaction.
        access._budget.get().started_at -= 901
        return urls

    monkeypatch.setattr("jobs.scrape_tasks.recordcity_db.scrape_search_result", scrape)
    monkeypatch.setattr(product_service, "_cache_external_images", slow_image_work)
    with pytest.raises(access.AccessBudgetExceeded):
        runtime.run_tracked_job("save-expired", execute_scrape_job, request)
    assert get_job_record("save-expired")["status"] == "failed"
    assert get_job_record("save-expired")["result"]["partial"] is True
    session = SessionLocal()
    try:
        assert session.query(Product).filter_by(user_id=user_id).count() == 0
    finally:
        session.close()


def test_expiry_during_repricing_does_not_commit_source_price_first(app, monkeypatch):
    from services import product_service

    session = SessionLocal()
    try:
        user = User(username="reprice-budget-owner")
        user.set_password("test-reprice-owner")
        session.add(user)
        session.flush()
        product = Product(user_id=user.id, source_url="https://www.recordcity.jp/catalog/777",
                          site="recordcity", last_title="Record", last_price=1000,
                          last_status="on_sale", manual_margin_rate=20, selling_price=1200)
        session.add(product)
        session.commit()
        user_id, product_id = user.id, product.id
    finally:
        session.close()
    create("reprice-expired")
    real_reprice = product_service.update_product_selling_price

    def reprice_and_expire(*args, **kwargs):
        result = real_reprice(*args, **kwargs)
        access._budget.get().started_at -= 901
        return result

    monkeypatch.setattr(product_service, "update_product_selling_price", reprice_and_expire)

    def task():
        partial("reprice-expired")
        return product_service.save_scraped_items_to_db(
            [{"url": "https://www.recordcity.jp/catalog/777", "title": "Record",
              "price": 2000, "status": "on_sale", "image_urls": []}],
            user_id=user_id, site="recordcity", raise_on_error=True,
        )

    with pytest.raises(access.AccessBudgetExceeded):
        runtime.run_tracked_job("reprice-expired", task)
    session = SessionLocal()
    try:
        product = session.get(Product, product_id)
        assert product.last_price == 1000
        assert product.selling_price == 1200
    finally:
        session.close()


@pytest.mark.parametrize("cross_deadline_after_commit", [False, True])
def test_tracked_successful_save_keeps_transaction_when_checking_job_state(app, cross_deadline_after_commit):
    from services.product_service import save_scraped_items_to_db

    session = SessionLocal()
    try:
        user = User(username="successful-save-owner")
        user.set_password("test-success-owner")
        session.add(user)
        session.commit()
        user_id = user.id
    finally:
        session.close()
    create("successful-save")
    item = {"url": "https://www.recordcity.jp/catalog/888", "title": "Record",
            "price": 2000, "status": "on_sale", "image_urls": []}

    def task():
        created, updated = save_scraped_items_to_db(
            [item], user_id=user_id, site="recordcity", raise_on_error=True)
        if cross_deadline_after_commit:
            # Once durable persistence has succeeded, crossing the deadline
            # during return bookkeeping must not relabel it as a failed save.
            access._budget.get().started_at -= 901
        return {"items": [item], "new_count": created, "updated_count": updated}

    result = runtime.run_tracked_job("successful-save", task)
    assert result["new_count"] == 1
    assert get_job_record("successful-save")["status"] == "completed"
    session = SessionLocal()
    try:
        product = session.query(Product).filter_by(user_id=user_id).one()
        assert product.last_price == 2000
    finally:
        session.close()
