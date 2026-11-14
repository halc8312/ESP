"""Exercise restrictive product FKs with SQLite enforcement actually enabled."""
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from jobs.translation_tasks import _persist_translation_result
from models import (CatalogRequest, CatalogRequestItem, ImageProcessingJob,
                    PriceList, PriceListItem, Product, ProductSnapshot,
                    ProductThumbnailJob, TranslationSuggestion, User, Variant)
from routes import trash
from services.translator.suggestion_store import (apply_suggestion_to_product,
    create_suggestion, mark_failed, mark_running)
from time_utils import utc_now


@pytest.fixture
def fk_world(client, db_session):
    engine = db_session.get_bind()

    def enable_fks(connection, record, proxy):
        connection.execute("PRAGMA foreign_keys=ON")

    event.listen(engine, "checkout", enable_fks)
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    owners = [User(username=f"fk-purge-owner-{i}", password_hash="hash") for i in range(2)]
    db_session.add_all(owners)
    db_session.commit()
    with client.session_transaction() as session:
        session["_user_id"] = str(owners[0].id)
        session["_fresh"] = True
    serial = [0]

    def product(*, owner=0, days=40, trashed=True, children=True):
        serial[0] += 1
        row = Product(user_id=owners[owner].id, site="manual",
            source_url=f"https://example.com/fk-{serial[0]}", last_title="原文",
            deleted_at=utc_now() - timedelta(days=days) if trashed else None)
        if children:
            row.variants.append(Variant(price=1000, inventory_qty=1))
            row.snapshots.append(ProductSnapshot(title="原文", price=1000, status="available"))
        db_session.add(row)
        db_session.commit()
        return row

    def suggestion(row, *, status="queued", owner=None, job_id=None):
        suggestion = TranslationSuggestion(product_id=row.id,
            user_id=row.user_id if owner is None else owners[owner].id,
            job_id=job_id or f"fk-job-{row.id}-{status}", provider="fake", scope="title",
            source_title="原文", translated_title="English", status=status,
            auto_apply=True, worker_token="worker-a" if status == "running" else None,
            lease_expires_at=utc_now() + timedelta(minutes=30) if status == "running" else None)
        db_session.add(suggestion)
        db_session.commit()
        return suggestion

    yield SimpleNamespace(client=client, session=db_session, owners=owners,
                          product=product, suggestion=suggestion, engine=engine)
    event.remove(engine, "checkout", enable_fks)


def test_fk_is_valid_and_raw_product_delete_is_rejected(fk_world):
    row = fk_world.product(children=False)
    suggestion = fk_world.suggestion(row)
    fk = next(key for key in inspect(fk_world.engine).get_foreign_keys("translation_suggestions")
              if key["constrained_columns"] == ["product_id"])
    assert fk["referred_table"] == "products"
    assert fk.get("options", {}).get("ondelete", "NO ACTION") == "NO ACTION"
    with pytest.raises(IntegrityError):
        fk_world.session.query(Product).filter_by(id=row.id).delete(synchronize_session=False)
    fk_world.session.rollback()
    assert fk_world.session.get(TranslationSuggestion, suggestion.id) is not None
    assert fk_world.session.get(Product, row.id) is not None


@pytest.mark.parametrize("status", ["queued", "running", "succeeded", "failed", "applied", "rejected"])
def test_manual_purge_removes_all_translation_states_in_same_transaction(fk_world, status):
    row = fk_world.product(days=2)
    pid = row.id
    suggestion = fk_world.suggestion(row, status=status)
    sid = suggestion.id
    assert fk_world.client.post("/trash/purge", data={"id": str(pid)}).status_code == 302
    fk_world.session.expire_all()
    assert fk_world.session.get(Product, pid) is None
    assert fk_world.session.get(TranslationSuggestion, sid) is None
    assert fk_world.session.query(ProductSnapshot).filter_by(product_id=pid).count() == 0
    assert fk_world.session.query(Variant).filter_by(product_id=pid).count() == 0


def test_automatic_purge_removes_image_jobs_membership_but_keeps_customer_snapshots(fk_world):
    row = fk_world.product()
    pid = row.id
    fk_world.suggestion(row)
    price_list = PriceList(user_id=row.user_id, name="Kept list", token="kept-fk-list")
    fk_world.session.add(price_list)
    fk_world.session.flush()
    fk_world.session.add(PriceListItem(price_list_id=price_list.id, product_id=pid))
    image_job = ImageProcessingJob(job_id="fk-image-job", product_id=pid, user_id=row.user_id,
                                  source_image_url="https://example.com/image.jpg", status="succeeded")
    request = CatalogRequest(reference="fk-request", user_id=row.user_id,
        pricelist_name="Original name", buyer_instagram="buyer", submission_key="fk-submission",
        payload_hash="hash")
    request.items.append(CatalogRequestItem(product_id=pid, title_snapshot="Historical title",
                                           price_jpy_snapshot=2000, quantity=2))
    fk_world.session.add_all([image_job, request])
    fk_world.session.commit()
    request_id, item_id, price_list_id = request.id, request.items[0].id, price_list.id
    assert trash.purge_old_trash() == 1
    fk_world.session.expire_all()
    assert fk_world.session.get(Product, pid) is None
    assert fk_world.session.query(TranslationSuggestion).filter_by(product_id=pid).count() == 0
    assert fk_world.session.query(ImageProcessingJob).filter_by(product_id=pid).count() == 0
    assert fk_world.session.query(PriceListItem).filter_by(product_id=pid).count() == 0
    assert fk_world.session.get(PriceList, price_list_id).name == "Kept list"
    assert fk_world.session.get(CatalogRequest, request_id) is not None
    snapshot = fk_world.session.get(CatalogRequestItem, item_id)
    assert snapshot.product_id is None
    assert (snapshot.title_snapshot, snapshot.price_jpy_snapshot, snapshot.quantity) == ("Historical title", 2000, 2)


def test_soft_delete_and_restore_keep_suggestion_history(fk_world):
    row = fk_world.product(trashed=False)
    pid = row.id
    suggestion = fk_world.suggestion(row, status="succeeded")
    sid = suggestion.id
    assert fk_world.client.post("/trash/delete", data={"id": str(pid)}).status_code == 302
    fk_world.session.expire_all()
    assert fk_world.session.get(TranslationSuggestion, sid) is not None
    assert fk_world.client.post("/trash/restore", data={"id": str(pid)}).status_code == 302
    fk_world.session.expire_all()
    assert fk_world.session.get(Product, pid).deleted_at is None
    assert fk_world.session.get(TranslationSuggestion, sid) is not None


@pytest.mark.parametrize("reference", ["translation", "image", "pricelist", "thumbnail"])
def test_mismatched_tenant_reference_fails_closed_and_does_not_block_other_product(fk_world, reference):
    row = fk_world.product()
    pid = row.id
    if reference == "translation":
        fk_world.suggestion(row, owner=1)
    elif reference == "image":
        fk_world.session.add(ImageProcessingJob(job_id="foreign-image", product_id=pid,
            user_id=fk_world.owners[1].id, source_image_url="https://example.com/img.jpg", status="succeeded"))
    elif reference == "pricelist":
        price_list = PriceList(user_id=fk_world.owners[1].id, name="Foreign list", token="foreign-fk-list")
        fk_world.session.add(price_list)
        fk_world.session.flush()
        fk_world.session.add(PriceListItem(price_list_id=price_list.id, product_id=pid))
    else:
        fk_world.session.add(ProductThumbnailJob(product_id=pid, user_id=fk_world.owners[1].id,
            product_source_url=row.source_url, source_snapshot_id=row.snapshots[0].id,
            source_image_url="https://example.com/img.jpg", state="complete"))
    fk_world.session.commit()
    eligible = fk_world.product()
    eligible_id = eligible.id
    fk_world.suggestion(eligible)
    assert trash.purge_old_trash() == 1
    fk_world.session.expire_all()
    assert fk_world.session.get(Product, pid) is not None
    assert fk_world.session.get(Product, eligible_id) is None
    assert fk_world.session.query(ProductSnapshot).filter_by(product_id=pid).count() == 1


def test_user_cannot_purge_foreign_product_or_active_product(fk_world):
    rows = [fk_world.product(owner=1), fk_world.product(trashed=False)]
    ids = [row.id for row in rows]
    for row in rows:
        fk_world.suggestion(row)
    assert fk_world.client.post("/trash/purge", data={"id": [str(pid) for pid in ids]}).status_code == 302
    fk_world.session.expire_all()
    assert all(fk_world.session.get(Product, pid) is not None for pid in ids)


def test_failed_purge_rolls_back_parent_and_all_children(fk_world, monkeypatch):
    row = fk_world.product()
    pid = row.id
    suggestion_id = fk_world.suggestion(row).id
    original = trash._purge_product

    def fail_after_deletion(session, product):
        original(session, product)
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(trash, "_purge_product", fail_after_deletion)
    with pytest.raises(RuntimeError, match="injected"):
        fk_world.client.post("/trash/purge", data={"id": str(pid)})
    fk_world.session.expire_all()
    assert fk_world.session.get(Product, pid) is not None
    assert fk_world.session.get(TranslationSuggestion, suggestion_id) is not None
    assert fk_world.session.query(ProductSnapshot).filter_by(product_id=pid).count() == 1
    assert fk_world.session.query(Variant).filter_by(product_id=pid).count() == 1


@pytest.mark.parametrize("state", ["queued", "running"])
def test_background_removal_physical_reservation_survives_purge(fk_world, state):
    row = fk_world.product()
    pid = row.id
    job = ImageProcessingJob(job_id="reserved-bg-job", product_id=pid, user_id=row.user_id,
        source_image_url="https://example.com/img.jpg", status=state)
    fk_world.session.add(job)
    fk_world.session.commit()
    assert trash.purge_old_trash() == 0
    assert fk_world.client.post("/trash/purge", data={"id": str(pid)}).status_code == 302
    fk_world.session.expire_all()
    assert fk_world.session.get(Product, pid) is not None
    assert fk_world.session.query(ImageProcessingJob).filter_by(job_id="reserved-bg-job").one().status == state


@pytest.mark.parametrize("auto_apply", [False, True])
def test_late_worker_result_after_purge_is_noop_and_cannot_recreate_rows(fk_world, auto_apply):
    row = fk_world.product()
    pid = row.id
    job_id = fk_world.suggestion(row, status="running").job_id
    assert trash.purge_old_trash() == 1
    assert _persist_translation_result(job_id=job_id, worker_token="worker-a",
        translated_title="Late result", translated_description=None, auto_apply=auto_apply) is False
    assert mark_failed(job_id, worker_token="worker-a", error_message="Late failure") is False
    assert mark_running(job_id, worker_token="worker-b") is False
    fk_world.session.expire_all()
    assert fk_world.session.get(Product, pid) is None
    assert fk_world.session.query(TranslationSuggestion).filter_by(job_id=job_id).count() == 0


def test_soft_deleted_product_cannot_be_claimed_created_or_applied(fk_world):
    row = fk_world.product()
    queued = fk_world.suggestion(row)
    succeeded = fk_world.suggestion(row, status="succeeded")
    assert mark_running(queued.job_id, worker_token="worker-a") is False
    assert apply_suggestion_to_product(succeeded, fk_world.session) == {}
    fk_world.session.commit()
    with pytest.raises(ValueError, match="live owned"):
        create_suggestion(session=fk_world.session, job_id="new-trash-translation",
            product_id=row.id, user_id=row.user_id, scope="title", provider="fake",
            source_title="原文", source_description=None, source_title_hash=None,
            source_description_hash=None)
    assert fk_world.client.post(f"/api/products/{row.id}/translate", json={"scope": "title"}).status_code == 404
    fk_world.session.expire_all()
    assert fk_world.session.get(Product, row.id).custom_title_en is None
    assert fk_world.session.get(TranslationSuggestion, succeeded.id).status == "succeeded"


def test_translation_creation_and_worker_claim_reject_cross_tenant_parent(fk_world):
    row = fk_world.product(trashed=False)
    wrong = fk_world.suggestion(row, owner=1)
    assert mark_running(wrong.job_id, worker_token="foreign-worker") is False
    with pytest.raises(ValueError, match="live owned"):
        create_suggestion(session=fk_world.session, job_id="new-foreign-translation",
            product_id=row.id, user_id=fk_world.owners[1].id, scope="title", provider="fake",
            source_title="原文", source_description=None, source_title_hash=None,
            source_description_hash=None)
    assert apply_suggestion_to_product(wrong, fk_world.session) == {}


def test_parent_lock_is_postgres_for_update_before_auto_apply_child_write(fk_world):
    row = fk_world.product(trashed=False)
    suggestion = fk_world.suggestion(row, status="running")
    statements = []

    def observe(connection, cursor, statement, parameters, context, many):
        statements.append((statement, context.compiled.statement if context.compiled else None))

    event.listen(fk_world.engine, "before_cursor_execute", observe)
    try:
        assert _persist_translation_result(job_id=suggestion.job_id, worker_token="worker-a",
            translated_title="English", translated_description=None, auto_apply=True)
    finally:
        event.remove(fk_world.engine, "before_cursor_execute", observe)
    parent_index = next(i for i, (sql, _) in enumerate(statements) if "FROM products" in sql)
    child_index = next(i for i, (sql, _) in enumerate(statements)
                       if sql.startswith("UPDATE translation_suggestions"))
    assert parent_index < child_index
    compiled = str(statements[parent_index][1].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in compiled
    assert "products.deleted_at IS NULL" in compiled


def test_purge_covers_every_product_foreign_key_without_changing_restrictive_schema():
    referenced = {table.name for table in Product.metadata.tables.values()
                  for fk in table.foreign_keys if fk.column.table.name == "products"}
    assert referenced == {"variants", "product_snapshots", "product_thumbnail_jobs",
                          "price_list_items", "translation_suggestions",
                          "image_processing_jobs", "catalog_request_items"}
    # The incident was a missing application cleanup, not an invalid FK or
    # proof of orphaned rows. Preserve the deployed NO ACTION migration.
    original = Path(__file__).resolve().parents[1] / "alembic/versions/20260420_0006_add_translation_suggestions.py"
    assert 'sa.ForeignKey("products.id")' in original.read_text()
