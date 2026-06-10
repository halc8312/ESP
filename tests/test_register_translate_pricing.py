"""Tests for Phase 3 registration options: translate + apply_pricing."""
from __future__ import annotations

import pytest

from models import PriceList, PricingRule, Product, TranslationSuggestion, User


class FakeTranslatorBackend:
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


class FakeQueue:
    def __init__(self):
        self.jobs = {}

    def get_status(self, job_id, user_id=None):
        job = self.jobs.get(job_id)
        if not job:
            return None
        if user_id is not None and job["user_id"] != user_id:
            return None
        return {k: v for k, v in job.items() if k != "user_id"}


@pytest.fixture(autouse=True)
def fake_backend(monkeypatch):
    from services.translator import registry as translator_registry

    backend = FakeTranslatorBackend()
    monkeypatch.setattr(translator_registry, "get_translator_backend", lambda: backend)
    monkeypatch.setattr(
        "jobs.translation_tasks.get_translator_backend",
        lambda: backend,
    )
    translator_registry.reset_translator_backend_for_tests()
    yield backend
    translator_registry.reset_translator_backend_for_tests()


def _login(client, db_session, username="phase3_tester"):
    user = User(username=username)
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    client.post("/login", data={"username": username, "password": "testpassword"})
    return user


def _make_completed_job(user_id, items=None):
    fake_queue = FakeQueue()
    fake_queue.jobs["job-p3"] = {
        "job_id": "job-p3",
        "status": "completed",
        "result": {
            "items": items
            if items is not None
            else [
                {
                    "url": "https://jp.mercari.com/item/m-p3-1",
                    "title": "テスト商品A",
                    "price": 3000,
                    "status": "on_sale",
                    "description": "<p>商品説明A</p>",
                    "image_urls": ["https://img.example.com/a.jpg"],
                },
                {
                    "url": "https://jp.mercari.com/item/m-p3-2",
                    "title": "テスト商品B",
                    "price": 5000,
                    "status": "on_sale",
                    "description": "<p>商品説明B</p>",
                    "image_urls": ["https://img.example.com/b.jpg"],
                },
            ],
            "site": "mercari",
        },
        "error": None,
        "elapsed_seconds": 0.1,
        "queue_position": None,
        "user_id": user_id,
    }
    return fake_queue


# -- apply_pricing tests --

def test_register_selected_apply_pricing_assigns_default_rule(client, db_session, monkeypatch):
    user = _login(client, db_session, "pricing_apply_user")
    rule = PricingRule(user_id=user.id, name="Default", margin_rate=30, shipping_cost=500, fixed_fee=100)
    db_session.add(rule)
    db_session.commit()

    user_row = db_session.query(User).filter_by(id=user.id).one()
    user_row.default_pricing_rule_id = rule.id
    db_session.commit()

    monkeypatch.setattr("routes.scrape.get_queue", lambda: _make_completed_job(user.id))

    response = client.post(
        "/scrape/register-selected",
        json={
            "job_id": "job-p3",
            "selected_indices": [0, 1],
            "apply_pricing": True,
        },
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["pricing_applied_count"] == 2

    db_session.expire_all()
    products = db_session.query(Product).filter_by(user_id=user.id).all()
    assert len(products) == 2
    for p in products:
        assert p.pricing_rule_id == rule.id
        assert p.selling_price is not None
        assert p.selling_price > 0


def test_register_selected_pricing_does_not_overwrite_existing_rule(client, db_session, monkeypatch):
    user = _login(client, db_session, "pricing_no_overwrite_user")

    rule1 = PricingRule(user_id=user.id, name="Rule1", margin_rate=50, shipping_cost=0, fixed_fee=0)
    rule2 = PricingRule(user_id=user.id, name="Default", margin_rate=30, shipping_cost=500, fixed_fee=100)
    db_session.add_all([rule1, rule2])
    db_session.commit()

    user_row = db_session.query(User).filter_by(id=user.id).one()
    user_row.default_pricing_rule_id = rule2.id
    db_session.commit()

    existing = Product(
        user_id=user.id,
        site="mercari",
        source_url="https://jp.mercari.com/item/m-p3-1",
        last_title="テスト商品A",
        last_price=3000,
        pricing_rule_id=rule1.id,
        selling_price=4500,
        status="draft",
    )
    db_session.add(existing)
    db_session.commit()

    monkeypatch.setattr("routes.scrape.get_queue", lambda: _make_completed_job(user.id))

    response = client.post(
        "/scrape/register-selected",
        json={
            "job_id": "job-p3",
            "selected_indices": [0, 1],
            "apply_pricing": True,
        },
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["pricing_applied_count"] == 1

    db_session.expire_all()
    p1 = db_session.query(Product).filter_by(source_url="https://jp.mercari.com/item/m-p3-1").first()
    assert p1.pricing_rule_id == rule1.id


def test_register_selected_pricing_noop_without_default(client, db_session, monkeypatch):
    user = _login(client, db_session, "pricing_no_default_user")
    monkeypatch.setattr("routes.scrape.get_queue", lambda: _make_completed_job(user.id))

    response = client.post(
        "/scrape/register-selected",
        json={
            "job_id": "job-p3",
            "selected_indices": [0],
            "apply_pricing": True,
        },
    )
    assert response.status_code == 200
    assert response.get_json()["pricing_applied_count"] == 0


# -- translate tests --

def test_register_selected_translate_creates_auto_apply_suggestion(client, db_session, monkeypatch):
    user = _login(client, db_session, "translate_auto_user")
    monkeypatch.setattr("routes.scrape.get_queue", lambda: _make_completed_job(user.id))

    response = client.post(
        "/scrape/register-selected",
        json={
            "job_id": "job-p3",
            "selected_indices": [0, 1],
            "translate": True,
        },
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["translation_jobs_enqueued"] == 2

    db_session.expire_all()
    suggestions = db_session.query(TranslationSuggestion).filter_by(user_id=user.id).all()
    assert len(suggestions) == 2
    for s in suggestions:
        assert s.auto_apply is True
        assert s.status == "applied"

    products = db_session.query(Product).filter_by(user_id=user.id).all()
    for p in products:
        assert p.custom_title_en is not None
        assert p.custom_title_en.startswith("EN[")


# -- register-to-pricelist + options --

def test_register_to_pricelist_with_translate_and_pricing(client, db_session, monkeypatch):
    user = _login(client, db_session, "pricelist_options_user")
    price_list = PriceList(user_id=user.id, name="Test PL", token="token-p3-opts")
    rule = PricingRule(user_id=user.id, name="Default", margin_rate=20, shipping_cost=300, fixed_fee=0)
    db_session.add_all([price_list, rule])
    db_session.commit()

    user_row = db_session.query(User).filter_by(id=user.id).one()
    user_row.default_pricing_rule_id = rule.id
    db_session.commit()

    monkeypatch.setattr("routes.scrape.get_queue", lambda: _make_completed_job(user.id))

    response = client.post(
        "/scrape/register-to-pricelist",
        json={
            "job_id": "job-p3",
            "selected_indices": [0],
            "price_list_id": price_list.id,
            "translate": True,
            "apply_pricing": True,
        },
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["translation_jobs_enqueued"] == 1
    assert body["pricing_applied_count"] == 1

    db_session.expire_all()
    products = db_session.query(Product).filter_by(user_id=user.id).all()
    assert len(products) == 1
    assert products[0].is_listed is False
    assert products[0].pricing_rule_id == rule.id
    assert products[0].custom_title_en is not None
