from datetime import timedelta

import pytest

from models import Product, ProductSnapshot, Shop, TranslationSuggestion, User, Variant
from services import product_detail_jobs as jobs
from services.product_service import save_scraped_items_to_db
from time_utils import utc_now


def card(source_id="1001", **overrides):
    result = {
        "_listing_card": True, "source_id": source_id, "currency": "JPY",
        "url": f"https://www.recordcity.jp/ja/catalog/{source_id}",
        "title": "Record", "price": 1200, "status": "unknown", "description": "",
        "image_urls": [f"https://files.recordcity.jp/image/{source_id}.jpg"],
    }
    result.update(overrides)
    return result


@pytest.fixture
def owner(db_session):
    user = User(username="deferred-owner", password_hash="test-hash")
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr("services.product_service._cache_external_images", lambda urls, pid: urls)
    monkeypatch.setattr("services.product_service.cache_deferred_detail_images", lambda item, pid, job_id: item.get("image_urls") or [])
    monkeypatch.setattr(jobs, "record_observation_safely", lambda **kw: True)


def make_pending(db_session, owner, *, shop_id=None):
    summary = save_scraped_items_to_db([card()], owner.id, site="recordcity", shop_id=shop_id, manual_selection=True, return_summary=True)
    assert summary["new_count"] == 1
    db_session.expire_all()
    return db_session.get(Product, summary["product_ids"][0])


def queue_one(monkeypatch, db_session, product, *, translate=False):
    dispatched = []
    monkeypatch.setattr(jobs, "_dispatch_detail_job", lambda *args: dispatched.append(args))
    result = jobs.enqueue_product_details([product.id], product.user_id, translate=translate)
    assert result["queued"] == 1
    db_session.expire_all()
    return dispatched[0]


def detail_for(product, **overrides):
    result = {
        "url": product.source_url, "title": "Verified record", "price": 1500,
        "status": "on_sale", "description": "Full detail",
        "image_urls": ["https://files.recordcity.jp/image/detail.jpg"],
    }
    result.update(overrides)
    return result


def run_args(args):
    return jobs.run_product_detail_job(*args[:4], expected_shop_id=args[4])


def test_listing_unknown_is_not_promoted_or_inventory_inferred(client, db_session, owner):
    product = make_pending(db_session, owner)
    assert product.last_status == "unknown"
    assert product.detail_fetch_state == "pending"
    assert product.variants[0].inventory_qty == 0
    assert product.snapshots[0].description == ""


@pytest.mark.parametrize("status,inventory", [("on_sale", 1), ("sold", 0)])
def test_explicit_listing_inventory(client, db_session, owner, status, inventory):
    result = save_scraped_items_to_db([card(status=status)], owner.id, site="recordcity", return_summary=True)
    product = db_session.get(Product, result["product_ids"][0])
    assert product.last_status == status
    assert product.variants[0].inventory_qty == inventory


def test_shallow_reimport_preserves_existing_complete_description_and_sold_inventory(client, db_session, owner):
    product = Product(user_id=owner.id, site="recordcity", source_url=card()["url"], last_title="Detailed", last_price=2000, last_status="sold")
    product.variants.append(Variant(option1_value="Default Title", inventory_qty=0, price=2000))
    product.snapshots.append(ProductSnapshot(description="Do not replace", image_urls="/media/full.jpg", status="sold"))
    db_session.add(product)
    db_session.commit()
    summary = save_scraped_items_to_db([card(status="on_sale")], owner.id, site="recordcity", manual_selection=True, return_summary=True)
    db_session.expire_all()
    assert summary["processed_count"] == 1
    assert product.detail_fetch_state is None
    assert product.last_status == "sold"
    assert product.last_title == "Detailed"
    assert product.last_price == 2000
    assert product.variants[0].inventory_qty == 0
    assert len(product.snapshots) == 1
    assert product.snapshots[0].description == "Do not replace"


def test_listing_alias_reuses_only_same_owner_shop(client, db_session, owner):
    shop = Shop(user_id=owner.id, name="One")
    db_session.add(shop)
    db_session.commit()
    product = Product(user_id=owner.id, shop_id=shop.id, site="recordcity", source_url="https://recordcity.jp/en/catalog/1001", last_title="Existing", last_status="sold")
    db_session.add(product)
    db_session.commit()
    result = save_scraped_items_to_db([card()], owner.id, site="recordcity", shop_id=shop.id, return_summary=True)
    assert result["new_count"] == 0
    assert result["product_ids"] == [product.id]
    second = save_scraped_items_to_db([card()], owner.id, site="recordcity", shop_id=None, return_summary=True)
    assert second["new_count"] == 1


def test_queue_deduplicates_and_or_merges_translation_request(client, db_session, owner, monkeypatch):
    product = make_pending(db_session, owner)
    args = queue_one(monkeypatch, db_session, product)
    result = jobs.enqueue_product_details([product.id, product.id], owner.id, translate=True)
    db_session.expire_all()
    assert result["queued"] == 0
    assert result["skipped"] == 1
    assert product.detail_job_id == args[3]
    assert product.detail_translate_requested is True


def test_queue_checks_owner_and_shop_owner(client, db_session, owner, monkeypatch):
    other = User(username="another", password_hash="test-hash")
    db_session.add(other)
    db_session.commit()
    shop = Shop(user_id=other.id, name="Not yours")
    db_session.add(shop)
    db_session.commit()
    product = make_pending(db_session, owner)
    monkeypatch.setattr(jobs, "_dispatch_detail_job", lambda *a: pytest.fail("must not queue"))
    assert jobs.enqueue_product_details([product.id], other.id)["queued"] == 0
    assert jobs.enqueue_product_details([product.id], owner.id, shop_id=shop.id)["queued"] == 0
    product.shop_id = shop.id
    db_session.commit()
    assert jobs.enqueue_product_details([product.id], owner.id)["queued"] == 0


def test_full_completion_updates_exact_product_and_fenced_state(client, db_session, owner, monkeypatch):
    product = make_pending(db_session, owner)
    item = detail_for(product)
    args = queue_one(monkeypatch, db_session, product)
    monkeypatch.setattr(jobs, "_scrape_detail", lambda site, url: item)
    assert run_args(args)["status"] == "complete"
    db_session.expire_all()
    assert product.detail_fetch_state == "complete"
    assert product.last_title == "Verified record"
    assert product.last_price == 1500
    assert product.last_status == "on_sale"
    assert product.variants[0].inventory_qty == 1
    assert len(product.snapshots) == 2
    assert any(s.description == "Full detail" for s in product.snapshots)
    assert run_args(args)["status"] == "stale"


@pytest.mark.parametrize("field,value", [("source_url", "https://www.recordcity.jp/ja/catalog/1002"), ("deleted_at", utc_now()), ("shop_id", "new_shop")])
def test_change_before_worker_does_not_fetch(client, db_session, owner, monkeypatch, field, value):
    product = make_pending(db_session, owner)
    args = queue_one(monkeypatch, db_session, product)
    if value == "new_shop":
        shop = Shop(user_id=owner.id, name="Changed")
        db_session.add(shop)
        db_session.flush()
        value = shop.id
    setattr(product, field, value)
    db_session.commit()
    monkeypatch.setattr(jobs, "_scrape_detail", lambda *a: pytest.fail("must not fetch"))
    assert run_args(args)["status"] == "stale"


@pytest.mark.parametrize("drift", ["source", "delete", "token", "shop"])
def test_change_during_fetch_rejects_late_write(client, db_session, owner, monkeypatch, drift):
    product = make_pending(db_session, owner)
    item = detail_for(product)
    args = queue_one(monkeypatch, db_session, product)

    def fetch(site, url):
        db_session.expire_all()
        if drift == "source":
            product.source_url = "https://www.recordcity.jp/ja/catalog/1002"
        elif drift == "delete":
            product.deleted_at = utc_now()
        elif drift == "token":
            product.detail_job_id = "new-claim"
        else:
            shop = Shop(user_id=owner.id, name="New shop")
            db_session.add(shop)
            db_session.flush()
            product.shop_id = shop.id
        db_session.commit()
        return item

    monkeypatch.setattr(jobs, "_scrape_detail", fetch)
    assert run_args(args)["status"] == "stale"
    db_session.expire_all()
    assert product.last_title == "Record"
    assert product.last_price == 1200
    assert len(product.snapshots) == 1


@pytest.mark.parametrize("patch", [{"status": "unknown"}, {"status": "error"}, {"status": "blocked"}, {"price": None}, {"url": "https://www.recordcity.jp/ja/catalog/9999"}])
def test_unverified_detail_preserves_snapshot_price_stock_and_is_retryable(client, db_session, owner, monkeypatch, patch):
    product = make_pending(db_session, owner)
    item = detail_for(product, **patch)
    args = queue_one(monkeypatch, db_session, product)
    monkeypatch.setattr(jobs, "_scrape_detail", lambda site, url: item)
    assert run_args(args)["status"] == "failed"
    db_session.expire_all()
    assert product.detail_fetch_state == "failed"
    assert product.detail_retry_at > utc_now()
    assert product.last_price == 1200
    assert product.last_status == "unknown"
    assert product.variants[0].inventory_qty == 0
    assert len(product.snapshots) == 1
    assert jobs.enqueue_product_details([product.id], owner.id)["queued"] == 0


def test_expired_request_recovers_with_new_token_and_blocks_old_worker(client, db_session, owner, monkeypatch):
    product = make_pending(db_session, owner)
    old_args = queue_one(monkeypatch, db_session, product)
    product.detail_fetch_state = "running"
    product.detail_lease_expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    dispatches = []
    monkeypatch.setattr(jobs, "_dispatch_detail_job", lambda *args: dispatches.append(args))
    result = jobs.recover_product_detail_jobs()
    db_session.expire_all()
    assert result["queued"] == 1
    assert product.detail_job_id != old_args[3]
    assert run_args(old_args)["status"] == "stale"
    assert jobs.recover_product_detail_jobs()["queued"] == 0


def test_translation_is_created_only_with_completed_details_and_source_hash(client, db_session, owner, monkeypatch):
    product = make_pending(db_session, owner)
    item = detail_for(product)
    args = queue_one(monkeypatch, db_session, product, translate=True)
    assert db_session.query(TranslationSuggestion).count() == 0
    dispatched = []
    monkeypatch.setattr(jobs, "_dispatch_translation", lambda jobid: dispatched.append(jobid))
    monkeypatch.setattr(jobs, "_scrape_detail", lambda *a: item)
    assert run_args(args)["status"] == "complete"
    db_session.expire_all()
    suggestion = db_session.query(TranslationSuggestion).one()
    assert suggestion.source_description == "Full detail"
    assert len(suggestion.source_description_hash) == 64
    assert suggestion.source_title == "Verified record"
    assert suggestion.auto_apply is True
    assert dispatched == [suggestion.job_id]


def test_enqueue_error_marks_failed_and_never_updates_completed_work(client, db_session, owner, monkeypatch):
    product = make_pending(db_session, owner)
    monkeypatch.setattr(jobs, "_dispatch_detail_job", lambda *a: (_ for _ in ()).throw(RuntimeError("unavailable")))
    result = jobs.enqueue_product_details([product.id], owner.id)
    db_session.expire_all()
    assert result["failed"] == 1
    assert product.detail_fetch_state == "failed"
    assert product.detail_error_code == "enqueue_failed"


def test_scraper_list_contract_and_identity(client, monkeypatch):
    import recordcity_db

    monkeypatch.setattr(recordcity_db, "scrape_single_item", lambda *a, **kw: [card()])
    assert jobs._scrape_detail("recordcity", card()["url"]) == card()
    monkeypatch.setattr(recordcity_db, "scrape_single_item", lambda *a, **kw: [card(), card("1002")])
    with pytest.raises(ValueError):
        jobs._scrape_detail("recordcity", card()["url"])


def test_owner_queue_limit_leaves_selected_demand_durable_but_not_unselected_cards(client, db_session, owner, monkeypatch):
    products = []
    for number in range(25):
        product = Product(user_id=owner.id, site="recordcity", source_url=card(str(2000 + number))["url"], detail_fetch_state="pending")
        db_session.add(product)
        products.append(product)
    db_session.commit()
    dispatched = []
    monkeypatch.setattr(jobs, "_dispatch_detail_job", lambda *args: dispatched.append(args))
    result = jobs.enqueue_product_details([p.id for p in products[:24]], owner.id, translate=True)
    db_session.expire_all()
    assert result["queued"] == 20
    assert result["pending"] == 24
    assert len(dispatched) == 20
    for product in products[20:24]:
        assert product.detail_fetch_state == "pending"
        assert product.detail_source_url == product.source_url
        assert product.detail_translate_requested is True
    assert products[24].detail_source_url is None
    products[0].detail_fetch_state = "complete"
    db_session.commit()
    assert jobs.recover_product_detail_jobs()["queued"] == 1
    assert len(dispatched) == 21
    assert products[24].detail_source_url is None


def test_global_queue_limit_and_refill_favor_owner_with_fewer_active_tasks(client, db_session, owner, monkeypatch):
    # A small injected ceiling makes the actual admission/refill algorithm
    # observable without creating 100 unused database fixtures.
    monkeypatch.setattr(jobs, "_OWNER_ACTIVE_LIMIT", 3)
    monkeypatch.setattr(jobs, "_GLOBAL_ACTIVE_LIMIT", 3)
    other = User(username="second-owner", password_hash="hash")
    db_session.add(other)
    db_session.flush()
    products = []
    for number, user_id in enumerate([owner.id, owner.id, owner.id, owner.id, other.id]):
        product = Product(user_id=user_id, site="recordcity", source_url=card(str(3000 + number))["url"], detail_fetch_state="pending")
        db_session.add(product)
        products.append(product)
    db_session.commit()
    dispatched = []
    monkeypatch.setattr(jobs, "_dispatch_detail_job", lambda *args: dispatched.append(args))
    assert jobs.enqueue_product_details([p.id for p in products[:4]], owner.id)["queued"] == 3
    assert jobs.enqueue_product_details([products[4].id], other.id)["queued"] == 0
    db_session.expire_all()
    products[0].detail_fetch_state = "complete"
    db_session.commit()
    assert jobs.recover_product_detail_jobs(limit=10)["queued"] == 1
    assert dispatched[-1][1] == other.id
    db_session.expire_all()
    assert products[3].detail_fetch_state == "pending"
    assert products[4].detail_fetch_state == "queued"


def test_expired_queued_job_still_waiting_is_renewed_not_duplicated(client, db_session, owner, monkeypatch):
    from concurrent.futures import Future

    product = make_pending(db_session, owner)
    args = queue_one(monkeypatch, db_session, product)
    product.detail_lease_expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    future = Future()
    monkeypatch.setitem(jobs._local_jobs, args[3], future)
    monkeypatch.setattr(jobs, "_dispatch_detail_job", lambda *a: pytest.fail("queued twice"))
    assert jobs.recover_product_detail_jobs()["queued"] == 0
    db_session.expire_all()
    assert product.detail_job_id == args[3]
    assert product.detail_lease_expires_at > utc_now()
    # An explicit selection on an expired but still waiting job is also safe.
    product.detail_lease_expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    assert jobs.enqueue_product_details([product.id], owner.id, translate=True)["queued"] == 0
    db_session.expire_all()
    assert product.detail_translate_requested is True
    assert product.detail_job_id == args[3]


def test_fetch_failure_after_source_drift_has_no_old_request_side_effect(client, db_session, owner, monkeypatch):
    product = make_pending(db_session, owner)
    args = queue_one(monkeypatch, db_session, product)

    def fail_after_edit(*unused):
        db_session.expire_all()
        product.source_url = "https://www.recordcity.jp/ja/catalog/1002"
        db_session.commit()
        raise RuntimeError("transport failed")

    monkeypatch.setattr(jobs, "_scrape_detail", fail_after_edit)
    assert run_args(args)["status"] == "stale"
    db_session.expire_all()
    assert product.detail_fail_count == 0
    assert product.detail_error_code is None
    assert product.last_price == 1200


def test_verified_normal_registration_supersedes_an_old_lazy_job(client, db_session, owner, monkeypatch):
    product = make_pending(db_session, owner)
    args = queue_one(monkeypatch, db_session, product)
    summary = save_scraped_items_to_db([detail_for(product)], owner.id, site="recordcity", return_summary=True)
    assert summary["processed_count"] == 1
    db_session.expire_all()
    assert product.detail_fetch_state == "complete"
    assert product.detail_job_id is None
    monkeypatch.setattr(jobs, "_scrape_detail", lambda *args: pytest.fail("old worker fetched"))
    assert run_args(args)["status"] == "stale"


def test_normal_manual_unknown_selection_does_not_confirm_lazy_detail(client, db_session, owner):
    product = make_pending(db_session, owner)
    save_scraped_items_to_db([detail_for(product, status="unknown")], owner.id, site="recordcity", manual_selection=True)
    db_session.expire_all()
    assert product.detail_fetch_state == "pending"
    assert product.last_status == "unknown"
    assert product.variants[0].inventory_qty == 0


def test_shop_ownership_transfer_during_detail_fetch_prevents_completion(client, db_session, owner, monkeypatch):
    shop = Shop(user_id=owner.id, name="Selected")
    other = User(username="transferred-shop-owner", password_hash="hash")
    db_session.add_all([shop, other])
    db_session.commit()
    product = make_pending(db_session, owner, shop_id=shop.id)
    args = queue_one(monkeypatch, db_session, product)
    item = detail_for(product)

    def fetch(*unused):
        shop.user_id = other.id
        db_session.commit()
        return item

    monkeypatch.setattr(jobs, "_scrape_detail", fetch)
    assert run_args(args)["status"] == "stale"
    db_session.expire_all()
    assert product.detail_fetch_state == "running"
    assert product.last_title == "Record"
    assert product.last_status == "unknown"


def test_verified_full_registration_fulfills_prior_durable_translation_request(client, db_session, owner, monkeypatch):
    product = make_pending(db_session, owner)
    args = queue_one(monkeypatch, db_session, product, translate=True)
    dispatched = []
    monkeypatch.setattr(jobs, "_dispatch_translation", lambda job_id: dispatched.append(job_id))
    save_scraped_items_to_db([detail_for(product)], owner.id, site="recordcity")
    db_session.expire_all()
    suggestion = db_session.query(TranslationSuggestion).one()
    assert suggestion.source_description == "Full detail"
    assert suggestion.source_title == "Verified record"
    assert suggestion.auto_apply is True
    assert suggestion.source_title_hash
    assert suggestion.source_description_hash
    assert product.detail_translate_requested is False
    assert product.detail_job_id is None
    assert dispatched == [suggestion.job_id]
    assert run_args(args)["status"] == "stale"


def test_new_source_selection_supersedes_active_old_url_immediately(client, db_session, owner, monkeypatch):
    product = make_pending(db_session, owner)
    old = queue_one(monkeypatch, db_session, product)
    product.source_url = "https://www.recordcity.jp/ja/catalog/1002"
    db_session.commit()
    new = queue_one(monkeypatch, db_session, product)
    assert new[2] == product.source_url
    assert new[3] != old[3]
    assert run_args(old)["status"] == "stale"


def test_new_shop_selection_supersedes_active_old_scope_immediately(client, db_session, owner, monkeypatch):
    product = make_pending(db_session, owner)
    old = queue_one(monkeypatch, db_session, product)
    shop = Shop(user_id=owner.id, name="New selected shop")
    db_session.add(shop)
    db_session.flush()
    product.shop_id = shop.id
    db_session.commit()
    new = queue_one(monkeypatch, db_session, product)
    assert new[4] == shop.id
    assert new[3] != old[3]
    assert product.detail_scope_key == jobs._scope_key(owner.id, shop.id)
    assert run_args(old)["status"] == "stale"


def test_listing_initial_save_does_not_download_hundreds_of_images(client, db_session, owner, monkeypatch):
    monkeypatch.setattr("services.product_service._cache_external_images", lambda *args: pytest.fail("listing save must not download"))
    result = save_scraped_items_to_db([card(str(8000 + i)) for i in range(30)], owner.id, site="recordcity", return_summary=True)
    assert result["new_count"] == 30


@pytest.mark.parametrize("drift", ["owner", "shop"])
def test_recovery_does_not_rearm_demand_selected_in_an_old_scope(client, db_session, owner, monkeypatch, drift):
    product = make_pending(db_session, owner)
    old = queue_one(monkeypatch, db_session, product)
    product.detail_fetch_state = "pending"
    product.detail_job_id = None
    product.detail_lease_expires_at = None
    if drift == "owner":
        other = User(username="new-detail-owner", password_hash="hash")
        db_session.add(other)
        db_session.flush()
        product.user_id = other.id
    else:
        shop = Shop(user_id=owner.id, name="Moved")
        db_session.add(shop)
        db_session.flush()
        product.shop_id = shop.id
    db_session.commit()
    dispatched = []
    monkeypatch.setattr(jobs, "_dispatch_detail_job", lambda *args: dispatched.append(args))
    assert jobs.recover_product_detail_jobs()["queued"] == 0
    assert dispatched == []
    db_session.expire_all()
    assert product.detail_fetch_state == "pending"
    # A new scope can expressly select the product and supersede that demand.
    assert jobs.enqueue_product_details([product.id], product.user_id)["queued"] == 1
    assert len(dispatched) == 1


def test_recovery_foreign_shop_rows_do_not_starve_later_valid_demand(client, db_session, owner, monkeypatch):
    other = User(username="foreign-shop-owner", password_hash="hash")
    db_session.add(other)
    db_session.flush()
    foreign = Shop(user_id=other.id, name="Foreign")
    db_session.add(foreign)
    db_session.flush()
    for number in range(25):
        # Scope matches the row's owner/shop, but the shop itself is foreign.
        db_session.add(Product(
            user_id=owner.id, shop_id=foreign.id, site="recordcity",
            source_url=card(str(9000 + number))["url"],
            detail_fetch_state="pending", detail_source_url=card(str(9000 + number))["url"],
            detail_scope_key=jobs._scope_key(owner.id, foreign.id),
        ))
    valid = Product(user_id=owner.id, site="recordcity", source_url=card("9999")["url"],
        detail_fetch_state="pending", detail_source_url=card("9999")["url"],
        detail_scope_key=jobs._scope_key(owner.id, None))
    db_session.add(valid)
    db_session.commit()
    dispatched = []
    monkeypatch.setattr(jobs, "_dispatch_detail_job", lambda *args: dispatched.append(args))
    assert jobs.recover_product_detail_jobs(limit=1)["queued"] == 1
    assert dispatched[0][0] == valid.id


@pytest.mark.parametrize("metadata", ["same", "missing", "changed"])
def test_public_enqueue_cannot_reset_future_cooldown_from_request_metadata(client, db_session, owner, monkeypatch, metadata):
    product = make_pending(db_session, owner)
    old = queue_one(monkeypatch, db_session, product)
    product.detail_fetch_state = "failed"
    cooldown = utc_now() + timedelta(minutes=5)
    product.detail_retry_at = cooldown
    product.detail_fail_count = 2
    if metadata == "missing":
        product.detail_source_url = None
        product.detail_scope_key = None
    elif metadata == "changed":
        product.source_url = "https://www.recordcity.jp/ja/catalog/1002"
    db_session.commit()
    dispatched = []
    monkeypatch.setattr(jobs, "_dispatch_detail_job", lambda *args: dispatched.append(args))
    result = jobs.enqueue_product_details([product.id], owner.id, respect_backoff=True)
    db_session.expire_all()
    assert result["queued"] == 0
    assert dispatched == []
    assert product.detail_fetch_state == "failed"
    assert product.detail_retry_at == cooldown
    assert product.detail_fail_count == 2


def test_public_enqueue_can_retry_after_cooldown_expires(client, db_session, owner, monkeypatch):
    product = make_pending(db_session, owner)
    queue_one(monkeypatch, db_session, product)
    product.detail_fetch_state = "failed"
    product.detail_retry_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    dispatched = []
    monkeypatch.setattr(jobs, "_dispatch_detail_job", lambda *args: dispatched.append(args))
    result = jobs.enqueue_product_details([product.id], owner.id, respect_backoff=True)
    assert result["queued"] == 1
    assert len(dispatched) == 1
