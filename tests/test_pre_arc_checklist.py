import json
from datetime import UTC, datetime
from pathlib import Path

from models import Product, ProductSnapshot, User, Variant
from services.product_service import save_scraped_items_to_db


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "pre_arc"


class StaticQueue:
    def __init__(self, jobs):
        self.jobs = jobs

    def get_status(self, job_id, user_id=None):
        job = self.jobs.get(job_id)
        if job is None:
            return None
        if user_id is not None and job["user_id"] != user_id:
            return None
        return {key: value for key, value in job.items() if key != "user_id"}

    def get_jobs_for_user(self, user_id, limit=10, include_terminal=True):
        jobs = [
            {key: value for key, value in job.items() if key != "user_id"}
            for job in self.jobs.values()
            if job["user_id"] == user_id
        ]
        jobs.sort(key=lambda job: job["created_at"], reverse=True)
        return jobs[:limit]


def _fixture_text(name):
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _snapshot_json(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _normalize_csv(text):
    return text.replace("\r\n", "\n")


def _login_user(client, db_session, username):
    user = User(username=username)
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()

    client.post(
        "/login",
        data={"username": username, "password": "testpassword"},
    )
    return user


def _seed_export_product(client, db_session, username):
    user = _login_user(client, db_session, username)

    product = Product(
        user_id=user.id,
        site="mercari",
        source_url="https://jp.mercari.com/item/m54321",
        last_title="Export Test Product",
        last_price=2000,
        last_status="on_sale",
        status="active",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db_session.add(product)
    db_session.commit()

    variant = Variant(
        product_id=product.id,
        option1_value="Default Title",
        sku="EXPORT-SKU",
        price=2000,
        inventory_qty=1,
        position=1,
    )
    db_session.add(variant)

    snapshot = ProductSnapshot(
        product_id=product.id,
        title="Export Test Product",
        price=2000,
        status="on_sale",
        description="Test description",
        scraped_at=datetime.now(UTC),
    )
    db_session.add(snapshot)
    db_session.commit()

    return user, product, variant


def test_pre_arc_api_scrape_status_completed_snapshot(client, db_session, monkeypatch):
    user = _login_user(client, db_session, "pre_arc_status_user")
    queue = StaticQueue(
        {
            "job-1": {
                "job_id": "job-1",
                "status": "completed",
                "site": "mercari",
                "result": {"items": [], "persist_to_db": True},
                "error": None,
                "elapsed_seconds": 0.1,
                "queue_position": None,
                "context": {
                    "site_label": "メルカリ",
                    "detail_label": "キーワード: persist",
                    "limit": 10,
                    "limit_label": "10件",
                    "persist_to_db": True,
                },
                "created_at": 10.0,
                "finished_at": 12.0,
                "user_id": user.id,
            }
        }
    )

    monkeypatch.setattr("routes.api.get_queue", lambda: queue)

    response = client.get("/api/scrape/status/job-1")

    assert response.status_code == 200
    assert _snapshot_json(response.json) == _fixture_text("api_scrape_status_completed.json").strip()


def test_pre_arc_api_scrape_jobs_preview_snapshot(client, db_session, monkeypatch):
    user = _login_user(client, db_session, "pre_arc_jobs_user")
    queue = StaticQueue(
        {
            "job-1": {
                "job_id": "job-1",
                "status": "queued",
                "site": "mercari",
                "result": None,
                "error": None,
                "elapsed_seconds": 0.1,
                "queue_position": 1,
                "context": {
                    "site_label": "メルカリ",
                    "detail_label": "キーワード: preview",
                    "limit": 10,
                    "limit_label": "10件",
                    "persist_to_db": False,
                },
                "created_at": 10.0,
                "finished_at": None,
                "user_id": user.id,
            }
        }
    )

    monkeypatch.setattr("routes.api.get_queue", lambda: queue)

    response = client.get("/api/scrape/jobs")

    assert response.status_code == 200
    assert _snapshot_json(response.json) == _fixture_text("api_scrape_jobs_preview.json").strip()


def test_pre_arc_shopify_export_matches_golden(client, db_session):
    _seed_export_product(client, db_session, "pre_arc_shopify_export_user")

    response = client.get("/export/shopify")

    assert response.status_code == 200
    assert response.content_type == "text/csv"
    assert _normalize_csv(response.data.decode("utf-8")) == _fixture_text("export_shopify_single.csv")


def test_pre_arc_ebay_export_matches_golden(client, db_session):
    _seed_export_product(client, db_session, "pre_arc_ebay_export_user")

    response = client.get("/export_ebay")

    assert response.status_code == 200
    assert "text/csv" in response.content_type
    actual = _normalize_csv(response.data.decode("utf-8"))
    assert actual == "\ufeff" + _fixture_text("export_ebay_single.csv")


def test_pre_arc_save_scraped_items_normalizes_source_url_identity(client, db_session):
    user = _login_user(client, db_session, "pre_arc_product_contract_user")

    first_counts = save_scraped_items_to_db(
        [
            {
                "url": "https://jp.mercari.com/item/m-pre-arc?ref=campaign",
                "title": "First Title",
                "price": 1200,
                "status": "on_sale",
                "description": "first snapshot",
                "image_urls": ["https://img.example.com/first.jpg"],
            }
        ],
        user_id=user.id,
        site="mercari",
    )
    second_counts = save_scraped_items_to_db(
        [
            {
                "url": "https://jp.mercari.com/item/m-pre-arc?foo=bar",
                "title": "Updated Title",
                "price": 1500,
                "status": "on_sale",
                "description": "second snapshot",
                "image_urls": ["https://img.example.com/second.jpg"],
            }
        ],
        user_id=user.id,
        site="mercari",
    )

    assert first_counts == (1, 0)
    assert second_counts == (0, 1)

    db_session.expire_all()
    products = db_session.query(Product).filter_by(user_id=user.id).all()

    assert len(products) == 1
    product = products[0]
    assert product.source_url == "https://jp.mercari.com/item/m-pre-arc"
    assert product.last_title == "Updated Title"
    assert product.last_price == 1500

    snapshots = (
        db_session.query(ProductSnapshot)
        .filter_by(product_id=product.id)
        .order_by(ProductSnapshot.id.asc())
        .all()
    )

    assert len(snapshots) == 2
    assert snapshots[0].title == "First Title"
    assert snapshots[1].title == "Updated Title"
