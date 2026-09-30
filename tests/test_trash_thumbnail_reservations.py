from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from models import Product, ProductSnapshot, ProductThumbnailJob, User, Variant
from routes.trash import purge_old_trash
from services import product_thumbnail_jobs as jobs
from time_utils import utc_now


@pytest.fixture
def world(client, db_session, monkeypatch):
    owners = [User(username=f"purge-owner-{index}", password_hash="hash") for index in range(2)]
    db_session.add_all(owners)
    db_session.commit()
    with client.session_transaction() as session:
        session["_user_id"] = str(owners[0].id)
        session["_fresh"] = True
    monkeypatch.setattr(jobs, "_existing_batch_is_alive", lambda *a: pytest.fail("purge must not probe Redis"))
    monkeypatch.setattr("services.image_service.download_external_image", lambda *a, **k: pytest.fail("purge must not fetch images"))
    serial = [1000]

    def create(*, owner=0, ledger=True, job_id="physical-batch", state="queued", days=5):
        serial[0] += 1
        product = Product(user_id=owners[owner].id, site="recordcity",
            source_url=f"https://www.recordcity.jp/ja/catalog/{serial[0]}",
            last_title="Trashed record", last_price=1200, last_status="unknown",
            deleted_at=utc_now() - timedelta(days=days))
        product.variants.append(Variant(option1_value="Default Title", price=1200, inventory_qty=0))
        snapshot = ProductSnapshot(title="Trashed record", price=1200, status="unknown", description="Keep snapshot",
            image_urls=f"https://files.recordcity.jp/image/{serial[0]}.jpg")
        product.snapshots.append(snapshot)
        db_session.add(product)
        db_session.flush()
        if ledger:
            db_session.add(ProductThumbnailJob(product_id=product.id, user_id=product.user_id,
                product_source_url=product.source_url, source_snapshot_id=snapshot.id,
                source_image_url=snapshot.image_urls, state=state, job_id=job_id,
                batch_user_id=product.user_id, lease_expires_at=utc_now() - timedelta(seconds=1)))
        db_session.commit()
        return product.id

    return SimpleNamespace(client=client, session=db_session, owners=owners, create=create)


@pytest.mark.parametrize("state", ["pending", "queued", "running", "complete", "failed", "unknown"])
@pytest.mark.parametrize("scope", ["current", "changed_image", "changed_source", "foreign_capture"])
def test_manual_purge_preserves_any_physical_reservation_even_expired_or_stale(world, state, scope):
    product_id = world.create(state=state)
    product = world.session.get(Product, product_id)
    if scope == "changed_image":
        product.snapshots[0].image_urls = "/media/owner-edited.png"
    elif scope == "changed_source":
        product.source_url = "https://www.recordcity.jp/ja/catalog/9999"
    elif scope == "foreign_capture":
        product.thumbnail_job.user_id = world.owners[1].id
        product.thumbnail_job.batch_user_id = world.owners[1].id
    world.session.commit()

    response = world.client.post("/trash/purge", data={"id": str(product_id)})
    assert response.status_code == 302
    world.session.expire_all()
    assert world.session.get(Product, product_id) is not None
    assert world.session.query(ProductSnapshot).filter_by(product_id=product_id).count() == 1
    assert world.session.query(Variant).filter_by(product_id=product_id).count() == 1
    assert world.session.get(ProductThumbnailJob, product_id).job_id == "physical-batch"
    with world.client.session_transaction() as session:
        assert any("画像処理が終了してから再試行" in message for category, message in session.get("_flashes", []))


def test_foreign_owned_product_is_not_purged_or_disclosed(world):
    product_id = world.create(owner=1)
    assert world.client.post("/trash/purge", data={"id": str(product_id)}).status_code == 302
    world.session.expire_all()
    assert world.session.get(Product, product_id).user_id == world.owners[1].id
    assert world.session.get(ProductThumbnailJob, product_id).job_id == "physical-batch"
    with world.client.session_transaction() as session:
        assert not any("画像処理中" in message for category, message in session.get("_flashes", []))


def test_manual_purge_is_allowed_after_worker_drain(world):
    product_id = world.create()
    assert jobs.run_thumbnail_batch(world.owners[0].id, "physical-batch") == {"complete": 0, "failed": 0, "stale": 0}
    world.session.expire_all()
    assert world.session.get(ProductThumbnailJob, product_id).job_id is None
    assert world.client.post("/trash/purge", data={"id": str(product_id)}).status_code == 302
    world.session.expire_all()
    assert world.session.get(Product, product_id) is None
    assert world.session.get(ProductThumbnailJob, product_id) is None
    assert world.session.query(ProductSnapshot).filter_by(product_id=product_id).count() == 0
    assert world.session.query(Variant).filter_by(product_id=product_id).count() == 0


@pytest.mark.parametrize("ledger", [False, True])
def test_legacy_or_unreserved_rows_retain_manual_purge_behavior(world, ledger):
    product_id = world.create(ledger=ledger, job_id=None, state="complete")
    assert world.client.post("/trash/purge", data={"id": str(product_id)}).status_code == 302
    world.session.expire_all()
    assert world.session.get(Product, product_id) is None


def test_automatic_purge_skips_reserved_rows_and_only_deletes_eligible_unreserved_rows(world):
    active = world.create(days=40)
    foreign_active = world.create(owner=1, state="complete", days=40)
    unreserved = world.create(job_id=None, state="complete", days=40)
    legacy = world.create(ledger=False, days=40)
    recent = world.create(ledger=False, days=2)
    assert purge_old_trash() == 2
    world.session.expire_all()
    assert world.session.get(Product, active) is not None
    assert world.session.get(Product, foreign_active) is not None
    assert world.session.get(Product, recent) is not None
    assert world.session.get(Product, unreserved) is None
    assert world.session.get(Product, legacy) is None
    assert world.session.get(ProductThumbnailJob, active).job_id == "physical-batch"
    assert world.session.get(ProductThumbnailJob, foreign_active).job_id == "physical-batch"


@pytest.mark.parametrize("drift", ["restore", "owner"])
def test_purge_fence_rechecks_deletion_and_ownership_after_initial_read(world, drift):
    product_id = world.create(ledger=False)
    engine = world.session.get_bind()
    triggered = []

    def mutate(connection, cursor, statement, parameters, context, many):
        if triggered or not statement.startswith("UPDATE products SET deleted_at=products.deleted_at"):
            return
        triggered.append(True)
        if drift == "restore":
            connection.exec_driver_sql("UPDATE products SET deleted_at=NULL WHERE id=?", (product_id,))
        else:
            connection.exec_driver_sql("UPDATE products SET user_id=? WHERE id=?", (world.owners[1].id, product_id))

    event.listen(engine, "before_cursor_execute", mutate)
    try:
        assert world.client.post("/trash/purge", data={"id": str(product_id)}).status_code == 302
    finally:
        event.remove(engine, "before_cursor_execute", mutate)
    assert triggered
    world.session.expire_all()
    product = world.session.get(Product, product_id)
    assert product is not None
    assert world.session.query(ProductSnapshot).filter_by(product_id=product_id).count() == 1
    assert world.session.query(Variant).filter_by(product_id=product_id).count() == 1
    assert (product.deleted_at is None) if drift == "restore" else product.user_id == world.owners[1].id


def test_new_thumbnail_claim_is_rejected_for_soft_deleted_product_before_purge(world):
    product_id = world.create(job_id=None, state="pending")
    assert jobs.recover_thumbnail_jobs(user_id=world.owners[0].id)["queued_images"] == 0
    assert world.client.post("/trash/purge", data={"id": str(product_id)}).status_code == 302
    world.session.expire_all()
    assert world.session.get(Product, product_id) is None
