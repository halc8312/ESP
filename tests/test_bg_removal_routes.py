"""E2E tests for the background-removal API routes using a fake backend."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from models import ImageProcessingJob, Product, ProductSnapshot, User
from services.bg_remover.internal_auth import (
    JOB_ID_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    compute_signature,
)


class FakeBgRemoverBackend:
    """Deterministic background-removal backend for tests."""

    name = "fake"

    def __init__(self):
        self.calls = []

    def remove_background(self, image_bytes: bytes) -> bytes:
        self.calls.append(len(image_bytes))
        return b"FAKE_PNG:" + image_bytes[:16]


@pytest.fixture(autouse=True)
def fake_backend(monkeypatch, tmp_path):
    """Replace the bg-remover singleton with a deterministic fake and
    direct the image storage path into a tmp dir so the route handlers
    can write result PNGs without touching real /var/data."""
    from services.bg_remover import registry as bg_registry
    from services import image_service

    backend = FakeBgRemoverBackend()
    bg_registry.set_bg_remover_backend_for_tests(backend)

    storage_dir = tmp_path / "images"
    storage_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(image_service, "IMAGE_STORAGE_PATH", str(storage_dir))
    monkeypatch.setattr(
        "routes.bg_removal.IMAGE_STORAGE_PATH", str(storage_dir)
    )

    yield backend
    bg_registry.reset_bg_remover_backend_for_tests()


def _login(client, db_session, username="bg_tester"):
    user = User(username=username)
    user.set_password("testpassword")
    db_session.add(user)
    db_session.commit()
    client.post(
        "/login", data={"username": username, "password": "testpassword"}
    )
    return user


def _create_product_with_images(db_session, user, image_urls):
    product = Product(
        user_id=user.id,
        site="manual",
        source_url=f"https://example.com/{user.id}/{image_urls[0]}",
        last_title="商品",
        custom_title="商品",
        status="draft",
    )
    db_session.add(product)
    db_session.commit()

    snap = ProductSnapshot(
        product_id=product.id,
        title="商品",
        image_urls="|".join(image_urls),
    )
    db_session.add(snap)
    db_session.commit()
    return product


def _write_local_image(monkeypatch, relative_name: str, content: bytes = b"FAKE_JPG") -> str:
    """Write ``content`` under IMAGE_STORAGE_PATH/product_images and return the public URL."""
    from routes import bg_removal as bg_routes

    storage_root = bg_routes.IMAGE_STORAGE_PATH
    dest_dir = os.path.join(storage_root, "product_images")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, relative_name)
    with open(dest, "wb") as fh:
        fh.write(content)
    return f"/media/product_images/{relative_name}"


# ---------- trigger endpoint ----------

def test_enqueue_bg_removal_runs_inline_and_persists_result(
    client, db_session, monkeypatch
):
    user = _login(client, db_session)
    image_url = _write_local_image(monkeypatch, "img1.jpg")
    product = _create_product_with_images(db_session, user, [image_url])

    response = client.post(
        f"/api/products/{product.id}/images/remove-background",
        json={"image_url": image_url},
    )
    assert response.status_code == 201, response.data

    body = response.get_json()
    assert body["backend"] == "inmemory"
    job_payload = body["job"]
    assert job_payload["status"] == "succeeded"
    assert job_payload["result_image_url"].startswith("/media/processed_images/")
    assert job_payload["source_image_url"] == image_url

    stored = (
        db_session.query(ImageProcessingJob)
        .filter_by(product_id=product.id)
        .all()
    )
    assert len(stored) == 1
    assert stored[0].status == "succeeded"
    assert stored[0].provider == "rembg"


def test_enqueue_bg_removal_rejects_image_not_on_product(
    client, db_session, monkeypatch
):
    user = _login(client, db_session)
    image_url = _write_local_image(monkeypatch, "img2.jpg")
    product = _create_product_with_images(db_session, user, [image_url])

    response = client.post(
        f"/api/products/{product.id}/images/remove-background",
        json={"image_url": "/media/product_images/not-on-product.jpg"},
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "image_url_not_associated"


def test_enqueue_bg_removal_rejects_other_users_product(client, db_session, monkeypatch):
    # Victim's product is off-limits to the attacker's session.
    victim = User(username="victim")
    victim.set_password("x")
    db_session.add(victim)
    db_session.commit()
    image_url = _write_local_image(monkeypatch, "victim.jpg")
    victim_product = _create_product_with_images(db_session, victim, [image_url])

    attacker = _login(client, db_session, username="attacker")  # noqa: F841

    response = client.post(
        f"/api/products/{victim_product.id}/images/remove-background",
        json={"image_url": image_url},
    )
    assert response.status_code == 404


def test_enqueue_bg_removal_requires_login(client):
    response = client.post(
        "/api/products/1/images/remove-background",
        json={"image_url": "/media/product_images/img.jpg"},
    )
    # Flask-Login typically redirects to /login (302) when unauthenticated.
    assert response.status_code in {302, 401}


# ---------- list endpoint ----------

def test_list_image_jobs_returns_only_current_user(client, db_session, monkeypatch):
    user = _login(client, db_session)
    image_url = _write_local_image(monkeypatch, "list.jpg")
    product = _create_product_with_images(db_session, user, [image_url])

    client.post(
        f"/api/products/{product.id}/images/remove-background",
        json={"image_url": image_url},
    )

    response = client.get(f"/api/products/{product.id}/image-processing-jobs")
    assert response.status_code == 200
    items = response.get_json()["items"]
    assert len(items) == 1
    assert items[0]["source_image_url"] == image_url


# ---------- apply endpoint ----------

def test_apply_updates_snapshot_image_list(client, db_session, monkeypatch):
    user = _login(client, db_session)
    image_url = _write_local_image(monkeypatch, "apply.jpg")
    product = _create_product_with_images(db_session, user, [image_url, "https://cdn.example/2.jpg"])

    trigger = client.post(
        f"/api/products/{product.id}/images/remove-background",
        json={"image_url": image_url},
    )
    job_id = trigger.get_json()["job_id"]

    response = client.post(f"/api/image-processing-jobs/{job_id}/apply")
    assert response.status_code == 200, response.data
    payload = response.get_json()
    assert payload["job"]["status"] == "applied"
    images = payload["images"]
    assert len(images) == 2
    assert images[0].startswith("/media/processed_images/")
    assert images[1] == "https://cdn.example/2.jpg"

    latest_snap = (
        db_session.query(ProductSnapshot)
        .filter_by(product_id=product.id)
        .order_by(ProductSnapshot.scraped_at.desc())
        .first()
    )
    assert latest_snap is not None
    assert latest_snap.image_urls.split("|")[0].startswith("/media/processed_images/")


def test_apply_rejects_jobs_not_succeeded(client, db_session, monkeypatch):
    user = _login(client, db_session)
    image_url = _write_local_image(monkeypatch, "notready.jpg")
    product = _create_product_with_images(db_session, user, [image_url])

    job = ImageProcessingJob(
        job_id="pending-1",
        product_id=product.id,
        user_id=user.id,
        source_image_url=image_url,
        provider="rembg",
        status="queued",
    )
    db_session.add(job)
    db_session.commit()

    response = client.post(f"/api/image-processing-jobs/{job.job_id}/apply")
    assert response.status_code == 409
    assert response.get_json()["error"] == "job_not_ready"


def test_apply_rejects_cross_user_job(client, db_session, monkeypatch):
    victim = User(username="victim-apply")
    victim.set_password("x")
    db_session.add(victim)
    db_session.commit()
    image_url = _write_local_image(monkeypatch, "victim-apply.jpg")
    victim_product = _create_product_with_images(db_session, victim, [image_url])

    victim_job = ImageProcessingJob(
        job_id="victim-job",
        product_id=victim_product.id,
        user_id=victim.id,
        source_image_url=image_url,
        result_image_url="/media/processed_images/victim.png",
        provider="rembg",
        status="succeeded",
    )
    db_session.add(victim_job)
    db_session.commit()

    _login(client, db_session, username="attacker-apply")

    response = client.post(f"/api/image-processing-jobs/{victim_job.job_id}/apply")
    assert response.status_code == 404


# ---------- reject endpoint ----------

def test_reject_marks_job_rejected(client, db_session, monkeypatch):
    user = _login(client, db_session)
    image_url = _write_local_image(monkeypatch, "reject.jpg")
    product = _create_product_with_images(db_session, user, [image_url])

    trigger = client.post(
        f"/api/products/{product.id}/images/remove-background",
        json={"image_url": image_url},
    )
    job_id = trigger.get_json()["job_id"]

    response = client.post(f"/api/image-processing-jobs/{job_id}/reject")
    assert response.status_code == 200
    assert response.get_json()["job"]["status"] == "rejected"


# ---------- internal HMAC upload endpoint ----------

def test_internal_upload_accepts_signed_payload(client, db_session, monkeypatch):
    user = User(username="hmac-user")
    user.set_password("x")
    db_session.add(user)
    db_session.commit()
    product = _create_product_with_images(
        db_session, user, ["/media/product_images/hmac.jpg"]
    )

    # Seed a queued job manually (worker flow).
    job = ImageProcessingJob(
        job_id="hmac-job-1",
        product_id=product.id,
        user_id=user.id,
        source_image_url="/media/product_images/hmac.jpg",
        provider="rembg",
        status="running",
    )
    db_session.add(job)
    db_session.commit()

    body = b"\x89PNG\r\n\x1a\nfakebytes"
    ts = str(int(time.time()))
    sig = compute_signature(job_id=job.job_id, timestamp=ts, body=body)

    response = client.post(
        f"/internal/bg-removal/{job.job_id}/upload",
        data=body,
        content_type="application/octet-stream",
        headers={
            SIGNATURE_HEADER: sig,
            TIMESTAMP_HEADER: ts,
            JOB_ID_HEADER: job.job_id,
        },
    )
    assert response.status_code == 200, response.data
    payload = response.get_json()
    assert payload["result_image_url"].startswith("/media/processed_images/")
    assert payload["job"]["status"] == "succeeded"


def test_internal_upload_rejects_bad_signature(client, db_session, monkeypatch):
    user = User(username="hmac-bad")
    user.set_password("x")
    db_session.add(user)
    db_session.commit()
    product = _create_product_with_images(
        db_session, user, ["/media/product_images/hmac2.jpg"]
    )
    job = ImageProcessingJob(
        job_id="hmac-job-bad",
        product_id=product.id,
        user_id=user.id,
        source_image_url="/media/product_images/hmac2.jpg",
        provider="rembg",
        status="running",
    )
    db_session.add(job)
    db_session.commit()

    ts = str(int(time.time()))
    response = client.post(
        f"/internal/bg-removal/{job.job_id}/upload",
        data=b"anything",
        content_type="application/octet-stream",
        headers={
            SIGNATURE_HEADER: "deadbeef",
            TIMESTAMP_HEADER: ts,
            JOB_ID_HEADER: job.job_id,
        },
    )
    assert response.status_code == 401


def test_internal_upload_is_csrf_exempt(app, client, db_session):
    """Worker->web upload must work even with CSRFProtect fully enabled,
    because workers authenticate via HMAC instead of browser cookies."""
    user = User(username="hmac-csrf")
    user.set_password("x")
    db_session.add(user)
    db_session.commit()
    product = _create_product_with_images(
        db_session, user, ["/media/product_images/hmac_csrf.jpg"]
    )
    job = ImageProcessingJob(
        job_id="hmac-job-csrf",
        product_id=product.id,
        user_id=user.id,
        source_image_url="/media/product_images/hmac_csrf.jpg",
        provider="rembg",
        status="running",
    )
    db_session.add(job)
    db_session.commit()

    body = b"\x89PNG\r\n\x1a\nfakebytes"
    ts = str(int(time.time()))
    sig = compute_signature(job_id=job.job_id, timestamp=ts, body=body)

    # Flip CSRF on to mimic production; the internal upload endpoint
    # should still succeed because app.py exempts it from CSRF.
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        response = client.post(
            f"/internal/bg-removal/{job.job_id}/upload",
            data=body,
            content_type="application/octet-stream",
            headers={
                SIGNATURE_HEADER: sig,
                TIMESTAMP_HEADER: ts,
                JOB_ID_HEADER: job.job_id,
            },
        )
    finally:
        app.config["WTF_CSRF_ENABLED"] = False

    assert response.status_code == 200, response.data


def test_internal_upload_rejects_stale_timestamp(client, db_session, monkeypatch):
    user = User(username="hmac-stale")
    user.set_password("x")
    db_session.add(user)
    db_session.commit()
    product = _create_product_with_images(
        db_session, user, ["/media/product_images/hmac3.jpg"]
    )
    job = ImageProcessingJob(
        job_id="hmac-job-stale",
        product_id=product.id,
        user_id=user.id,
        source_image_url="/media/product_images/hmac3.jpg",
        provider="rembg",
        status="running",
    )
    db_session.add(job)
    db_session.commit()

    body = b"some"
    stale_ts = str(int(time.time()) - 10_000)
    sig = compute_signature(job_id=job.job_id, timestamp=stale_ts, body=body)

    response = client.post(
        f"/internal/bg-removal/{job.job_id}/upload",
        data=body,
        content_type="application/octet-stream",
        headers={
            SIGNATURE_HEADER: sig,
            TIMESTAMP_HEADER: stale_ts,
            JOB_ID_HEADER: job.job_id,
        },
    )
    assert response.status_code == 401
