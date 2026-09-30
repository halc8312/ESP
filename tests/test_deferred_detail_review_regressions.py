"""Independent regressions for queue uncertainty and detail write fencing."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Query

from models import Product, ProductSnapshot, TranslationSuggestion, User, Variant
from services import product_detail_jobs as jobs
from services.product_service import save_scraped_items_to_db
from time_utils import utc_now


@pytest.fixture
def detail_case(db_session, monkeypatch):
    owner = User(username="fence_owner", password_hash="hash")
    other = User(username="fence_other", password_hash="hash")
    db_session.add_all([owner, other])
    db_session.flush()
    product = Product(
        user_id=owner.id, site="recordcity", source_url="https://www.recordcity.jp/ja/catalog/9021",
        last_title="Card title", last_price=1200, last_status="unknown", detail_fetch_state="pending",
        variants=[Variant(option1_value="Default Title", price=1200, inventory_qty=0)],
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(ProductSnapshot(product_id=product.id, title="Card title", price=1200, status="unknown", description=""))
    db_session.commit()
    dispatched = []
    monkeypatch.setattr(jobs, "_dispatch_detail_job", lambda *args: dispatched.append(args))
    monkeypatch.setattr(jobs, "record_observation_safely", lambda **kwargs: True)
    monkeypatch.setattr("services.product_service._cache_external_images", lambda urls, product_id: [])
    detail = {"url": product.source_url, "title": "Verified title", "price": 1500,
              "status": "on_sale", "description": "Verified detail", "image_urls": []}
    return SimpleNamespace(owner=owner, other=other, product=product, dispatched=dispatched, detail=detail)


def test_expired_queued_redis_inspection_error_never_appends_another_job(db_session, detail_case, monkeypatch):
    case = detail_case
    assert jobs.enqueue_product_details([case.product.id], case.owner.id)["queued"] == 1
    db_session.refresh(case.product)
    token = case.product.detail_job_id
    case.product.detail_lease_expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    monkeypatch.setattr(jobs, "resolve_queue_backend_name", lambda: "rq")
    monkeypatch.setattr(jobs, "resolve_redis_url", lambda: "redis://unused.invalid")
    monkeypatch.setattr("redis.Redis.from_url", lambda *args, **kwargs: object())
    def uncertain(*args, **kwargs):
        raise RuntimeError("Redis reply unavailable")
    monkeypatch.setattr("rq.job.Job.fetch", uncertain)
    result = jobs.enqueue_product_details([case.product.id], case.owner.id)
    db_session.refresh(case.product)
    assert result["queued"] == 0
    assert case.product.detail_job_id == token
    assert case.product.detail_lease_expires_at > utc_now()
    assert len(case.dispatched) == 1


@pytest.mark.parametrize("drift", ["owner", "site", "lease", "selected_source"])
def test_sqlite_final_update_rechecks_scope_after_select(db_session, detail_case, monkeypatch, drift):
    case = detail_case
    assert jobs.enqueue_product_details([case.product.id], case.owner.id)["queued"] == 1
    args = case.dispatched[0]
    monkeypatch.setattr(jobs, "_scrape_detail", lambda *args: case.detail)
    original_update = Query.update
    changed = False
    def drift_before_fence(query, values, *args, **kwargs):
        nonlocal changed
        if values.get(Product.detail_fetch_state) == "complete" and not changed:
            changed = True
            value = {
                "owner": {Product.user_id: case.other.id},
                "site": {Product.site: "snkrdunk"},
                "lease": {Product.detail_lease_expires_at: utc_now() - timedelta(seconds=1)},
                "selected_source": {Product.detail_source_url: "https://www.recordcity.jp/ja/catalog/9022"},
            }[drift]
            original_update(db_session.query(Product).filter(Product.id == case.product.id), value, synchronize_session=False)
            db_session.commit()
        return original_update(query, values, *args, **kwargs)
    monkeypatch.setattr(Query, "update", drift_before_fence)
    result = jobs.run_product_detail_job(*args[:4], expected_shop_id=args[4])
    assert changed, "the test must inject drift between verification and write"
    assert result["status"] == "stale"
    db_session.expire_all()
    product = db_session.get(Product, case.product.id)
    assert product.last_title == "Card title" and product.last_price == 1200
    assert len(product.snapshots) == 1


def test_verified_full_ingestion_completes_card_and_invalidates_older_worker(db_session, detail_case, monkeypatch):
    case = detail_case
    assert jobs.enqueue_product_details([case.product.id], case.owner.id)["queued"] == 1
    args = case.dispatched[0]
    summary = save_scraped_items_to_db([case.detail], case.owner.id, site="recordcity", manual_selection=True, return_summary=True)
    assert summary["processed_count"] == 1
    db_session.refresh(case.product)
    assert case.product.detail_fetch_state == "complete"
    assert case.product.detail_job_id is None
    assert case.product.variants[0].inventory_qty == 1
    monkeypatch.setattr(jobs, "_scrape_detail", lambda *args: pytest.fail("superseded job must never fetch"))
    assert jobs.run_product_detail_job(*args[:4], expected_shop_id=args[4])["status"] == "stale"


def test_manual_unknown_full_ingestion_never_promotes_deferred_card_inventory(db_session, detail_case):
    case = detail_case
    unknown = {**case.detail, "status": "unknown"}
    summary = save_scraped_items_to_db([unknown], case.owner.id, site="recordcity", manual_selection=True, return_summary=True)
    db_session.refresh(case.product)
    assert summary["rejected_count"] == 1
    assert case.product.detail_fetch_state == "pending"
    assert case.product.last_status == "unknown"
    assert case.product.variants[0].inventory_qty == 0
    assert case.product.last_title == "Card title"


@pytest.mark.parametrize("old_state", ["queued", "running", "failed"])
def test_corrected_source_can_be_selected_before_old_lease_expires(db_session, detail_case, monkeypatch, old_state):
    case = detail_case
    assert jobs.enqueue_product_details([case.product.id], case.owner.id)["queued"] == 1
    db_session.refresh(case.product)
    old_args = case.dispatched[0]
    case.product.detail_fetch_state = old_state
    if old_state == "failed":
        case.product.detail_retry_at = utc_now() + timedelta(hours=1)
    case.product.source_url = "https://www.recordcity.jp/ja/catalog/9022"
    db_session.commit()
    result = jobs.enqueue_product_details([case.product.id], case.owner.id)
    db_session.refresh(case.product)
    assert result["queued"] == 1
    assert case.product.detail_job_id != old_args[3]
    assert case.product.detail_source_url == case.product.source_url
    assert case.dispatched[-1][2] == case.product.source_url
    monkeypatch.setattr(jobs, "_scrape_detail", lambda *args: pytest.fail("old source must not fetch"))
    assert jobs.run_product_detail_job(*old_args[:4], expected_shop_id=old_args[4])["status"] == "stale"


def test_full_registration_translation_reuses_same_source_but_not_curated_source_changes(db_session, detail_case, monkeypatch):
    from routes.scrape import _enqueue_translation_for_products

    case = detail_case
    assert jobs.enqueue_product_details([case.product.id], case.owner.id, translate=True)["queued"] == 1
    dispatched = []
    monkeypatch.setattr(jobs, "_dispatch_translation", lambda job_id: dispatched.append(job_id))
    inline = []
    monkeypatch.setattr("routes.scrape._run_translation_inline", lambda job_id: inline.append(job_id))
    save_scraped_items_to_db([case.detail], case.owner.id, site="recordcity", return_summary=True)
    db_session.expire_all()
    assert db_session.query(TranslationSuggestion).count() == 1
    assert _enqueue_translation_for_products([case.product.id], case.owner.id, db_session) == 0
    assert db_session.query(TranslationSuggestion).count() == 1
    assert len(dispatched) == 1 and inline == []
    case.product.custom_title = "New curated title"
    db_session.commit()
    assert _enqueue_translation_for_products([case.product.id], case.owner.id, db_session) == 1
    assert db_session.query(TranslationSuggestion).count() == 2
    assert len(inline) == 1
