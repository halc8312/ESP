"""List-first requests cannot expand another site's detail crawl or infer stock."""
import pytest

from jobs.scrape_tasks import execute_scrape_job
from models import PriceList, PriceListItem, Product, ScrapeJob, TranslationSuggestion, User
from services.product_service import save_scraped_items_to_db
from services.scrape_job_store import create_job_record
from services.scrape_request import build_scrape_task_request, build_scrape_job_context
from services.search_result_quality import build_search_quality, inspect_search_quality


def _request(site="recordcity", target_url="", limit=500):
    return build_scrape_task_request(
        site, target_url, "jazz", None, None, "", None, limit, 1,
        persist_to_db=False,
    )


def _card(source_id=1001, **overrides):
    return {
        "url": f"https://www.recordcity.jp/ja/catalog/{source_id}",
        "source_id": str(source_id), "title": "Record", "price": 1500,
        "currency": "JPY", "description": "", "status": "unknown",
        "image_urls": ["https://files.recordcity.jp/image/record.jpg"],
        "_listing_card": True, **overrides,
    }


@pytest.fixture(autouse=True)
def _offline_and_opt_in(monkeypatch):
    monkeypatch.delenv("RECORDCITY_LISTING_ENABLED", raising=False)
    monkeypatch.setattr("jobs.scrape_tasks.filter_excluded_items", lambda items, user_id: (items, 0))
    monkeypatch.setattr("jobs.scrape_tasks.filter_items_by_price", lambda items, **kwargs: (items, 0))
    monkeypatch.setattr("jobs.scrape_tasks._record_task_observation", lambda **kwargs: None)


def test_list_first_is_disabled_by_default_and_cannot_be_posted_into_payload():
    payload = _request()
    assert payload["limit"] == 100
    assert payload["acquisition_mode"] == "detail"


@pytest.mark.parametrize("site", ["mercari", "rakuma", "yahoo", "snkrdunk", "surugaya"])
def test_other_site_requests_stay_bounded_when_recordcity_is_enabled(monkeypatch, site):
    monkeypatch.setenv("RECORDCITY_LISTING_ENABLED", "true")
    payload = _request(site=site)
    assert payload["limit"] == 100
    assert payload["acquisition_mode"] == "detail"


def test_server_selects_effective_site_and_keeps_direct_item_reads(monkeypatch):
    monkeypatch.setenv("RECORDCITY_LISTING_ENABLED", "true")
    search = _request(site="mercari", target_url="https://recordcity.jp/ja/catalog?condition=new")
    assert (search["site"], search["limit"], search["acquisition_mode"]) == ("recordcity", 500, "listing")
    detail = _request(target_url="https://recordcity.jp/ja/catalog/1001")
    assert (detail["limit"], detail["acquisition_mode"]) == (1, "detail")
    context = build_scrape_job_context("recordcity", "", "jazz", 900, False)
    assert context["limit"] == 500
    assert context["acquisition_mode"] == "listing"


def test_worker_uses_shallow_adapter_preserves_filters_and_checkpoints(monkeypatch):
    monkeypatch.setenv("RECORDCITY_LISTING_ENABLED", "true")
    target = "https://www.recordcity.jp/ja/catalog?narrow_down_17=5000-11000&condition=new&page=2"
    first, second = _card(), _card(1002)
    checkpoints = []

    def listing(**kwargs):
        assert kwargs["search_url"] == target
        assert kwargs["max_items"] == 500
        assert kwargs["max_pages"] <= 6
        kwargs["progress_callback"]([first, second], {
            "phase": "listing", "pages_fetched": 1, "end_reason": "listing_exhausted",
            "processed_count": 2, "candidates_count": 2, "detail_error_count": 0,
        })
        return [first, second]

    monkeypatch.setattr("jobs.scrape_tasks.recordcity_db.scrape_listing_result", listing)
    monkeypatch.setattr("jobs.scrape_tasks.recordcity_db.scrape_search_result", lambda **kwargs: pytest.fail("detail crawl"))
    monkeypatch.setattr("jobs.scrape_tasks.recordcity_db.scrape_single_item", lambda **kwargs: pytest.fail("detail fetch"))
    monkeypatch.setattr("jobs.scrape_tasks.save_scraped_items_to_db", lambda *args, **kwargs: pytest.fail("preview persisted"))
    monkeypatch.setattr("jobs.scrape_tasks.checkpoint_current_job", lambda result, progress: checkpoints.append((result, progress)))

    result = execute_scrape_job(_request(target_url=target))
    assert result["acquisition_mode"] == "listing"
    assert result["search_quality"]["valid_count"] == 2
    assert result["search_quality"]["acquisition_rate"] == 2 / 500
    assert result["search_quality"]["completion_verified"] is True
    assert checkpoints[0][0]["items"] == [first, second]
    assert checkpoints[0][1]["items_count"] == 2
    assert result["items"][0]["status"] == "unknown"


def test_gate_disabled_worker_does_not_honor_injected_listing_mode(monkeypatch):
    calls = []
    monkeypatch.setattr("jobs.scrape_tasks.recordcity_db.scrape_search_result", lambda **kwargs: calls.append(kwargs) or [])
    monkeypatch.setattr("jobs.scrape_tasks.recordcity_db.scrape_listing_result", lambda **kwargs: pytest.fail("gate bypass"))
    payload = {**_request(), "acquisition_mode": "listing", "limit": 500}
    result = execute_scrape_job(payload)
    assert result["limit"] == 100
    assert result["acquisition_mode"] == "detail"
    assert calls[0]["max_items"] <= 150


def test_unknown_stock_is_valid_card_without_becoming_verified_inventory():
    item = _card()
    quality = build_search_quality([item], requested_count=1, site="recordcity", acquisition_mode="listing")
    assert quality["completion_verified"] is True
    assert inspect_search_quality([item], quality)["outcome"] == "success"
    assert item["status"] == "unknown"
    legacy = build_search_quality([item], requested_count=1, site="recordcity")
    assert legacy["valid_count"] == 0


def test_missing_card_and_adapter_rejections_cannot_close_incidents():
    item = _card()
    quality = build_search_quality(
        [item], requested_count=1, site="recordcity", acquisition_mode="listing",
        progress={"invalid_card_count": 2, "duplicate_count": 3},
    )
    assert quality["invalid_card_count"] == 2
    assert quality["duplicate_count"] == 3
    assert quality["completion_verified"] is False
    assert inspect_search_quality([item], quality) == {
        "outcome": "failure", "reason": "invalid_result", "success_count": 1, "error_count": 2,
    }
    bad = _card(image_urls=[])
    bad_quality = build_search_quality([bad], requested_count=1, site="recordcity", acquisition_mode="listing")
    assert bad_quality["valid_count"] == 0
    assert bad_quality["completion_verified"] is False


def test_direct_post_limit_and_acquisition_mode_are_server_owned(client, db_session, monkeypatch):
    monkeypatch.setenv("RECORDCITY_LISTING_ENABLED", "true")
    user = User(username="listing_request_owner")
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    client.post("/login", data={"username": user.username, "password": "testpassword"})
    calls = []

    class Queue:
        def enqueue(self, **kwargs):
            calls.append(kwargs)
            return "listing-job"

    monkeypatch.setattr("routes.scrape.get_queue", lambda: Queue())
    response = client.post("/scrape/run", data={
        "site": "mercari", "keyword": "jazz", "limit": "500",
        "acquisition_mode": "listing", "response_mode": "preview",
    })
    assert response.status_code == 202
    assert calls[-1]["request_payload"]["limit"] == 100
    assert calls[-1]["request_payload"]["acquisition_mode"] == "detail"
    response = client.post("/scrape/run", data={
        "target_url": "https://recordcity.jp/ja/catalog?condition=new", "limit": "999",
        "acquisition_mode": "detail", "response_mode": "preview",
    })
    assert response.status_code == 202
    assert calls[-1]["request_payload"]["limit"] == 500
    assert calls[-1]["request_payload"]["acquisition_mode"] == "listing"


def test_ui_hides_large_requests_until_adapter_is_enabled(client, db_session, monkeypatch):
    user = User(username="listing_ui_owner")
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    client.post("/login", data={"username": user.username, "password": "testpassword"})
    assert b'value="500"' not in client.get("/scrape").data
    monkeypatch.setenv("RECORDCITY_LISTING_ENABLED", "true")
    html = client.get("/scrape").get_data(as_text=True)
    assert 'value="500" data-listing-limit disabled hidden' in html
    assert 'data-recordcity-listing-enabled="true"' in html


@pytest.mark.parametrize("register_to_list", [False, True])
@pytest.mark.parametrize("translate", [False, True])
def test_registration_queues_only_selected_cards_and_defers_translation(
    client, db_session, monkeypatch, register_to_list, translate,
):
    user = User(username="listing_registration_owner")
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    client.post("/login", data={"username": user.username, "password": "testpassword"})

    class Queue:
        def get_status(self, job_id, user_id=None):
            if job_id != "listing-job" or user_id != user.id:
                return None
            return {"status": "completed", "result": {
                "items": [_card(), _card(1002)], "site": "recordcity",
                "acquisition_mode": "listing", "shop_id": None,
            }}

    calls = []
    monkeypatch.setattr("routes.scrape.get_queue", lambda: Queue())
    monkeypatch.setattr("routes.scrape._enqueue_details_for_registration", lambda ids, uid, **kwargs: calls.append((ids, uid, kwargs)) or {"queued": len(ids), "failed": 0})
    monkeypatch.setattr("routes.scrape._run_translation_inline", lambda *args: pytest.fail("premature translation"))
    payload = {"job_id": "listing-job", "selected_indices": [1], "translate": translate}
    if register_to_list:
        payload["new_list_name"] = "Selected records"
    endpoint = "/scrape/register-to-pricelist" if register_to_list else "/scrape/register-selected"
    response = client.post(endpoint, json=payload)
    assert response.status_code == 200
    assert response.json["registered_count"] == 1
    assert response.json["detail_jobs_enqueued"] == 1
    assert response.json["translation_jobs_enqueued"] == 0
    db_session.expire_all()
    products = db_session.query(Product).filter_by(user_id=user.id).all()
    assert len(products) == 1
    assert products[0].source_url.endswith("/1002")
    assert products[0].detail_fetch_state == "pending"
    assert products[0].last_status == "unknown"
    assert products[0].is_listed is (not register_to_list)
    assert calls == [([products[0].id], user.id, {"shop_id": None, "translate": translate})]
    assert db_session.query(TranslationSuggestion).count() == 0


def test_checkpoint_keeps_list_cards_recoverable_after_a_later_failure(monkeypatch):
    monkeypatch.setenv("RECORDCITY_LISTING_ENABLED", "true")
    checkpoints = []

    def listing(**kwargs):
        kwargs["progress_callback"]([_card()], {"phase": "listing", "pages_fetched": 1})
        raise RuntimeError("second page failed")

    monkeypatch.setattr("jobs.scrape_tasks.recordcity_db.scrape_listing_result", listing)
    monkeypatch.setattr("jobs.scrape_tasks.checkpoint_current_job", lambda result, progress: checkpoints.append((result, progress)))
    with pytest.raises(RuntimeError, match="second page failed"):
        execute_scrape_job(_request())
    result, progress = checkpoints[-1]
    assert result["items"] == [_card()]
    assert result["acquisition_mode"] == "listing"
    assert result["search_quality"]["valid_count"] == 1
    assert progress["items_count"] == 1


def test_price_list_add_queues_owned_shallow_product_once(client, db_session, monkeypatch):
    owner, other = User(username="listing_price_owner"), User(username="listing_price_other")
    owner.set_password("testpassword")
    other.set_password("testpassword")
    db_session.add_all([owner, other])
    db_session.commit()
    owner_id, other_id = owner.id, other.id
    owned = save_scraped_items_to_db([_card()], owner_id, site="recordcity", return_summary=True)
    foreign = save_scraped_items_to_db([_card(1002)], other_id, site="recordcity", return_summary=True)
    price_list = PriceList(user_id=owner_id, name="Records", token="listing-price-test")
    db_session.add(price_list)
    db_session.commit()
    price_list_id = price_list.id
    client.post("/login", data={"username": "listing_price_owner", "password": "testpassword"})
    dispatches = []
    monkeypatch.setattr("services.product_detail_jobs._dispatch_detail_job", lambda *args: dispatches.append(args))
    product_id = owned["product_ids"][0]
    foreign_id = foreign["product_ids"][0]
    response = client.post(f"/pricelists/{price_list_id}/add-products", data={
        "product_ids": [str(product_id), str(product_id), str(foreign_id)],
    })
    assert response.status_code == 302
    db_session.expire_all()
    list_ids = [row.product_id for row in db_session.query(PriceListItem).filter_by(price_list_id=price_list_id).all()]
    assert list_ids == [product_id]
    assert len(dispatches) == 1
    assert dispatches[0][0:2] == (product_id, owner_id)
    assert db_session.query(Product).filter_by(id=product_id).one().detail_fetch_state == "queued"
    assert db_session.query(Product).filter_by(id=foreign_id).one().detail_fetch_state == "pending"


@pytest.mark.parametrize("response_mode", ["preview", ""])
def test_full_queue_is_readable_and_does_not_create_or_dispatch_job(
    client, db_session, monkeypatch, response_mode,
):
    owner = User(username="full_queue_owner")
    owner.set_password("testpassword")
    db_session.add(owner)
    db_session.commit()
    owner_id = owner.id
    client.post("/login", data={"username": owner.username, "password": "testpassword"})
    monkeypatch.setenv("SCRAPE_MAX_ACTIVE_JOBS_PER_USER", "1")
    create_job_record("already-running", "recordcity", user_id=owner_id)

    class Queue:
        def enqueue(self, **kwargs):
            create_job_record("must-not-be-created", kwargs["site"], user_id=kwargs["user_id"])
            pytest.fail("admission should fail before dispatch")

    monkeypatch.setattr("routes.scrape.get_queue", lambda: Queue())
    response = client.post("/scrape/run", data={
        "site": "recordcity", "keyword": "jazz", "response_mode": response_mode,
    })
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "30"
    assert response.json == {
        "error": "実行中・待機中の商品取得が多いため、少し待ってから再実行してください。",
        "kind": "queue_full",
    }
    assert "少し待って" in response.get_data(as_text=True)
    assert "already-running" not in response.get_data(as_text=True)
    db_session.expire_all()
    assert [job.job_id for job in db_session.query(ScrapeJob).all()] == ["already-running"]
