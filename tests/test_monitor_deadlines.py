from datetime import timedelta
import time

import pytest

from database import create_isolated_session
from models import Product, User
from services.monitor_service import MonitorService
from services.operation_deadline import OperationDeadlineExceeded
from services.patrol_supervisor import PatrolSupervisorQuarantined
from services.patrol.base_patrol import PatrolResult
from time_utils import utc_now


def _products(db_session, count=2):
    user = User(username="bounded_monitor")
    user.set_password("testpassword")
    db_session.add(user)
    db_session.flush()
    old = utc_now() - timedelta(days=1)
    rows = [Product(user_id=user.id, site="mercari",
                    source_url=f"https://jp.mercari.com/item/m-bounded-{index}",
                    last_title="Existing", last_price=100, last_status="on_sale",
                    archived=False, is_listed=True, created_at=old, updated_at=old)
            for index in range(count)]
    db_session.add_all(rows)
    db_session.commit()
    return rows


def test_supervised_fetch_holds_no_parent_database_transaction(client, db_session, monkeypatch):
    import services.monitor_service as module
    rows = _products(db_session, count=1)
    created = []
    def new_session():
        session = create_isolated_session()
        created.append(session)
        return session
    monkeypatch.setattr(module, "create_isolated_session", new_session)
    def fetch(site, url, *, deadline, cancelled):
        assert created
        assert all(not session.in_transaction() for session in created)
        assert deadline - time.monotonic() <= 110
        return PatrolResult(price=200, status="active")
    summary = MonitorService.check_stale_products(limit=1, fetcher=fetch)
    assert summary["successful_count"] == 1
    db_session.expire_all()
    assert db_session.get(Product, rows[0].id).last_price == 200


def test_scheduler_loss_drops_fetch_result_and_leaves_remaining_products_untouched(client, db_session):
    rows = _products(db_session)
    stopped = [False]
    def fetch(site, url, *, deadline, cancelled):
        stopped[0] = True
        return PatrolResult(price=999, status="sold")
    summary = MonitorService.check_stale_products(limit=2, fetcher=fetch, cancelled=lambda: stopped[0])
    assert summary["status"] == "cancelled"
    assert summary["attempted_count"] == 1
    assert summary["successful_count"] == 0
    assert summary["unprocessed_count"] == 2
    db_session.expire_all()
    assert all(db_session.get(Product, row.id).last_price == 100 for row in rows)
    assert all(db_session.get(Product, row.id).last_patrolled_at is None for row in rows)


def test_batch_deadline_reports_partial_counts_and_leaves_unprocessed_cursor(client, db_session, monkeypatch):
    import services.monitor_service as module
    rows = _products(db_session)
    clock = [1000.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    def fetch(site, url, *, deadline, cancelled):
        clock[0] += 10
        return PatrolResult(price=200, status="active")
    summary = MonitorService.check_stale_products(limit=2, fetcher=fetch, deadline=1031)
    assert summary["status"] == "deadline_exceeded"
    assert summary["successful_count"] == 1
    assert summary["attempted_count"] == 1
    assert summary["unprocessed_count"] == 1
    db_session.expire_all()
    assert db_session.get(Product, rows[0].id).last_patrolled_at is not None
    assert db_session.get(Product, rows[1].id).last_patrolled_at is None


@pytest.mark.parametrize("change", ["source_url", "last_patrolled_at", "owner"])
def test_current_row_lock_and_snapshot_reject_stale_result(client, db_session, change):
    rows = _products(db_session, count=1)
    product_id = rows[0].id
    owner = User(username="replacement_owner")
    owner.set_password("testpassword")
    db_session.add(owner)
    db_session.commit()
    owner_id = owner.id
    def fetch(site, url, *, deadline, cancelled):
        session = create_isolated_session()
        try:
            product = session.get(Product, product_id)
            if change == "source_url":
                product.source_url = "https://jp.mercari.com/item/m-corrected"
            elif change == "last_patrolled_at":
                product.last_patrolled_at = utc_now()
            else:
                product.user_id = owner_id
            session.commit()
        finally:
            session.close()
        return PatrolResult(price=999, status="sold")
    summary = MonitorService.check_stale_products(limit=1, fetcher=fetch)
    assert summary["discarded_count"] == 1
    assert summary["successful_count"] == 0
    assert summary["updated_count"] == 0
    db_session.expire_all()
    assert db_session.get(Product, product_id).last_price == 100
    assert db_session.get(Product, product_id).last_status == "on_sale"


def test_timed_out_item_backoff_preserves_data_then_next_product_runs(client, db_session):
    rows = _products(db_session)
    attempted = []
    def fetch(site, url, *, deadline, cancelled):
        attempted.append(url)
        if len(attempted) == 1:
            raise OperationDeadlineExceeded("bounded fetch terminated")
        return PatrolResult(price=200, status="active")
    summary = MonitorService.check_stale_products(limit=2, fetcher=fetch)
    assert summary["status"] == "completed"
    assert summary["error_count"] == 1
    assert summary["successful_count"] == 1
    assert summary["unprocessed_count"] == 0
    db_session.expire_all()
    failed = db_session.get(Product, rows[0].id)
    healthy = db_session.get(Product, rows[1].id)
    assert failed.last_price == 100
    assert failed.last_status == "on_sale"
    assert failed.patrol_fail_count == 1
    assert failed.next_patrol_at > utc_now()
    assert healthy.last_price == 200


def test_attempt_checkpoint_commits_safe_retry_cursor_before_hung_fetch(client, db_session, monkeypatch):
    rows = _products(db_session, count=1)
    product_id = rows[0].id
    def fetch(url):
        session = create_isolated_session()
        try:
            product = session.get(Product, product_id)
            assert product.last_patrolled_at is not None
            assert product.next_patrol_at > utc_now()
            assert product.last_price == 100
            assert product.last_status == "on_sale"
        finally:
            session.close()
        return PatrolResult(price=200, status="active")
    monkeypatch.setattr(MonitorService._patrols["mercari"], "fetch", fetch)
    summary = MonitorService.check_stale_products(limit=1, checkpoint_attempts=True)
    assert summary["successful_count"] == 1
    db_session.expire_all()
    assert db_session.get(Product, product_id).last_price == 200
    assert db_session.get(Product, product_id).next_patrol_at is None


def test_caught_adapter_cleanup_error_aborts_remaining_work(client, db_session):
    rows = _products(db_session)
    attempts = []
    def fetch(site, url, **kwargs):
        attempts.append(url)
        return PatrolResult(error="cleanup did not complete")
    def resource_guard():
        raise PatrolSupervisorQuarantined("Browser cleanup unknown")
    summary = MonitorService.check_stale_products(limit=2, fetcher=fetch, resource_guard=resource_guard)
    assert summary["status"] == "quarantined"
    assert len(attempts) == 1
    assert summary["unprocessed_count"] == 2
    assert summary["successful_count"] == 0
    db_session.expire_all()
    assert all(db_session.get(Product, row.id).last_price == 100 for row in rows)


def test_two_batches_keep_soft_sold_threshold_across_owned_child_retirement(client, db_session, monkeypatch):
    from services.patrol_supervisor import SupervisedPatrolRunner, _bounded_soft_sold_counts
    rows = _products(db_session, count=1)
    product_id = rows[0].id
    monkeypatch.setattr(MonitorService, "_mercari_soft_sold_counts", {})
    monkeypatch.setattr(MonitorService._patrols["mercari"], "fetch", lambda _: PatrolResult(
        price=100, status="sold", evidence_strength="soft", reason="soft evidence"))
    first = MonitorService.check_stale_products(limit=1, checkpoint_attempts=True)
    assert first["inconclusive_count"] == 1
    db_session.expire_all()
    assert db_session.get(Product, product_id).last_status == "on_sale"

    # JSON transport changes object keys to strings. A fresh child contains no
    # prior monitor objects; only validated committed scalar counts carry over.
    parent = object.__new__(SupervisedPatrolRunner)
    parent._accept_batch_result({"retire": True, "summary": first,
                                "soft_sold_counts": {str(product_id): 1}})
    MonitorService._mercari_soft_sold_counts = _bounded_soft_sold_counts(parent._soft_sold_counts)
    second = MonitorService.check_stale_products(limit=1, checkpoint_attempts=True)
    assert second["successful_count"] == 1
    assert second["inconclusive_count"] == 0
    db_session.expire_all()
    assert db_session.get(Product, product_id).last_status == "sold"

    parent._accept_batch_result({"retire": True, "summary": {"status": "quarantined"},
                                "soft_sold_counts": {str(product_id): 2}})
    assert parent._soft_sold_counts == {}
