"""E2E tests for the translation API routes using a fake translator backend."""
from __future__ import annotations

import pytest

from database import SessionLocal
from models import Product, TranslationSuggestion, User


class FakeTranslatorBackend:
    """Deterministic translator used in tests."""

    name = "fake"

    def translate_plain(self, text: str) -> str:
        if not text or not text.strip():
            return ""
        return f"EN[{text.strip()}]"

    def translate_html(self, html: str) -> str:
        if not html or not html.strip():
            return ""
        from services.translator.html_segmenter import iter_html_text_segments

        soup, segments = iter_html_text_segments(html)
        for segment in segments:
            segment.apply(f"EN[{segment.text}]")
        return str(soup)


@pytest.fixture(autouse=True)
def fake_backend(monkeypatch):
    """Replace the translator singleton with a deterministic fake."""
    from services.translator import registry as translator_registry

    backend = FakeTranslatorBackend()
    monkeypatch.setattr(
        translator_registry, "get_translator_backend", lambda: backend
    )
    monkeypatch.setattr(
        "jobs.translation_tasks.get_translator_backend",
        lambda: backend,
    )
    translator_registry.reset_translator_backend_for_tests()
    yield backend
    translator_registry.reset_translator_backend_for_tests()


def _login(client, db_session, username="translation_tester"):
    user = User(username=username)
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    client.post(
        "/login",
        data={"username": username, "password": "testpassword"},
    )
    return user


def _create_product(db_session, user, *, title="日本語タイトル", description="<p>説明文</p>"):
    product = Product(
        user_id=user.id,
        site="manual",
        source_url=f"https://example.com/{user.id}/{title}",
        last_title=title,
        custom_title=title,
        custom_description=description,
        status="draft",
    )
    db_session.add(product)
    db_session.commit()
    return product


def test_enqueue_translation_runs_inline_on_inmemory_backend(client, db_session):
    user = _login(client, db_session)
    product = _create_product(db_session, user)

    response = client.post(
        f"/api/products/{product.id}/translate",
        json={"scope": "full"},
    )
    assert response.status_code == 201, response.data
    body = response.get_json()
    assert body["backend"] in {"inmemory", "rq"}
    suggestion_payload = body["suggestion"]
    assert suggestion_payload["status"] == "succeeded"
    assert suggestion_payload["translated_title"] == "EN[日本語タイトル]"
    assert "EN[説明文]" in suggestion_payload["translated_description"]

    stored = (
        db_session.query(TranslationSuggestion)
        .filter_by(product_id=product.id)
        .all()
    )
    assert len(stored) == 1
    assert stored[0].status == "succeeded"


def test_enqueue_translation_rejects_unknown_scope(client, db_session):
    user = _login(client, db_session, "bad_scope_tester")
    product = _create_product(db_session, user)

    response = client.post(
        f"/api/products/{product.id}/translate",
        json={"scope": "nope"},
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_scope"


def test_enqueue_translation_rejects_empty_source(client, db_session):
    user = _login(client, db_session, "empty_source_tester")
    product = Product(
        user_id=user.id,
        site="manual",
        source_url=f"https://example.com/empty/{user.id}",
        status="draft",
    )
    db_session.add(product)
    db_session.commit()

    response = client.post(
        f"/api/products/{product.id}/translate",
        json={"scope": "full"},
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "empty_source"


def test_enqueue_translation_denies_other_users_products(client, db_session):
    other_user = User(username="other_translation_user")
    other_user.set_password("testpassword")
    db_session.add(other_user)
    db_session.commit()
    product = _create_product(db_session, other_user, title="他人の商品")

    user = _login(client, db_session, "requesting_user")
    assert user.id != other_user.id

    response = client.post(
        f"/api/products/{product.id}/translate",
        json={"scope": "title"},
    )
    assert response.status_code == 404


def test_list_translation_suggestions_returns_recent_rows(client, db_session):
    user = _login(client, db_session, "list_suggestions_tester")
    product = _create_product(db_session, user)

    # enqueue two suggestions
    client.post(f"/api/products/{product.id}/translate", json={"scope": "title"})
    client.post(f"/api/products/{product.id}/translate", json={"scope": "full"})

    response = client.get(f"/api/products/{product.id}/translation-suggestions")
    assert response.status_code == 200
    items = response.get_json()["items"]
    assert len(items) == 2
    for item in items:
        assert item["product_id"] == product.id
        assert item["status"] in {"queued", "running", "succeeded", "failed"}


def test_apply_suggestion_copies_fields_and_sets_source_hash(client, db_session):
    user = _login(client, db_session, "apply_tester")
    product = _create_product(db_session, user)

    enqueue_resp = client.post(
        f"/api/products/{product.id}/translate",
        json={"scope": "full"},
    )
    job_id = enqueue_resp.get_json()["job_id"]

    apply_resp = client.post(f"/api/translation-suggestions/{job_id}/apply")
    assert apply_resp.status_code == 200, apply_resp.data

    db_session.expire_all()
    refreshed = db_session.query(Product).filter_by(id=product.id).one()
    assert refreshed.custom_title_en == "EN[日本語タイトル]"
    assert "EN[説明文]" in (refreshed.custom_description_en or "")
    assert refreshed.custom_title_en_source_hash
    assert refreshed.custom_description_en_source_hash

    suggestion = (
        db_session.query(TranslationSuggestion).filter_by(job_id=job_id).one()
    )
    assert suggestion.status == "applied"


def test_apply_rejects_suggestion_not_ready(client, db_session):
    user = _login(client, db_session, "not_ready_tester")
    product = _create_product(db_session, user)

    suggestion = TranslationSuggestion(
        job_id="not-ready-job",
        product_id=product.id,
        user_id=user.id,
        scope="full",
        provider="fake",
        source_title="x",
        status="queued",
    )
    db_session.add(suggestion)
    db_session.commit()

    response = client.post("/api/translation-suggestions/not-ready-job/apply")
    assert response.status_code == 409
    assert response.get_json()["error"] == "suggestion_not_ready"


def test_reject_marks_suggestion_rejected(client, db_session):
    user = _login(client, db_session, "reject_tester")
    product = _create_product(db_session, user)

    enqueue_resp = client.post(
        f"/api/products/{product.id}/translate",
        json={"scope": "title"},
    )
    job_id = enqueue_resp.get_json()["job_id"]

    reject_resp = client.post(f"/api/translation-suggestions/{job_id}/reject")
    assert reject_resp.status_code == 200
    assert reject_resp.get_json()["status"] == "rejected"

    db_session.expire_all()
    suggestion = (
        db_session.query(TranslationSuggestion).filter_by(job_id=job_id).one()
    )
    assert suggestion.status == "rejected"


def test_translation_pipeline_strips_malicious_html_before_storing(client, db_session):
    """Script/onerror payloads in scraped source must never survive translation."""
    user = _login(client, db_session, "xss_guard_tester")
    product = _create_product(
        db_session,
        user,
        description='<p>こんにちは</p><script>alert(1)</script>'
                    '<img src=x onerror="alert(document.cookie)">',
    )

    response = client.post(
        f"/api/products/{product.id}/translate",
        json={"scope": "description"},
    )
    assert response.status_code == 201, response.data
    suggestion = response.get_json()["suggestion"]
    translated = suggestion["translated_description"] or ""

    assert "<script" not in translated.lower()
    assert "onerror" not in translated.lower()
    assert "<img" not in translated.lower()

    # Applying must also not reintroduce unsafe HTML.
    job_id = suggestion["job_id"]
    apply_resp = client.post(f"/api/translation-suggestions/{job_id}/apply")
    assert apply_resp.status_code == 200

    db_session.expire_all()
    refreshed = db_session.query(Product).filter_by(id=product.id).one()
    stored = refreshed.custom_description_en or ""
    assert "<script" not in stored.lower()
    assert "onerror" not in stored.lower()


def test_reject_denies_other_users_suggestion(client, db_session):
    owner = User(username="suggestion_owner")
    owner.set_password("testpassword")
    db_session.add(owner)
    db_session.commit()
    product = _create_product(db_session, owner, title="所有者の商品")

    suggestion = TranslationSuggestion(
        job_id="other-user-job",
        product_id=product.id,
        user_id=owner.id,
        scope="title",
        provider="fake",
        source_title="x",
        status="succeeded",
        translated_title="y",
    )
    db_session.add(suggestion)
    db_session.commit()

    _login(client, db_session, "unauthorised_user")
    response = client.post("/api/translation-suggestions/other-user-job/reject")
    assert response.status_code == 404
