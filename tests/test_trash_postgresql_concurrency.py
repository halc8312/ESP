"""Opt-in real PostgreSQL races in a disposable, isolated test schema.

Only a local/CI database whose name contains ``test`` is accepted. Never set
ESP_TEST_POSTGRES_URL to a production database. No configured application
DATABASE_URL is used by these tests.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import database
from jobs.translation_tasks import _persist_translation_result
from models import Product, ProductSnapshot, TranslationSuggestion, User, Variant
from routes.trash import _lock_trashed_product, _purge_product
from services.product_purge import ProductPurgeOwnershipError
from services.translator.suggestion_store import (
    apply_suggestion_to_product, mark_failed, mark_running, mark_succeeded,
)
from time_utils import utc_now


@pytest.fixture
def pg_world(monkeypatch):
    raw_url = os.environ.get("ESP_TEST_POSTGRES_URL")
    if not raw_url:
        pytest.skip("ESP_TEST_POSTGRES_URL not configured; real PostgreSQL gate is opt-in")
    url = make_url(database.normalize_database_url(raw_url))
    if (url.get_backend_name() != "postgresql"
            or url.host not in {"localhost", "127.0.0.1", "::1", "postgres"}
            or "test" not in (url.database or "").lower()):
        pytest.fail("PostgreSQL integration tests require a local/CI host and a database name containing 'test'")

    schema = f"esp_fk_test_{uuid.uuid4().hex}"
    admin = create_engine(url, connect_args={"connect_timeout": 5})
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(url, pool_size=5, max_overflow=0, connect_args={
        "connect_timeout": 5,
        "options": f"-c search_path={schema} -c statement_timeout=5000 -c lock_timeout=3000",
    })
    factory = sessionmaker(bind=engine)
    runs = []
    gates = []

    def gate():
        value = threading.Event()
        gates.append(value)
        return value

    def session_for_thread():
        session = factory()
        session.execute(text("SELECT set_config('application_name', :name, false)"),
                        {"name": f"{schema}:{threading.current_thread().name}"})
        return session

    monkeypatch.setattr(database, "create_isolated_session", session_for_thread)

    def start(name, target):
        run = SimpleNamespace(value=None, error=None, finished=threading.Event())

        def execute():
            try:
                run.value = target()
            except BaseException as exc:
                run.error = exc
            finally:
                run.finished.set()

        run.thread = threading.Thread(name=name, target=execute, daemon=True)
        runs.append(run)
        run.thread.start()
        return run

    def join(run):
        assert run.finished.wait(8), "PostgreSQL test operation exceeded its bounded wait"
        run.thread.join(timeout=1)
        assert not run.thread.is_alive()
        if run.error is not None:
            raise run.error
        return run.value

    def await_blocked(pid, blocker_pid):
        # Observe a real server lock wait rather than assuming timing from a
        # sleep. The event wait bounds polling and permits prompt teardown.
        deadline = time.monotonic() + 2
        poll = threading.Event()
        with engine.connect() as connection:
            while time.monotonic() < deadline:
                blocked = connection.execute(text(
                    "SELECT :blocker = ANY(pg_blocking_pids(:pid))"
                ), {"pid": pid, "blocker": blocker_pid}).scalar_one()
                if blocked:
                    return
                poll.wait(timeout=0.01)
        pytest.fail("expected PostgreSQL parent-row lock wait was not observed")

    def create(*, trashed=True, foreign_suggestion=False, status="running"):
        with factory() as session:
            owners = [User(username=f"pg-owner-{uuid.uuid4().hex}", password_hash="hash")
                      for _ in range(2)]
            session.add_all(owners)
            session.flush()
            product = Product(user_id=owners[0].id, site="manual",
                source_url="https://example.com/pg-fk-test", last_title="Original",
                deleted_at=utc_now() - timedelta(days=40) if trashed else None)
            product.snapshots.append(ProductSnapshot(title="Original", price=1200, status="available"))
            product.variants.append(Variant(price=1200, inventory_qty=1))
            session.add(product)
            session.flush()
            suggestion = TranslationSuggestion(job_id=f"pg-job-{uuid.uuid4().hex}",
                product_id=product.id, user_id=owners[1 if foreign_suggestion else 0].id,
                scope="title", provider="fake", source_title="Original",
                translated_title="Translated", status=status, auto_apply=True,
                worker_token="worker-a" if status == "running" else None)
            session.add(suggestion)
            session.commit()
            return SimpleNamespace(product_id=product.id, suggestion_id=suggestion.id,
                owner_id=owners[0].id, foreign_owner_id=owners[1].id, job_id=suggestion.job_id)

    try:
        database.Base.metadata.create_all(engine)
        yield SimpleNamespace(engine=engine, factory=factory, session=session_for_thread,
                              start=start, join=join, gate=gate,
                              await_blocked=await_blocked, create=create)
    finally:
        for value in gates:
            value.set()
        for run in runs:
            run.thread.join(timeout=8)
        engine.dispose()
        # Only the UUID schema created above is dropped; public is never in
        # this engine's search_path and no application database is touched.
        with admin.begin() as connection:
            connection.exec_driver_sql("SET LOCAL lock_timeout = '3000ms'")
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def test_postgresql_purge_blocks_new_fk_insert_then_rejects_it(pg_world):
    target = pg_world.create()
    started = pg_world.gate()
    insert_pid = []
    with pg_world.session() as purge:
        parent = _lock_trashed_product(purge, target.product_id, user_id=target.owner_id)
        assert parent is not None
        purge_pid = purge.execute(text("SELECT pg_backend_pid()")).scalar_one()
        _purge_product(purge, parent)

        def insert():
            with pg_world.session() as session:
                insert_pid.append(session.execute(text("SELECT pg_backend_pid()")).scalar_one())
                started.set()
                session.add(TranslationSuggestion(job_id="late-insert", product_id=target.product_id,
                    user_id=target.owner_id, scope="title", provider="fake", status="queued"))
                try:
                    session.commit()
                except IntegrityError as exc:
                    session.rollback()
                    return exc.orig.diag.constraint_name
                return "unexpectedly committed"

        run = pg_world.start("pg-new-suggestion", insert)
        assert started.wait(2)
        pg_world.await_blocked(insert_pid[0], purge_pid)
        purge.commit()
    assert pg_world.join(run) == "translation_suggestions_product_id_fkey"
    with pg_world.factory() as session:
        assert session.get(Product, target.product_id) is None
        assert session.query(TranslationSuggestion).filter_by(product_id=target.product_id).count() == 0


def test_postgresql_auto_apply_and_soft_delete_purge_use_parent_first_order(pg_world):
    target = pg_world.create(trashed=False)
    parent_locked = pg_world.gate()
    release_apply = pg_world.gate()
    purge_started = pg_world.gate()
    apply_pid = []
    purge_pid = []
    intercepted = []

    def pause_before_child_write(connection, cursor, statement, parameters, context, many):
        if (threading.current_thread().name == "pg-auto-result" and not intercepted
                and statement.startswith("UPDATE translation_suggestions")):
            intercepted.append(True)
            apply_pid.append(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            parent_locked.set()
            assert release_apply.wait(5), "test did not release the parent-first apply transaction"

    event.listen(pg_world.engine, "before_cursor_execute", pause_before_child_write)
    try:
        apply_run = pg_world.start("pg-auto-result", lambda: _persist_translation_result(
            job_id=target.job_id, worker_token="worker-a", translated_title="English result",
            translated_description=None, auto_apply=True))
        assert parent_locked.wait(2)

        def soft_delete_then_purge():
            with pg_world.session() as session:
                purge_pid.append(session.execute(text("SELECT pg_backend_pid()")).scalar_one())
                purge_started.set()
                # This represents a deletion concurrent with a translation
                # that started while Product was still live.
                session.query(Product).filter_by(id=target.product_id, user_id=target.owner_id).update(
                    {Product.deleted_at: utc_now() - timedelta(days=40)}, synchronize_session=False)
                parent = _lock_trashed_product(session, target.product_id, user_id=target.owner_id)
                assert parent is not None
                _purge_product(session, parent)
                session.commit()
                return True

        purge_run = pg_world.start("pg-purge-after-delete", soft_delete_then_purge)
        assert purge_started.wait(2)
        pg_world.await_blocked(purge_pid[0], apply_pid[0])
        release_apply.set()
        assert pg_world.join(apply_run) is True
        assert pg_world.join(purge_run) is True
    finally:
        release_apply.set()
        event.remove(pg_world.engine, "before_cursor_execute", pause_before_child_write)
    with pg_world.factory() as session:
        assert session.get(Product, target.product_id) is None
        assert session.get(TranslationSuggestion, target.suggestion_id) is None


def test_postgresql_late_worker_and_loaded_manual_apply_are_noops_after_purge(pg_world):
    target = pg_world.create()
    loaded = pg_world.gate()
    release_worker = pg_world.gate()

    def late_result():
        with pg_world.session() as session:
            captured = session.get(TranslationSuggestion, target.suggestion_id)
            session.expunge(captured)
        loaded.set()
        assert release_worker.wait(5)
        with pg_world.session() as session:
            changes = apply_suggestion_to_product(captured, session)
            succeeded = mark_succeeded(target.job_id, worker_token="worker-a",
                translated_title="Late", translated_description=None, session=session)
            failed = mark_failed(target.job_id, worker_token="worker-a", error_message="Late", session=session)
            claimed = mark_running(target.job_id, worker_token="worker-b", session=session)
            session.commit()
        automatic = _persist_translation_result(job_id=target.job_id, worker_token="worker-a",
            translated_title="Late auto", translated_description=None, auto_apply=True)
        return changes, succeeded, failed, claimed, automatic

    run = pg_world.start("pg-late-result", late_result)
    assert loaded.wait(2)
    with pg_world.session() as session:
        parent = _lock_trashed_product(session, target.product_id, user_id=target.owner_id)
        _purge_product(session, parent)
        session.commit()
    release_worker.set()
    assert pg_world.join(run) == ({}, False, False, False, False)
    with pg_world.factory() as session:
        assert session.get(Product, target.product_id) is None
        assert session.get(TranslationSuggestion, target.suggestion_id) is None


def test_postgresql_purge_failure_rolls_back_parent_and_dependents(pg_world):
    target = pg_world.create()
    with pg_world.session() as session:
        parent = _lock_trashed_product(session, target.product_id, user_id=target.owner_id)
        _purge_product(session, parent)
        assert session.query(Product).filter_by(id=target.product_id).count() == 0
        assert session.query(TranslationSuggestion).filter_by(product_id=target.product_id).count() == 0
        session.rollback()
    with pg_world.factory() as session:
        assert session.get(Product, target.product_id) is not None
        assert session.get(TranslationSuggestion, target.suggestion_id) is not None
        assert session.query(ProductSnapshot).filter_by(product_id=target.product_id).count() == 1
        assert session.query(Variant).filter_by(product_id=target.product_id).count() == 1


def test_postgresql_purge_owner_validation_and_foreign_dependents_fail_closed(pg_world):
    target = pg_world.create(foreign_suggestion=True)
    with pg_world.session() as session:
        assert _lock_trashed_product(session, target.product_id, user_id=target.foreign_owner_id) is None
        parent = _lock_trashed_product(session, target.product_id, user_id=target.owner_id)
        with pytest.raises(ProductPurgeOwnershipError):
            _purge_product(session, parent)
        session.rollback()
    with pg_world.factory() as session:
        assert session.get(Product, target.product_id) is not None
        assert session.get(TranslationSuggestion, target.suggestion_id) is not None
        assert session.query(ProductSnapshot).filter_by(product_id=target.product_id).count() == 1
