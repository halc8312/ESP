"""Independent capacity regressions for changed thumbnail demand captures."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from models import Product, ProductSnapshot, ProductThumbnailJob, Shop, User, Variant
from services import product_thumbnail_jobs as jobs
from time_utils import utc_now


@pytest.fixture
def world(db_session, monkeypatch):
    owners = [User(username=f"capacity-owner-{index}", password_hash="hash") for index in range(6)]
    db_session.add_all(owners)
    db_session.flush()
    shops = [Shop(user_id=owner.id, name=f"Capacity shop {index}") for index, owner in enumerate(owners)]
    db_session.add_all(shops)
    db_session.commit()
    tick = [utc_now()]
    monkeypatch.setattr(jobs, "utc_now", lambda: tick[0])
    dispatched = []
    monkeypatch.setattr(jobs, "_dispatch_batch", lambda *args: dispatched.append(args))
    monkeypatch.setattr("services.image_service.download_external_image", lambda *a, **k: pytest.fail("stale work must never fetch images"))
    serial = [1000]

    def create(owner_index=0):
        serial[0] += 1
        product = Product(user_id=owners[owner_index].id, shop_id=shops[owner_index].id,
            site="recordcity", source_url=f"https://www.recordcity.jp/ja/catalog/{serial[0]}",
            last_title="Captured listing", last_price=1200, last_status="unknown", detail_fetch_state="pending")
        product.variants.append(Variant(option1_value="Default Title", price=1200, inventory_qty=0))
        snapshot = ProductSnapshot(title="Captured listing", price=1200, status="unknown", description="",
            image_urls=f"https://files.recordcity.jp/image/{serial[0]}.jpg", scraped_at=tick[0])
        product.snapshots.append(snapshot)
        db_session.add(product)
        db_session.flush()
        assert jobs.create_thumbnail_demand(db_session, product, snapshot)
        db_session.commit()
        return product

    def queue(product):
        result = jobs.recover_thumbnail_jobs(user_id=product.user_id, limit_batches=1)
        assert result["queued_batches"] == 1
        db_session.expire_all()
        return product.thumbnail_job.job_id

    def expire(product):
        product.thumbnail_job.lease_expires_at = tick[0] - timedelta(seconds=1)
        db_session.commit()

    def rebind(product, kind="owner"):
        if kind == "owner":
            product.user_id = owners[1].id
            product.shop_id = shops[1].id
        elif kind == "shop":
            shop = Shop(user_id=product.user_id, name="Corrected owned shop")
            db_session.add(shop)
            db_session.flush()
            product.shop_id = shop.id
        elif kind == "source":
            product.source_url = "https://www.recordcity.jp/ja/catalog/9999"
            product.snapshots[0].image_urls = "https://files.recordcity.jp/image/9999.jpg"
        else:
            product.snapshots.append(ProductSnapshot(title="New listing snapshot", price=1200,
                status="unknown", description="", image_urls="https://files.recordcity.jp/image/new.jpg",
                scraped_at=tick[0] + timedelta(seconds=1)))
            db_session.flush()
        snapshot = max(product.snapshots, key=lambda row: (row.scraped_at, row.id))
        assert jobs.create_thumbnail_demand(db_session, product, snapshot)
        db_session.commit()
        return product.thumbnail_job

    return SimpleNamespace(owners=owners, shops=shops, tick=tick, dispatched=dispatched,
        create=create, queue=queue, expire=expire, rebind=rebind, session=db_session)


@pytest.fixture
def physical_queue(monkeypatch):
    """Exercise the real conservative RQ probe without any Redis connection."""
    from redis import Redis
    from rq.exceptions import NoSuchJobError
    from rq.job import Job

    states = {}
    probed = []
    monkeypatch.setattr(jobs, "resolve_queue_backend_name", lambda: "rq")
    monkeypatch.setattr(Redis, "from_url", lambda *a, **k: object())

    def fetch(job_id, connection):
        probed.append(job_id)
        state = states.get(job_id, "started")
        if state == "missing":
            raise NoSuchJobError("synthetic missing batch")
        if state == "inspection_error":
            raise ConnectionError("synthetic probe unavailable")
        return SimpleNamespace(get_status=lambda refresh: state)

    monkeypatch.setattr(Job, "fetch", fetch)
    return SimpleNamespace(states=states, probed=probed)


@pytest.mark.parametrize("state", ["queued", "running"])
@pytest.mark.parametrize("physical_state", ["started", None, "inspection_error"])
def test_changed_same_snapshot_image_retains_live_capacity_then_releases_when_gone(world, physical_queue, state, physical_state):
    old = world.create()
    old_batch = world.queue(old)
    replacement = world.create()
    row = old.thumbnail_job
    row.state = state
    row.claim_token = "old-upload-token" if state == "running" else None
    old.snapshots[0].image_urls = "/media/owner-edited.png"
    world.expire(old)
    physical_queue.states[old_batch] = physical_state

    assert jobs.recover_thumbnail_jobs(user_id=old.user_id)["queued_batches"] == 0
    world.session.expire_all()
    assert old_batch in physical_queue.probed
    assert old.thumbnail_job.job_id == old_batch
    assert replacement.thumbnail_job.state == "pending"
    assert jobs.authorize_thumbnail_delivery(world.session, old.id, "old-upload-token") is None
    world.session.rollback()
    assert jobs._run_thumbnail(old.id, old.user_id, old_batch) == "stale"

    physical_queue.states[old_batch] = "missing"
    world.expire(old)
    result = jobs.recover_thumbnail_jobs(user_id=old.user_id)
    world.session.expire_all()
    assert result["queued_batches"] == 1 and result["queued_images"] == 1
    assert replacement.thumbnail_job.job_id != old_batch
    assert old.thumbnail_job.job_id is None
    assert old.snapshots[0].image_urls == "/media/owner-edited.png"
    assert old.last_price == 1200 and old.variants[0].inventory_qty == 0


@pytest.mark.parametrize("kind", ["owner", "shop", "source", "snapshot"])
def test_rebinding_demand_keeps_original_batch_owner_until_definite_finish(world, physical_queue, kind):
    old = world.create()
    original_owner = old.user_id
    old_batch = world.queue(old)
    original_lease = old.thumbnail_job.lease_expires_at
    world.rebind(old, kind)
    replacement = world.create()
    world.session.expire_all()
    assert old.thumbnail_job.job_id == old_batch
    assert old.thumbnail_job.batch_user_id == original_owner
    assert old.thumbnail_job.lease_expires_at == original_lease
    assert old.thumbnail_job.state == "pending" and old.thumbnail_job.claim_token is None
    assert jobs.recover_thumbnail_jobs(user_id=original_owner)["queued_batches"] == 0
    assert jobs.recover_thumbnail_jobs(user_id=old.user_id)["queued_batches"] == 0

    physical_queue.states[old_batch] = "inspection_error"
    world.expire(old)
    assert jobs.recover_thumbnail_jobs(user_id=original_owner)["queued_batches"] == 0
    assert jobs.recover_thumbnail_jobs(user_id=old.user_id)["queued_batches"] == 0
    assert old_batch in physical_queue.probed
    physical_queue.states[old_batch] = "finished"
    world.expire(old)
    result = jobs.recover_thumbnail_jobs(user_id=old.user_id)
    assert result["queued_batches"] == 1
    world.session.expire_all()
    assert old.thumbnail_job.job_id != old_batch
    assert old.thumbnail_job.batch_user_id == old.user_id
    assert old.detail_fetch_state == "pending" and old.variants[0].inventory_qty == 0
    if kind == "owner":
        assert jobs.recover_thumbnail_jobs(user_id=original_owner)["queued_batches"] == 1
        world.session.expire_all()
        assert replacement.thumbnail_job.state == "queued"


def test_new_owner_can_use_own_other_demand_without_overwriting_old_batch_capture(world, physical_queue):
    old = world.create()
    original_owner = old.user_id
    old_batch = world.queue(old)
    world.rebind(old, "owner")
    other_new_owner_demand = world.create(1)
    other_old_owner_demand = world.create(0)

    result = jobs.recover_thumbnail_jobs(user_id=old.user_id)
    assert result["queued_images"] == 1
    world.session.expire_all()
    assert other_new_owner_demand.thumbnail_job.state == "queued"
    assert old.thumbnail_job.job_id == old_batch and old.thumbnail_job.state == "pending"
    assert old.thumbnail_job.batch_user_id == original_owner
    assert jobs.recover_thumbnail_jobs(user_id=original_owner)["queued_images"] == 0
    world.session.expire_all()
    assert other_old_owner_demand.thumbnail_job.state == "pending"
    assert len(world.dispatched) == 2


def test_global_five_batch_limit_survives_rebinding_and_unknown_rq_state(world, physical_queue):
    products = [world.create(index) for index in range(5)]
    batches = [world.queue(product) for product in products]
    changed = products[0]
    changed.user_id = world.owners[5].id
    changed.shop_id = world.shops[5].id
    assert jobs.create_thumbnail_demand(world.session, changed, changed.snapshots[0])
    world.session.commit()
    for product in products:
        world.expire(product)
    physical_queue.states[batches[0]] = None

    assert jobs.recover_thumbnail_jobs(user_id=changed.user_id)["queued_batches"] == 0
    assert len(world.dispatched) == 5
    world.session.expire_all()
    assert changed.thumbnail_job.batch_user_id == world.owners[0].id
    assert changed.thumbnail_job.state == "pending" and changed.thumbnail_job.job_id == batches[0]

    physical_queue.states[batches[0]] = "missing"
    world.expire(changed)
    assert jobs.recover_thumbnail_jobs(user_id=changed.user_id)["queued_batches"] == 1
    world.session.expire_all()
    assert changed.thumbnail_job.batch_user_id == changed.user_id
    assert changed.thumbnail_job.job_id != batches[0]
    assert len(world.dispatched) == 6
    assert len({row.job_id for row in world.session.query(ProductThumbnailJob) if row.job_id}) == 5


def test_worker_drained_rebound_batch_releases_old_owner_and_refills_both_owners(world, physical_queue):
    old = world.create()
    original_owner = old.user_id
    old_batch = world.queue(old)
    world.rebind(old, "owner")
    replacement = world.create()

    # This is the original batch's worker reaching its end after the selected
    # product moved scope. There is no image IO left when capacity is released.
    assert jobs.run_thumbnail_batch(original_owner, old_batch) == {"complete": 0, "failed": 0, "stale": 0}
    world.session.expire_all()
    assert old.thumbnail_job.state == "queued" and old.thumbnail_job.job_id != old_batch
    assert old.thumbnail_job.batch_user_id == old.user_id
    assert replacement.thumbnail_job.state == "queued"
    assert replacement.thumbnail_job.batch_user_id == original_owner
    assert len(world.dispatched) == 3


def test_rebind_during_image_io_holds_capacity_until_worker_drains(world, physical_queue, monkeypatch):
    from database import create_isolated_session

    old = world.create()
    original_owner = old.user_id
    old_batch = world.queue(old)
    replacement = world.create()
    delivered = []

    def download(url, **kwargs):
        with kwargs["request_admission"](url):
            world.session.expire_all()
            assert old.thumbnail_job.state == "running"
            world.rebind(old, "owner")
            assert jobs.recover_thumbnail_jobs(user_id=original_owner)["queued_images"] == 0
            assert jobs.recover_thumbnail_jobs(user_id=old.user_id)["queued_images"] == 0
            assert len(world.dispatched) == 1
            assert old.thumbnail_job.job_id == old_batch
        return b"synthetic image bytes", ".png"

    def reject_stale_upload(product_id, token, index, content, **kwargs):
        session = create_isolated_session()
        try:
            assert jobs.authorize_thumbnail_delivery(session, product_id, token) is None
        finally:
            session.close()
        delivered.append("rejected")
        raise ValueError("stale synthetic upload")

    monkeypatch.setattr("services.image_service.download_external_image", download)
    monkeypatch.setattr("services.product_image_delivery.deliver_image_bytes", reject_stale_upload)
    assert jobs.run_thumbnail_batch(original_owner, old_batch) == {"complete": 0, "failed": 0, "stale": 1}
    world.session.expire_all()
    assert delivered == ["rejected"]
    assert old.thumbnail_job.job_id != old_batch and old.thumbnail_job.state == "queued"
    assert old.thumbnail_job.batch_user_id == old.user_id
    assert replacement.thumbnail_job.state == "queued"
    assert len(world.dispatched) == 3
    assert old.snapshots[0].image_urls.startswith("https://files.recordcity.jp/")
    assert old.last_price == 1200 and old.variants[0].inventory_qty == 0
