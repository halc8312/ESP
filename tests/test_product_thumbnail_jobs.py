from datetime import timedelta
from io import BytesIO

from PIL import Image
import pytest
from sqlalchemy import event

from database import create_isolated_session
from models import Product, ProductSnapshot, ProductThumbnailJob, Shop, TranslationSuggestion, User, Variant
from services import product_thumbnail_jobs as jobs
from services.image_service import download_external_image as guarded_image_download
from services.product_service import save_scraped_items_to_db
from time_utils import utc_now


def listing_card(source_id):
    return {
        "_listing_card": True, "source_id": str(source_id), "currency": "JPY",
        "url": f"https://www.recordcity.jp/ja/catalog/{source_id}",
        "title": "Listing record", "price": 1200, "status": "unknown", "description": "",
        "image_urls": [f"https://files.recordcity.jp/image/{source_id}.jpg"],
    }


def png_bytes():
    output = BytesIO()
    Image.new("RGB", (7, 5), "red").save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def owners(db_session):
    users = [User(username=f"thumb-owner-{index}", password_hash="hash") for index in range(7)]
    db_session.add_all(users)
    db_session.flush()
    for user in users:
        db_session.add(Shop(user_id=user.id, name=f"Shop {user.id}"))
    db_session.commit()
    return users


@pytest.fixture
def factory(db_session, owners):
    serial = 1000

    def create(*, owner=None, count=1, shop=None, created=None):
        nonlocal serial
        owner = owner or owners[0]
        products = []
        for _ in range(count):
            serial += 1
            item = listing_card(serial)
            product = Product(user_id=owner.id, shop_id=shop, site="recordcity", source_url=item["url"],
                last_title=item["title"], last_price=1200, last_status="unknown", detail_fetch_state="pending")
            product.variants.append(Variant(option1_value="Default Title", inventory_qty=0, price=1200))
            snapshot = ProductSnapshot(title=item["title"], price=1200, status="unknown", description="",
                image_urls=item["image_urls"][0], scraped_at=created or utc_now())
            product.snapshots.append(snapshot)
            db_session.add(product)
            db_session.flush()
            assert jobs.create_thumbnail_demand(db_session, product, snapshot)
            if created:
                product.thumbnail_job.created_at = created
                product.thumbnail_job.updated_at = created
            products.append(product)
        db_session.commit()
        return products

    return create


@pytest.fixture
def courier(monkeypatch):
    downloaded = []
    delivered = []

    def download(url, **kwargs):
        with kwargs["request_admission"](url):
            downloaded.append(url)
            return png_bytes(), ".png"

    def upload(product_id, token, index, content, *, kind="detail"):
        assert kind == "thumbnail" and index == 0 and content == png_bytes()
        url = f"/media/product-delivery/thumbnail/{product_id}/{token}/0.png"
        session = create_isolated_session()
        try:
            context = jobs.authorize_thumbnail_delivery(session, product_id, token)
            if context is None or not jobs.finalize_thumbnail_delivery(session, context, url):
                session.rollback()
                raise ValueError("stale_delivery")
            session.commit()
        finally:
            session.close()
        delivered.append(url)
        return url

    monkeypatch.setattr("services.image_service.download_external_image", download)
    monkeypatch.setattr("services.product_image_delivery.deliver_image_bytes", upload)
    return downloaded, delivered


def queue_product(db_session, product):
    result = jobs.recover_thumbnail_jobs(user_id=product.user_id, limit_batches=1)
    assert result["queued_batches"] == 1
    db_session.expire_all()
    return product.thumbnail_job.job_id


def test_listing_save_500_is_one_bounded_batch_and_performs_no_image_download(db_session, owners, monkeypatch):
    dispatched = []
    monkeypatch.setattr(jobs, "_dispatch_batch", lambda *args: dispatched.append(args))
    monkeypatch.setattr("services.image_service.download_external_image", lambda *a, **k: pytest.fail("listing save must not download"))
    summary = save_scraped_items_to_db([listing_card(index) for index in range(1000, 1500)],
        owners[0].id, site="recordcity", manual_selection=True, return_summary=True)
    db_session.expire_all()
    assert summary["new_count"] == 500
    assert db_session.query(ProductThumbnailJob).count() == 500
    assert db_session.query(ProductThumbnailJob).filter_by(state="queued").count() == 10
    assert db_session.query(ProductThumbnailJob).filter_by(state="pending").count() == 490
    assert len(dispatched) == 1
    assert {product.detail_fetch_state for product in db_session.query(Product)} == {"pending"}
    assert {variant.inventory_qty for variant in db_session.query(Variant)} == {0}
    assert db_session.query(TranslationSuggestion).count() == 0


def test_image_only_completion_preserves_price_stock_details_and_translation(db_session, factory, courier):
    product = factory()[0]
    batch = queue_product(db_session, product)
    assert jobs._run_thumbnail(product.id, product.user_id, batch) == "complete"
    db_session.expire_all()
    row = product.thumbnail_job
    assert row.state == "complete" and row.attempts == 1
    assert product.snapshots[0].image_urls == courier[1][0]
    assert product.last_price == 1200 and product.last_status == "unknown"
    assert product.last_title == "Listing record"
    assert product.variants[0].inventory_qty == 0
    assert product.detail_fetch_state == "pending"
    assert product.snapshots[0].description == "" and len(product.snapshots) == 1
    assert db_session.query(TranslationSuggestion).count() == 0
    assert jobs._run_thumbnail(product.id, product.user_id, batch) == "stale"
    assert len(courier[0]) == 1


def test_reimport_preserves_completed_image_and_deduplicates_demand(db_session, owners, courier):
    first = save_scraped_items_to_db([listing_card(1001)], owners[0].id, site="recordcity", return_summary=True)
    product = db_session.get(Product, first["product_ids"][0])
    batch = product.thumbnail_job.job_id
    assert jobs._run_thumbnail(product.id, product.user_id, batch) == "complete"
    save_scraped_items_to_db([listing_card(1001)], owners[0].id, site="recordcity", return_summary=True)
    db_session.expire_all()
    assert product.thumbnail_job.state == "complete"
    assert product.thumbnail_job.attempts == 1
    assert product.snapshots[0].image_urls == courier[1][0]
    assert db_session.query(ProductThumbnailJob).count() == 1


def test_explicit_shallow_reimport_rebinds_owned_shop_without_changing_details(db_session, owners):
    first = save_scraped_items_to_db([listing_card(1001)], owners[0].id, site="recordcity", return_summary=True)
    product = db_session.get(Product, first["product_ids"][0])
    old_batch = product.thumbnail_job.job_id
    shop = db_session.query(Shop).filter_by(user_id=owners[0].id).one()
    summary = save_scraped_items_to_db([listing_card(1001)], owners[0].id, site="recordcity", shop_id=shop.id, return_summary=True)
    db_session.expire_all()
    assert summary["new_count"] == 0
    assert product.thumbnail_job.shop_id == shop.id and product.shop_id == shop.id
    assert product.thumbnail_job.job_id == old_batch
    assert product.thumbnail_job.batch_user_id == owners[0].id
    assert product.thumbnail_job.state == "pending"
    assert jobs._run_thumbnail(product.id, product.user_id, old_batch) == "stale"
    assert product.last_price == 1200 and product.variants[0].inventory_qty == 0
    assert product.detail_fetch_state == "pending" and len(product.snapshots) == 1
    assert jobs.run_thumbnail_batch(owners[0].id, old_batch) == {"complete": 0, "failed": 0, "stale": 0}
    db_session.expire_all()
    assert product.thumbnail_job.job_id != old_batch
    assert product.thumbnail_job.state == "queued"


@pytest.mark.parametrize("drift", ["owner", "shop", "shop_owner", "source", "site", "deleted", "archived", "suspended", "snapshot", "image", "token", "lease"])
def test_change_during_fetch_rejects_image_without_overwriting_current_product(db_session, factory, owners, monkeypatch, courier, drift):
    shop = db_session.query(Shop).filter_by(user_id=owners[0].id).one()
    product = factory(shop=shop.id)[0]
    batch = queue_product(db_session, product)
    original_image = product.snapshots[0].image_urls

    def download(url, **kwargs):
        with kwargs["request_admission"](url):
            pass
        db_session.expire_all()
        if drift == "owner":
            product.user_id = owners[1].id
        elif drift == "shop":
            product.shop_id = db_session.query(Shop).filter_by(user_id=owners[1].id).one().id
        elif drift == "shop_owner":
            shop.user_id = owners[1].id
        elif drift == "source":
            product.source_url = listing_card(9999)["url"]
        elif drift == "site":
            product.site = "mercari"
        elif drift == "deleted":
            product.deleted_at = utc_now()
        elif drift == "archived":
            product.archived = True
        elif drift == "suspended":
            owners[0].suspended_at = utc_now()
        elif drift == "snapshot":
            product.snapshots.append(ProductSnapshot(title="Verified detail", description="Keep full detail",
                status="sold", price=777, image_urls="/media/verified.png", scraped_at=utc_now() + timedelta(seconds=1)))
            product.last_status = "sold"
            product.last_price = 777
            product.detail_fetch_state = "complete"
        elif drift == "image":
            product.snapshots[0].image_urls = "/media/owner-edited.png"
        elif drift == "token":
            product.thumbnail_job.claim_token = "new-claim-token"
        elif drift == "lease":
            product.thumbnail_job.lease_expires_at = utc_now() - timedelta(seconds=1)
        db_session.commit()
        return png_bytes(), ".png"

    monkeypatch.setattr("services.image_service.download_external_image", download)
    assert jobs._run_thumbnail(product.id, owners[0].id, batch) in {"stale", "failed"}
    db_session.expire_all()
    assert courier[1] == []
    assert all(not snapshot.image_urls.startswith("/media/product-delivery/") for snapshot in product.snapshots)
    if drift == "snapshot":
        assert product.last_status == "sold" and product.last_price == 777
        assert product.detail_fetch_state == "complete"
        assert any(snapshot.description == "Keep full detail" for snapshot in product.snapshots)
    elif drift == "image":
        assert product.snapshots[0].image_urls == "/media/owner-edited.png"
    else:
        assert product.snapshots[0].image_urls == original_image


def test_complete_replay_requires_current_snapshot_and_scope(db_session, factory, courier):
    product = factory()[0]
    batch = queue_product(db_session, product)
    assert jobs._run_thumbnail(product.id, product.user_id, batch) == "complete"
    db_session.expire_all()
    token = product.thumbnail_job.claim_token
    context = jobs.authorize_thumbnail_delivery(db_session, product.id, token)
    assert context is not None
    assert jobs.finalize_thumbnail_delivery(db_session, context, courier[1][0])
    db_session.commit()
    product.snapshots.append(ProductSnapshot(image_urls="/media/new-detail.png", scraped_at=utc_now() + timedelta(seconds=1)))
    db_session.commit()
    assert jobs.authorize_thumbnail_delivery(db_session, product.id, token) is None


@pytest.mark.parametrize("drift", ["owner", "site", "archived", "source", "snapshot", "capture"])
def test_final_sql_update_fences_scope_changes_after_authorization(db_session, factory, owners, drift):
    product = factory()[0]
    queue_product(db_session, product)
    row = product.thumbnail_job
    row.state = "running"
    row.claim_token = "sql-fence-token"
    row.lease_expires_at = utc_now() + timedelta(minutes=5)
    db_session.commit()
    context = jobs.authorize_thumbnail_delivery(db_session, product.id, row.claim_token)
    assert context is not None
    engine = db_session.get_bind()
    fired = []

    def mutate(connection, cursor, statement, parameters, ctx, many):
        if fired or not statement.startswith("UPDATE product_thumbnail_jobs SET state="):
            return
        fired.append(True)
        if drift == "owner":
            connection.exec_driver_sql("UPDATE products SET user_id=? WHERE id=?", (owners[1].id, product.id))
        elif drift == "site":
            connection.exec_driver_sql("UPDATE products SET site='mercari' WHERE id=?", (product.id,))
        elif drift == "archived":
            connection.exec_driver_sql("UPDATE products SET archived=1 WHERE id=?", (product.id,))
        elif drift == "source":
            connection.exec_driver_sql("UPDATE products SET source_url=? WHERE id=?", (listing_card(9999)["url"], product.id))
        elif drift == "snapshot":
            connection.exec_driver_sql("INSERT INTO product_snapshots (product_id,scraped_at,image_urls) VALUES (?,?,'/media/latest.png')", (product.id, utc_now() + timedelta(days=1)))
        else:
            # Simulate a changed capture with the same token. The context
            # cannot publish its old image into a newly bound source snapshot.
            connection.exec_driver_sql("INSERT INTO product_snapshots (product_id,scraped_at,image_urls) VALUES (?,?,?)", (product.id, utc_now() + timedelta(days=1), row.source_image_url))
            latest_id = connection.exec_driver_sql("SELECT max(id) FROM product_snapshots").scalar_one()
            connection.exec_driver_sql("UPDATE product_thumbnail_jobs SET source_snapshot_id=? WHERE product_id=?", (latest_id, product.id))

    event.listen(engine, "before_cursor_execute", mutate)
    try:
        assert not jobs.finalize_thumbnail_delivery(db_session, context, f"/media/product-delivery/thumbnail/{product.id}/sql-fence-token/0.png")
        assert fired
        assert db_session.in_transaction()  # Endpoint owns cleanup then rollback.
        db_session.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", mutate)
    db_session.expire_all()
    assert product.thumbnail_job.state == "running"
    assert product.snapshots[0].image_urls.startswith("https://")


def test_response_loss_after_web_commit_still_completes(db_session, factory, courier, monkeypatch):
    from services.product_image_delivery import deliver_image_bytes
    product = factory()[0]
    batch = queue_product(db_session, product)

    def lose_response(*args, **kwargs):
        deliver_image_bytes(*args, **kwargs)
        raise ConnectionError("response unavailable")

    monkeypatch.setattr("services.product_image_delivery.deliver_image_bytes", lose_response)
    assert jobs._run_thumbnail(product.id, product.user_id, batch) == "complete"
    db_session.expire_all()
    assert product.thumbnail_job.state == "complete"
    assert product.snapshots[0].image_urls == courier[1][0]


def test_global_and_owner_capacity_are_batches_not_500_individual_tasks(db_session, factory, owners, monkeypatch):
    for owner in owners[:6]:
        factory(owner=owner, count=12)
    dispatched = []
    monkeypatch.setattr(jobs, "_dispatch_batch", lambda *args: dispatched.append(args))
    result = jobs.recover_thumbnail_jobs()
    assert result == {"queued_batches": 5, "queued_images": 50, "failed": 0}
    assert len(dispatched) == 5 and len({owner for owner, batch in dispatched}) == 5
    assert jobs.recover_thumbnail_jobs()["queued_batches"] == 0
    db_session.expire_all()
    assert db_session.query(ProductThumbnailJob).filter_by(state="pending").count() == 22


def test_batch_completion_refills_immediately_and_gives_waiting_owner_a_turn(db_session, factory, owners, courier, monkeypatch):
    for owner in owners[:6]:
        factory(owner=owner, count=12, created=utc_now() - timedelta(minutes=10))
    dispatched = []
    monkeypatch.setattr(jobs, "_dispatch_batch", lambda *args: dispatched.append(args))
    assert jobs.recover_thumbnail_jobs()["queued_batches"] == 5
    owner, batch = dispatched[0]
    assert jobs.run_thumbnail_batch(owner, batch) == {"complete": 10, "failed": 0, "stale": 0}
    assert len(dispatched) == 6
    assert dispatched[-1][0] == owners[5].id
    db_session.expire_all()
    assert db_session.query(ProductThumbnailJob).filter_by(state="complete").count() == 10
    assert jobs.recover_thumbnail_jobs()["queued_batches"] == 0


def test_rebinding_during_image_delivery_holds_capacity_until_worker_drains(db_session, factory, owners, courier, monkeypatch):
    dispatched = []
    monkeypatch.setattr(jobs, "_dispatch_batch", lambda *args: dispatched.append(args))
    product = factory(owner=owners[0])[0]
    old_owner = product.user_id
    old_batch = queue_product(db_session, product)
    waiting = []

    def in_flight_delivery(product_id, token, index, content, *, kind):
        assert product_id == product.id and kind == "thumbnail"
        db_session.expire_all()
        product.user_id = owners[1].id
        assert jobs.create_thumbnail_demand(db_session, product, product.snapshots[0])
        db_session.commit()
        waiting.append(factory(owner=owners[0])[0])
        assert product.thumbnail_job.job_id == old_batch
        assert product.thumbnail_job.batch_user_id == old_owner
        assert jobs.recover_thumbnail_jobs(user_id=old_owner)["queued_batches"] == 0
        assert jobs.recover_thumbnail_jobs(user_id=product.user_id)["queued_batches"] == 0
        assert len(dispatched) == 1
        assert jobs.authorize_thumbnail_delivery(db_session, product.id, token) is None
        db_session.rollback()
        raise ValueError("stale_delivery")

    monkeypatch.setattr("services.product_image_delivery.deliver_image_bytes", in_flight_delivery)
    assert jobs.run_thumbnail_batch(old_owner, old_batch) == {"complete": 0, "failed": 0, "stale": 1}
    db_session.expire_all()
    assert len(dispatched) == 3
    assert product.thumbnail_job.job_id != old_batch and product.thumbnail_job.state == "queued"
    assert product.thumbnail_job.batch_user_id == product.user_id
    assert waiting[0].thumbnail_job.state == "queued"
    assert waiting[0].thumbnail_job.batch_user_id == old_owner
    assert len(courier[0]) == 1 and courier[1] == []


def test_recovery_excludes_stale_scope_before_batch_limit(db_session, factory, owners):
    invalid = factory(count=15)
    valid = factory(count=2)
    for product in invalid:
        product.user_id = owners[1].id
    db_session.commit()
    result = jobs.recover_thumbnail_jobs(user_id=owners[0].id)
    assert result["queued_images"] == 2
    db_session.expire_all()
    assert all(product.thumbnail_job.state == "pending" for product in invalid)
    assert all(product.thumbnail_job.state == "queued" for product in valid)


@pytest.mark.parametrize("invalid", ["archived", "suspended"])
def test_archived_or_suspended_demand_is_not_admitted_or_fetched(db_session, factory, owners, monkeypatch, invalid):
    product = factory()[0]
    if invalid == "archived":
        product.archived = True
    else:
        owners[0].suspended_at = utc_now()
    db_session.commit()
    monkeypatch.setattr("services.image_service.download_external_image", lambda *a, **k: pytest.fail("inactive demand must not fetch"))
    assert jobs.recover_thumbnail_jobs()["queued_images"] == 0
    assert jobs._run_thumbnail(product.id, product.user_id, "old-batch") == "stale"


def test_future_failed_cooldown_and_max_attempts_stop_automatic_retry(db_session, factory, monkeypatch):
    product = factory()[0]

    def invalid_image(url, **kwargs):
        with kwargs["request_admission"](url):
            raise ValueError("invalid image")

    monkeypatch.setattr("services.image_service.download_external_image", invalid_image)
    for expected_attempt in range(1, jobs.MAX_ATTEMPTS + 1):
        batch = queue_product(db_session, product)
        assert jobs._run_thumbnail(product.id, product.user_id, batch) == "failed"
        db_session.expire_all()
        assert product.thumbnail_job.attempts == expected_attempt
        assert product.thumbnail_job.retry_at > utc_now()
        assert jobs.recover_thumbnail_jobs()["queued_images"] == 0
        product.thumbnail_job.retry_at = utc_now() - timedelta(seconds=1)
        db_session.commit()
    assert jobs.recover_thumbnail_jobs()["queued_images"] == 0
    db_session.expire_all()
    assert product.thumbnail_job.state == "failed"
    assert product.snapshots[0].image_urls.startswith("https://files.recordcity.jp/")
    assert product.detail_fetch_state == "pending"


def test_expired_queued_unknown_transport_is_not_reissued(db_session, factory, monkeypatch):
    product = factory()[0]
    batch = queue_product(db_session, product)
    product.thumbnail_job.lease_expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    monkeypatch.setattr(jobs, "_existing_batch_is_alive", lambda token: True)
    assert jobs.recover_thumbnail_jobs()["queued_images"] == 0
    db_session.expire_all()
    assert product.thumbnail_job.job_id == batch
    assert product.thumbnail_job.lease_expires_at > utc_now()


def test_expired_running_claim_reissues_and_old_token_cannot_deliver(db_session, factory):
    product = factory()[0]
    old_batch = queue_product(db_session, product)
    product.thumbnail_job.state = "running"
    product.thumbnail_job.claim_token = "old-token"
    product.thumbnail_job.attempts = 1
    product.thumbnail_job.lease_expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    assert jobs.recover_thumbnail_jobs()["queued_images"] == 1
    db_session.expire_all()
    assert product.thumbnail_job.job_id != old_batch
    assert jobs.authorize_thumbnail_delivery(db_session, product.id, "old-token") is None


@pytest.mark.parametrize("queue_state", ["started", "unknown", None, "unrecognized-status"])
def test_expired_running_rq_batch_keeps_owner_and_global_reservations(db_session, factory, owners, monkeypatch, queue_state):
    from types import SimpleNamespace
    from redis import Redis
    from rq.job import Job

    dispatched = []
    monkeypatch.setattr(jobs, "_dispatch_batch", lambda owner, batch: dispatched.append((owner, batch)))
    products = [factory(owner=owner)[0] for owner in owners[:jobs.GLOBAL_BATCH_LIMIT]]
    assert jobs.recover_thumbnail_jobs()["queued_batches"] == jobs.GLOBAL_BATCH_LIMIT
    db_session.expire_all()
    old_batches = {product.id: product.thumbnail_job.job_id for product in products}
    old_tokens = {}
    for product in products:
        row = product.thumbnail_job
        row.state = "running"
        row.claim_token = f"expired-{product.id}"
        old_tokens[product.id] = row.claim_token
        row.attempts = 1
        row.lease_expires_at = utc_now() - timedelta(seconds=1)
    # Exhaustion cannot free a slot while its RQ work horse is still alive or
    # unknown. It becomes terminal only after the old queue job has stopped.
    products[0].thumbnail_job.attempts = jobs.MAX_ATTEMPTS
    waiting = factory(owner=owners[jobs.GLOBAL_BATCH_LIMIT])[0]
    db_session.commit()
    probe_calls = []
    state = [queue_state]

    def fetch(batch_id, connection=None):
        probe_calls.append(batch_id)
        if state[0] == "unknown":
            raise ConnectionError("queue status unavailable")
        return SimpleNamespace(get_status=lambda refresh=True: state[0])

    monkeypatch.setattr(jobs, "resolve_queue_backend_name", lambda: "rq")
    monkeypatch.setattr(jobs, "resolve_redis_url", lambda: "redis://queue-stub")
    monkeypatch.setattr(Redis, "from_url", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(Job, "fetch", fetch)
    assert jobs.recover_thumbnail_jobs()["queued_batches"] == 0
    assert set(probe_calls) == set(old_batches.values())
    assert len(dispatched) == jobs.GLOBAL_BATCH_LIMIT
    db_session.expire_all()
    for product in products:
        row = product.thumbnail_job
        assert row.job_id == old_batches[product.id] and row.state == "running"
        assert row.claim_token == old_tokens[product.id]
        assert row.lease_expires_at <= utc_now()
        assert jobs.authorize_thumbnail_delivery(db_session, product.id, old_tokens[product.id]) is None
    assert waiting.thumbnail_job.state == "pending"

    state[0] = "failed"
    result = jobs.recover_thumbnail_jobs()
    assert result["queued_batches"] == jobs.GLOBAL_BATCH_LIMIT
    db_session.expire_all()
    assert products[0].thumbnail_job.state == "failed"
    assert products[0].thumbnail_job.error_code == "attempts_exhausted"
    assert all(product.thumbnail_job.job_id != old_batches[product.id] for product in products[1:])
    assert waiting.thumbnail_job.state == "queued"


def test_running_lease_expiring_during_queue_probe_keeps_owner_reservation(db_session, factory, owners, monkeypatch):
    clock = [utc_now()]
    monkeypatch.setattr(jobs, "utc_now", lambda: clock[0])
    first = factory(owner=owners[0])[0]
    second = factory(owner=owners[1])[0]
    assert jobs.recover_thumbnail_jobs()["queued_batches"] == 2
    db_session.expire_all()
    for product in (first, second):
        product.thumbnail_job.state = "running"
        product.thumbnail_job.claim_token = f"running-{product.id}"
        product.thumbnail_job.attempts = 1
    first.thumbnail_job.lease_expires_at = clock[0] - timedelta(seconds=1)
    second.thumbnail_job.lease_expires_at = clock[0] + timedelta(seconds=5)
    second.thumbnail_job.attempts = jobs.MAX_ATTEMPTS
    old_batch = second.thumbnail_job.job_id
    db_session.commit()
    same_owner_waiting = factory(owner=owners[1])[0]
    other_owner_waiting = factory(owner=owners[2])[0]
    probes = []

    def alive(batch):
        probes.append(batch)
        clock[0] += timedelta(seconds=10)
        return True

    monkeypatch.setattr(jobs, "_existing_batch_is_alive", alive)
    result = jobs.recover_thumbnail_jobs()
    assert result["queued_batches"] == 1
    assert probes == [first.thumbnail_job.job_id]
    db_session.expire_all()
    assert second.thumbnail_job.job_id == old_batch and second.thumbnail_job.state == "running"
    assert second.thumbnail_job.lease_expires_at <= clock[0]
    assert jobs.authorize_thumbnail_delivery(db_session, second.id, second.thumbnail_job.claim_token) is None
    assert same_owner_waiting.thumbnail_job.state == "pending"
    assert other_owner_waiting.thumbnail_job.state == "queued"


def test_uninspected_expired_batch_is_reserved_when_queue_probe_limit_is_reached(db_session, factory, owners, monkeypatch):
    products = [factory(owner=owner)[0] for owner in owners[:jobs.GLOBAL_BATCH_LIMIT + 1]]
    assert jobs.recover_thumbnail_jobs()["queued_batches"] == jobs.GLOBAL_BATCH_LIMIT
    db_session.expire_all()
    for index, product in enumerate(products):
        row = product.thumbnail_job
        row.state = "running"
        row.job_id = f"legacy-batch-{index}"
        row.claim_token = f"legacy-token-{index}"
        row.attempts = 1
        row.lease_expires_at = utc_now() - timedelta(minutes=10 - index)
    products[-1].thumbnail_job.attempts = jobs.MAX_ATTEMPTS
    db_session.commit()
    waiting = factory(owner=owners[-2])[0]
    probes = []
    monkeypatch.setattr(jobs, "_existing_batch_is_alive", lambda batch: probes.append(batch) or False)
    result = jobs.recover_thumbnail_jobs()
    assert len(probes) == jobs.GLOBAL_BATCH_LIMIT
    assert "legacy-batch-5" not in probes
    assert result["queued_batches"] == jobs.GLOBAL_BATCH_LIMIT - 1
    db_session.expire_all()
    assert products[-1].thumbnail_job.state == "running"
    assert products[-1].thumbnail_job.job_id == "legacy-batch-5"
    assert waiting.thumbnail_job.state == "pending"


def test_last_attempt_worker_loss_is_terminal_failed_without_sixth_request(db_session, factory):
    product = factory()[0]
    queue_product(db_session, product)
    product.thumbnail_job.state = "running"
    product.thumbnail_job.claim_token = "last-attempt"
    product.thumbnail_job.attempts = jobs.MAX_ATTEMPTS
    product.thumbnail_job.lease_expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    assert jobs.recover_thumbnail_jobs()["queued_images"] == 0
    db_session.expire_all()
    assert product.thumbnail_job.state == "failed"
    assert product.thumbnail_job.error_code == "attempts_exhausted"
    assert product.thumbnail_job.attempts == jobs.MAX_ATTEMPTS
    assert jobs.authorize_thumbnail_delivery(db_session, product.id, "last-attempt") is None


def test_enqueue_failure_is_backed_off_and_keeps_placeholder(db_session, factory, monkeypatch):
    product = factory()[0]
    monkeypatch.setattr(jobs, "_dispatch_batch", lambda *a: (_ for _ in ()).throw(ConnectionError("queue unavailable")))
    monkeypatch.setattr(jobs, "_existing_batch_is_alive", lambda token: False)
    assert jobs.recover_thumbnail_jobs()["failed"] == 1
    db_session.expire_all()
    assert product.thumbnail_job.state == "failed"
    assert product.thumbnail_job.attempts == 0
    assert product.thumbnail_job.dispatch_attempts == 1
    assert product.thumbnail_job.retry_at > utc_now()
    assert product.snapshots[0].image_urls.startswith("https://")
    assert jobs.recover_thumbnail_jobs()["failed"] == 0


def test_shared_pause_over_five_recoveries_does_not_spend_image_attempts(db_session, factory, courier):
    from services.marketplace_access import get_access_store
    store = get_access_store()
    tick = [1000.0]
    store.clock = lambda: tick[0]
    store.pause("recordcity", 600)
    product = factory()[0]
    for _ in range(7):
        batch = queue_product(db_session, product)
        assert jobs._run_thumbnail(product.id, product.user_id, batch) == "failed"
        db_session.expire_all()
        assert product.thumbnail_job.attempts == 0
        assert product.thumbnail_job.dispatch_attempts == 0
        assert product.thumbnail_job.error_code == "access_wait"
        assert 595 < (product.thumbnail_job.retry_at - utc_now()).total_seconds() <= 600
        assert jobs.recover_thumbnail_jobs()["queued_images"] == 0
        # Forced repeated recovery simulates old/duplicate scheduler demand
        # while the independent shared site pause is still in effect.
        product.thumbnail_job.retry_at = utc_now() - timedelta(seconds=1)
        db_session.commit()
    assert courier[0] == []
    tick[0] += 601
    batch = queue_product(db_session, product)
    assert jobs._run_thumbnail(product.id, product.user_id, batch) == "complete"
    db_session.expire_all()
    assert product.thumbnail_job.attempts == 1
    assert len(courier[0]) == 1


def test_cdn_429_shares_pause_and_preserves_remaining_thumbnail_demand(db_session, factory, monkeypatch):
    from services.marketplace_access import get_access_store
    products = factory(count=2)
    batch = queue_product(db_session, products[0])
    calls = []

    class Response:
        status = 429
        headers = {"Retry-After": "900"}
        closed = False

        def close(self):
            self.closed = True

        def release_conn(self):
            self.closed = True

    response = Response()

    def throttled(url, headers):
        calls.append(url)
        return response

    monkeypatch.setattr("services.image_service._open_pinned_image_response", throttled)
    monkeypatch.setattr("services.image_service.download_external_image", guarded_image_download)
    assert jobs._run_thumbnail(products[0].id, products[0].user_id, batch) == "failed"
    assert jobs._run_thumbnail(products[1].id, products[1].user_id, batch) == "failed"
    db_session.expire_all()
    assert products[0].thumbnail_job.attempts == 1
    assert products[1].thumbnail_job.attempts == 0
    assert len(calls) == 1
    assert response.closed
    assert 895 < (products[1].thumbnail_job.retry_at - utc_now()).total_seconds() <= 900
    assert get_access_store().states["recordcity"]["pause"] > 0


def test_consecutive_queue_failure_has_its_own_five_attempt_limit(db_session, factory, monkeypatch):
    product = factory()[0]
    monkeypatch.setattr(jobs, "_dispatch_batch", lambda *a: (_ for _ in ()).throw(ConnectionError("queue unavailable")))
    monkeypatch.setattr(jobs, "_existing_batch_is_alive", lambda token: False)
    for expected in range(1, jobs.MAX_ATTEMPTS + 1):
        assert jobs.recover_thumbnail_jobs()["failed"] == 1
        db_session.expire_all()
        assert product.thumbnail_job.dispatch_attempts == expected
        assert product.thumbnail_job.attempts == 0
        product.thumbnail_job.retry_at = utc_now() - timedelta(seconds=1)
        db_session.commit()
    assert jobs.recover_thumbnail_jobs()["queued_images"] == 0


def test_three_physical_redirect_requests_spend_one_thumbnail_attempt(db_session, factory, courier, monkeypatch):
    from services.marketplace_access import request_budget
    product = factory()[0]
    batch = queue_product(db_session, product)

    class Response:
        def __init__(self, status, headers):
            self.status = status
            self.headers = headers
            self.closed = False

        def stream(self, chunk_size):
            yield png_bytes()

        def close(self):
            self.closed = True

        def release_conn(self):
            self.closed = True

    responses = [Response(302, {"Location": "/image/two.png"}),
        Response(307, {"Location": "/image/three.png"}), Response(200, {"Content-Type": "image/png"})]
    opened = []

    def fetch(url, headers):
        opened.append(url)
        return responses[len(opened) - 1]

    monkeypatch.setattr("services.image_service._open_pinned_image_response", fetch)
    monkeypatch.setattr("services.image_service.download_external_image", guarded_image_download)
    with request_budget(max_requests=3, max_seconds=30) as budget:
        assert jobs._run_thumbnail(product.id, product.user_id, batch) == "complete"
        assert budget.requests == 3
    db_session.expire_all()
    assert product.thumbnail_job.attempts == 1
    assert len(opened) == 3 and all(response.closed for response in responses)
    assert product.snapshots[0].image_urls == courier[1][0]


def test_exhausted_budget_before_first_physical_hop_spends_zero_thumbnail_attempts(db_session, factory, monkeypatch):
    from services.marketplace_access import request_budget
    product = factory()[0]
    batch = queue_product(db_session, product)
    monkeypatch.setattr("services.image_service.download_external_image", guarded_image_download)
    monkeypatch.setattr("services.image_service._open_pinned_image_response", lambda *a, **k: pytest.fail("budget must reject before HTTP"))
    with request_budget(max_requests=0, max_seconds=30):
        assert jobs._run_thumbnail(product.id, product.user_id, batch) == "failed"
    db_session.expire_all()
    assert product.thumbnail_job.attempts == 0
    assert product.thumbnail_job.error_code == "access_wait"
    assert product.thumbnail_job.retry_at > utc_now()


@pytest.mark.parametrize("url", ["https://files.recordcity.jp.evil.example/image.jpg", "http://files.recordcity.jp/image.jpg", "https://cdn.snkrdunk.com/image.jpg"])
def test_thumbnail_demand_rejects_unapproved_source_images(db_session, factory, url):
    product = factory()[0]
    snapshot = product.snapshots[0]
    db_session.delete(product.thumbnail_job)
    db_session.flush()
    snapshot.image_urls = url
    assert not jobs.create_thumbnail_demand(db_session, product, snapshot)
    db_session.commit()
    assert db_session.get(ProductThumbnailJob, product.id) is None
