from __future__ import annotations

from datetime import timedelta

import pytest

from database import create_isolated_session
from models import Product, TranslationSuggestion, User
from services.translator.suggestion_store import (
    apply_suggestion_to_product,
    mark_failed,
    mark_running,
    mark_succeeded,
    mark_terminal_state,
    recover_expired_translation_suggestions,
)
from time_utils import utc_now


def _create_product_and_suggestion(
    db_session,
    *,
    username: str,
    job_id: str,
    status: str = "running",
    worker_token: str | None = "initial-worker",
) -> tuple[Product, TranslationSuggestion]:
    user = User(username=username)
    user.set_password("testpassword")
    db_session.add(user)
    db_session.flush()

    product = Product(
        user_id=user.id,
        site="manual",
        source_url=f"https://example.com/{job_id}",
        last_title="日本語タイトル",
        custom_title="日本語タイトル",
        status="draft",
    )
    db_session.add(product)
    db_session.flush()

    suggestion = TranslationSuggestion(
        job_id=job_id,
        product_id=product.id,
        user_id=user.id,
        scope="title",
        provider="fake",
        source_title=product.last_title,
        source_title_hash="new-source-hash",
        translated_title="New automatic title",
        auto_apply=True,
        status=status,
        worker_token=worker_token if status == "running" else None,
        lease_expires_at=(
            utc_now() + timedelta(minutes=30)
            if status == "running"
            else None
        ),
    )
    db_session.add(suggestion)
    db_session.commit()
    return product, suggestion


def test_only_one_worker_can_claim_a_queued_suggestion(db_session):
    _, suggestion = _create_product_and_suggestion(
        db_session,
        username="translation_single_worker_claim",
        job_id="translation-single-worker-claim",
        status="queued",
    )

    assert mark_running(suggestion.job_id, worker_token="worker-one") is True
    assert mark_running(suggestion.job_id, worker_token="worker-two") is False

    db_session.expire_all()
    assert db_session.get(TranslationSuggestion, suggestion.id).status == "running"


def test_expired_lease_can_be_reclaimed_and_fences_old_worker(db_session):
    _, suggestion = _create_product_and_suggestion(
        db_session,
        username="translation_expired_lease",
        job_id="translation-expired-lease",
        status="running",
        worker_token="old-worker",
    )
    suggestion.lease_expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()

    assert (
        mark_running(
            suggestion.job_id,
            worker_token="new-worker",
            lease_seconds=600,
        )
        is True
    )
    assert (
        mark_succeeded(
            suggestion.job_id,
            worker_token="old-worker",
            translated_title="Stale translation",
            translated_description=None,
        )
        is False
    )
    assert (
        mark_succeeded(
            suggestion.job_id,
            worker_token="new-worker",
            translated_title="Current translation",
            translated_description=None,
        )
        is True
    )

    db_session.expire_all()
    stored = db_session.get(TranslationSuggestion, suggestion.id)
    assert stored.status == "succeeded"
    assert stored.translated_title == "Current translation"
    assert stored.worker_token is None
    assert stored.lease_expires_at is None


def test_recovery_requeues_expired_translation_with_distinct_queue_id(
    db_session,
    monkeypatch,
):
    _, suggestion = _create_product_and_suggestion(
        db_session,
        username="translation_recovery",
        job_id="translation-recovery-original",
        status="running",
        worker_token="dead-worker",
    )
    suggestion.lease_expires_at = utc_now() - timedelta(minutes=1)
    db_session.commit()
    enqueued = []

    monkeypatch.setattr(
        "services.media_queue.resolve_queue_backend_name",
        lambda: "rq",
    )
    monkeypatch.setattr(
        "services.media_queue.enqueue_media_job",
        lambda **kwargs: enqueued.append(kwargs) or kwargs["job_id"],
    )

    summary = recover_expired_translation_suggestions(lease_seconds=600)

    assert summary["recovered"] == [suggestion.job_id]
    assert summary["enqueued"] == [suggestion.job_id]
    assert summary["failed"] == []
    assert enqueued[0]["job_id"].startswith("translation-recovery-")
    assert enqueued[0]["job_id"] != suggestion.job_id
    assert enqueued[0]["args"] == (suggestion.job_id,)
    db_session.expire_all()
    stored = db_session.get(TranslationSuggestion, suggestion.id)
    assert stored.status == "queued"
    assert stored.worker_token is None
    assert stored.lease_expires_at is None


def test_recovery_enqueue_error_keeps_suggestion_claimable_by_original_rq_entry(
    db_session,
    monkeypatch,
):
    _, suggestion = _create_product_and_suggestion(
        db_session,
        username="translation_recovery_ambiguous_enqueue",
        job_id="translation-recovery-ambiguous-enqueue",
        status="queued",
    )
    stale_time = utc_now() - timedelta(hours=1)
    suggestion.updated_at = stale_time
    db_session.commit()

    monkeypatch.setattr(
        "services.media_queue.resolve_queue_backend_name",
        lambda: "rq",
    )

    def ambiguous_enqueue(**_kwargs):
        # Redis/RQ may have accepted either this recovery job or the original
        # queued entry before the client observed a transport failure.
        raise ConnectionError("connection dropped after enqueue")

    monkeypatch.setattr(
        "services.media_queue.enqueue_media_job",
        ambiguous_enqueue,
    )

    summary = recover_expired_translation_suggestions(lease_seconds=600)

    assert summary["recovered"] == [suggestion.job_id]
    assert summary["enqueued"] == []
    assert summary["failed"] == [suggestion.job_id]
    db_session.expire_all()
    stored = db_session.get(TranslationSuggestion, suggestion.id)
    assert stored.status == "queued"
    assert stored.worker_token is None
    assert stored.lease_expires_at is None
    assert stored.completed_at is None
    assert stored.updated_at > stale_time
    assert "ConnectionError" in (stored.error_message or "")

    # A surviving original RQ entry must still be able to claim the row.
    assert mark_running(stored.job_id, worker_token="original-rq-worker") is True
    db_session.expire_all()
    running = db_session.get(TranslationSuggestion, suggestion.id)
    assert running.status == "running"
    assert running.error_message is None
    assert running.completed_at is None
    assert (
        mark_succeeded(
            running.job_id,
            worker_token="original-rq-worker",
            translated_title="Recovered by original queue entry",
            translated_description=None,
        )
        is True
    )
    db_session.expire_all()
    completed = db_session.get(TranslationSuggestion, suggestion.id)
    assert completed.status == "succeeded"
    assert completed.error_message is None
    assert completed.translated_title == "Recovered by original queue entry"


def test_auto_apply_crash_rolls_back_result_and_product_for_lease_recovery(
    db_session,
    monkeypatch,
):
    from jobs.translation_tasks import execute_translation_job
    from jobs import translation_tasks

    product, suggestion = _create_product_and_suggestion(
        db_session,
        username="translation_atomic_auto_apply_crash",
        job_id="translation-atomic-auto-apply-crash",
        status="queued",
    )
    suggestion.translated_title = None
    db_session.commit()

    class FakeBackend:
        def translate_plain(self, text):
            return "Atomic translated title"

    class SimulatedWorkerCrash(BaseException):
        pass

    monkeypatch.setattr(
        translation_tasks,
        "get_translator_backend",
        lambda: FakeBackend(),
    )
    real_apply = translation_tasks.apply_suggestion_to_product

    def crash_after_product_update(*args, **kwargs):
        changes = real_apply(*args, **kwargs)
        assert changes == {"custom_title_en": "Atomic translated title"}
        raise SimulatedWorkerCrash()

    monkeypatch.setattr(
        translation_tasks,
        "apply_suggestion_to_product",
        crash_after_product_update,
    )

    with pytest.raises(SimulatedWorkerCrash):
        execute_translation_job(suggestion.job_id)

    db_session.expire_all()
    stored_product = db_session.get(Product, product.id)
    stored_suggestion = db_session.get(TranslationSuggestion, suggestion.id)
    assert stored_product.custom_title_en is None
    assert stored_suggestion.status == "running"
    assert stored_suggestion.translated_title is None
    assert stored_suggestion.worker_token
    assert stored_suggestion.lease_expires_at is not None

    stored_suggestion.lease_expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    enqueued = []
    monkeypatch.setattr(
        "services.media_queue.resolve_queue_backend_name",
        lambda: "rq",
    )
    monkeypatch.setattr(
        "services.media_queue.enqueue_media_job",
        lambda **kwargs: enqueued.append(kwargs) or kwargs["job_id"],
    )

    summary = recover_expired_translation_suggestions(lease_seconds=600)

    assert summary["recovered"] == [suggestion.job_id]
    assert summary["enqueued"] == [suggestion.job_id]
    assert enqueued[0]["args"] == (suggestion.job_id,)
    db_session.expire_all()
    recovered = db_session.get(TranslationSuggestion, suggestion.id)
    assert recovered.status == "queued"
    assert recovered.worker_token is None
    assert recovered.translated_title is None


@pytest.mark.parametrize("worker_result", ["succeeded", "failed"])
def test_rejection_cas_wins_over_stale_worker_write(
    db_session,
    worker_result,
):
    _, suggestion = _create_product_and_suggestion(
        db_session,
        username=f"translation_reject_cas_{worker_result}",
        job_id=f"translation-reject-cas-{worker_result}",
    )

    # The worker observed ``running`` before the operator made a decision.
    worker_session = create_isolated_session()
    operator_session = create_isolated_session()
    try:
        stale_worker_view = worker_session.get(TranslationSuggestion, suggestion.id)
        assert stale_worker_view.status == "running"
        # End SQLite's read transaction while retaining the fact that this
        # worker proceeded from an earlier view of the row.
        worker_session.commit()

        terminal = mark_terminal_state(
            suggestion.job_id,
            status="rejected",
            user_id=suggestion.user_id,
            session=operator_session,
        )
        operator_session.commit()
        assert terminal is not None

        if worker_result == "succeeded":
            updated = mark_succeeded(
                suggestion.job_id,
                worker_token="initial-worker",
                translated_title="Late result",
                translated_description=None,
                session=worker_session,
            )
        else:
            updated = mark_failed(
                suggestion.job_id,
                worker_token="initial-worker",
                error_message="late failure",
                session=worker_session,
            )
        worker_session.commit()
        assert updated is False
    finally:
        operator_session.close()
        worker_session.close()

    db_session.expire_all()
    stored = db_session.get(TranslationSuggestion, suggestion.id)
    assert stored.status == "rejected"
    assert stored.translated_title == "New automatic title"
    assert stored.error_message is None


def test_auto_apply_cas_preserves_intentionally_cleared_title(db_session):
    product, suggestion = _create_product_and_suggestion(
        db_session,
        username="translation_manual_empty_cas",
        job_id="translation-manual-empty-cas",
        status="succeeded",
    )

    worker_session = create_isolated_session()
    try:
        worker_suggestion = worker_session.get(
            TranslationSuggestion,
            suggestion.id,
        )
        stale_product = worker_session.get(Product, product.id)
        assert stale_product.custom_title_en is None
        assert stale_product.custom_title_en_manually_edited is False

        # Simulate a manual clear landing after the worker loaded Product.
        # synchronize_session=False deliberately keeps the worker's ORM view
        # stale; only the UPDATE predicate can prevent the lost update.
        (
            worker_session.query(Product)
            .filter(Product.id == product.id)
            .update(
                {
                    Product.custom_title_en: None,
                    Product.custom_title_en_source_hash: None,
                    Product.custom_title_en_manually_edited: True,
                },
                synchronize_session=False,
            )
        )

        changes = apply_suggestion_to_product(
            worker_suggestion,
            worker_session,
            preserve_existing=True,
        )
        worker_session.commit()
    finally:
        worker_session.close()

    assert changes == {}
    db_session.expire_all()
    stored_product = db_session.get(Product, product.id)
    stored_suggestion = db_session.get(TranslationSuggestion, suggestion.id)
    assert stored_product.custom_title_en is None
    assert stored_product.custom_title_en_manually_edited is True
    assert stored_suggestion.status == "succeeded"


def test_inline_manual_empty_blocks_late_auto_apply(client, db_session):
    from jobs.translation_tasks import _auto_apply_suggestion

    product, suggestion = _create_product_and_suggestion(
        db_session,
        username="translation_inline_manual_empty",
        job_id="translation-inline-manual-empty",
        status="succeeded",
    )
    client.post(
        "/login",
        data={
            "username": "translation_inline_manual_empty",
            "password": "testpassword",
        },
    )

    response = client.patch(
        f"/api/products/{product.id}/inline-update",
        json={"field": "custom_title_en", "value": ""},
    )
    assert response.status_code == 200

    _auto_apply_suggestion(suggestion.job_id)

    db_session.expire_all()
    stored_product = db_session.get(Product, product.id)
    stored_suggestion = db_session.get(TranslationSuggestion, suggestion.id)
    assert stored_product.custom_title_en is None
    assert stored_product.custom_title_en_manually_edited is True
    assert stored_suggestion.status == "succeeded"


def test_auto_apply_cas_checks_expected_value_even_for_legacy_writer(db_session):
    product, suggestion = _create_product_and_suggestion(
        db_session,
        username="translation_expected_value_cas",
        job_id="translation-expected-value-cas",
        status="succeeded",
    )

    worker_session = create_isolated_session()
    try:
        worker_suggestion = worker_session.get(
            TranslationSuggestion,
            suggestion.id,
        )
        stale_product = worker_session.get(Product, product.id)
        assert stale_product.custom_title_en is None

        # A legacy/concurrent writer that does not know about the ownership
        # flag is still protected by the expected target value in the CAS.
        (
            worker_session.query(Product)
            .filter(Product.id == product.id)
            .update(
                {Product.custom_title_en: "Concurrent manual title"},
                synchronize_session=False,
            )
        )

        changes = apply_suggestion_to_product(
            worker_suggestion,
            worker_session,
            preserve_existing=True,
        )
        worker_session.commit()
    finally:
        worker_session.close()

    assert changes == {}
    db_session.expire_all()
    stored_product = db_session.get(Product, product.id)
    stored_suggestion = db_session.get(TranslationSuggestion, suggestion.id)
    assert stored_product.custom_title_en == "Concurrent manual title"
    assert stored_suggestion.status == "succeeded"


def test_explicit_apply_can_replace_manual_value_and_releases_ownership(
    client,
    db_session,
):
    product, suggestion = _create_product_and_suggestion(
        db_session,
        username="translation_explicit_apply_manual",
        job_id="translation-explicit-apply-manual",
        status="succeeded",
    )
    product.custom_title_en = "Manual title"
    product.custom_title_en_manually_edited = True
    db_session.commit()

    client.post(
        "/login",
        data={
            "username": "translation_explicit_apply_manual",
            "password": "testpassword",
        },
    )
    response = client.post(
        f"/api/translation-suggestions/{suggestion.job_id}/apply",
        json={"apply_title": True, "apply_description": False},
    )

    assert response.status_code == 200
    db_session.expire_all()
    stored_product = db_session.get(Product, product.id)
    stored_suggestion = db_session.get(TranslationSuggestion, suggestion.id)
    assert stored_product.custom_title_en == "New automatic title"
    assert stored_product.custom_title_en_manually_edited is False
    assert stored_suggestion.status == "applied"
