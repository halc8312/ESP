from datetime import timedelta
from io import BytesIO
from types import SimpleNamespace
import time
import uuid

from PIL import Image
import pytest

from models import Product, ProductSnapshot, Shop, User, Variant
from services import product_image_delivery as delivery
from services.product_detail_jobs import _scope_key
from time_utils import utc_now


def image_bytes(color="red", *, metadata=False):
    image = Image.new("RGB", (12, 8), color)
    stream = BytesIO()
    if metadata:
        exif = Image.Exif()
        exif[270] = "https://private.example/secret-source"
        image.save(stream, format="JPEG", exif=exif)
    else:
        image.save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def channel(client, db_session, monkeypatch, tmp_path):
    monkeypatch.setenv("SECRET_KEY", "a-test-secret-shared-with-the-worker")
    monkeypatch.setattr(delivery, "IMAGE_STORAGE_PATH", str(tmp_path / "web-disk"))
    monkeypatch.setattr("app.IMAGE_STORAGE_PATH", str(tmp_path / "web-disk"))
    owner = User(username="image-owner", password_hash="test")
    other = User(username="image-other", password_hash="test")
    db_session.add_all([owner, other])
    db_session.flush()
    shop = Shop(user_id=owner.id, name="Owner shop")
    foreign_shop = Shop(user_id=other.id, name="Other shop")
    db_session.add_all([shop, foreign_shop])
    db_session.flush()
    token = str(uuid.uuid4())
    product = Product(
        user_id=owner.id, shop_id=shop.id, site="recordcity",
        source_url="https://www.recordcity.jp/ja/catalog/1001",
        last_title="Private source item", last_price=1200, last_status="unknown",
        detail_fetch_state="running", detail_job_id=token,
        detail_source_url="https://www.recordcity.jp/ja/catalog/1001",
        detail_scope_key=_scope_key(owner.id, shop.id),
        detail_lease_expires_at=utc_now() + timedelta(minutes=10),
    )
    product.variants.append(Variant(option1_value="Default Title", inventory_qty=0, price=1200))
    product.snapshots.append(ProductSnapshot(status="unknown", image_urls="https://files.recordcity.jp/private-source.jpg"))
    db_session.add(product)
    db_session.commit()
    return SimpleNamespace(
        client=client, session=db_session, product=product, owner=owner,
        other=other, shop=shop, foreign_shop=foreign_shop, token=token,
        root=tmp_path / "web-disk", body=image_bytes(),
    )


def post(channel, *, body=None, token=None, index=0, kind="detail", product_id=None, headers=None, content_type="image/png"):
    body = channel.body if body is None else body
    token = channel.token if token is None else token
    product_id = channel.product.id if product_id is None else product_id
    timestamp = str(int(time.time()))
    signed = {
        delivery.TIMESTAMP_HEADER: timestamp,
        delivery.SIGNATURE_HEADER: delivery.compute_delivery_signature(
            product_id=product_id, token=token, index=index, kind=kind,
            timestamp=timestamp, body=body,
        ),
    }
    if headers is not None:
        signed.update(headers)
    return channel.client.post(
        f"/internal/product-images/{kind}/{product_id}/{token}/{index}",
        data=body, content_type=content_type, headers=signed,
    )


def test_web_disk_delivery_is_servable_idempotent_and_does_not_complete_details(channel):
    first = post(channel)
    assert first.status_code == 200
    url = first.json["image_url"]
    assert url.startswith("/media/product-delivery/detail/")
    assert "recordcity" not in url and "1001" not in url
    media = channel.client.get(url)
    assert media.status_code == 200
    assert media.data == delivery.canonical_delivery_bytes(channel.body, ".png")
    assert media.mimetype == "image/png"
    before = (channel.root / url.removeprefix("/media/")).stat().st_mtime_ns
    repeated = post(channel)
    assert repeated.status_code == 200 and repeated.json == first.json
    assert (channel.root / url.removeprefix("/media/")).stat().st_mtime_ns == before
    assert len(list(channel.root.rglob("*.png"))) == 1
    channel.session.expire_all()
    assert channel.product.detail_fetch_state == "running"
    assert channel.product.last_status == "unknown"
    assert channel.product.variants[0].inventory_qty == 0
    assert channel.product.snapshots[0].image_urls.startswith("https://")


def test_delivery_discards_source_exif_metadata(channel):
    response = post(channel, body=image_bytes(metadata=True), content_type="image/jpeg")
    assert response.status_code == 200
    media = channel.client.get(response.json["image_url"])
    with Image.open(BytesIO(media.data)) as image:
        assert not image.getexif()
    assert b"private.example" not in media.data


@pytest.mark.parametrize("format,ext", [("PNG", ".png"), ("GIF", ".gif")])
def test_canonical_images_preserve_palette_transparency(format, ext):
    image = Image.new("P", (2, 2), 0)
    image.putpalette([255, 0, 0, 0, 0, 255] + [0] * (768 - 6))
    image.putpixel((1, 1), 1)
    stream = BytesIO()
    image.save(stream, format=format, transparency=0)
    canonical = delivery.canonical_delivery_bytes(stream.getvalue(), ext)
    with Image.open(BytesIO(canonical)) as result:
        assert result.convert("RGBA").getpixel((0, 0)) == (255, 0, 0, 0)
        assert result.convert("RGBA").getpixel((1, 1)) == (0, 0, 255, 255)


def test_upload_slots_are_immutable_across_bytes_and_extensions(channel):
    first = post(channel)
    assert first.status_code == 200
    assert post(channel, body=image_bytes("blue")).status_code == 409
    assert post(channel, body=image_bytes(metadata=True), content_type="image/jpeg").status_code == 409
    assert channel.client.get(first.json["image_url"]).data == delivery.canonical_delivery_bytes(channel.body, ".png")
    assert len(list(channel.root.rglob("*.*"))) == 1


@pytest.mark.parametrize("alteration", ["missing", "body", "kind", "product", "index", "old_timestamp", "bg_signature"])
def test_hmac_is_bound_to_purpose_owner_product_claim_index_timestamp_and_body(channel, alteration):
    timestamp = str(int(time.time()))
    signature = delivery.compute_delivery_signature(
        product_id=channel.product.id, token=channel.token, index=0,
        kind="detail", timestamp=timestamp, body=channel.body,
    )
    body, kind, product_id, index = channel.body, "detail", channel.product.id, 0
    if alteration == "missing":
        signature = ""
    elif alteration == "body":
        body = image_bytes("blue")
    elif alteration == "kind":
        kind = "thumbnail"
    elif alteration == "product":
        product_id += 1
    elif alteration == "index":
        index = 1
    elif alteration == "old_timestamp":
        timestamp = str(int(time.time()) - 301)
        signature = delivery.compute_delivery_signature(
            product_id=product_id, token=channel.token, index=index,
            kind=kind, timestamp=timestamp, body=body,
        )
    elif alteration == "bg_signature":
        from services.bg_remover.internal_auth import compute_signature
        signature = compute_signature(job_id=channel.token, timestamp=timestamp, body=body)
    response = channel.client.post(
        f"/internal/product-images/{kind}/{product_id}/{channel.token}/{index}", data=body,
        content_type="image/png", headers={delivery.TIMESTAMP_HEADER: timestamp, delivery.SIGNATURE_HEADER: signature},
    )
    assert response.status_code == 401
    assert not channel.root.exists()


@pytest.mark.parametrize("drift", ["owner", "shop", "shop_owner", "source", "captured_source", "scope", "lease", "token", "legacy", "queued", "deleted", "site"])
def test_only_the_current_owned_running_source_scope_and_live_lease_accept_images(channel, drift):
    product = channel.product
    if drift == "owner":
        product.user_id = channel.other.id
    elif drift == "shop":
        product.shop_id = channel.foreign_shop.id
        product.detail_scope_key = _scope_key(product.user_id, product.shop_id)
    elif drift == "shop_owner":
        channel.shop.user_id = channel.other.id
    elif drift == "source":
        product.source_url = "https://www.recordcity.jp/ja/catalog/1002"
    elif drift == "captured_source":
        product.detail_source_url = "https://www.recordcity.jp/ja/catalog/1002"
    elif drift == "scope":
        product.detail_scope_key = "wrong:scope"
    elif drift == "lease":
        product.detail_lease_expires_at = utc_now() - timedelta(seconds=1)
    elif drift == "token":
        product.detail_job_id = str(uuid.uuid4())
    elif drift == "legacy":
        product.detail_fetch_state = None
    elif drift == "queued":
        product.detail_fetch_state = "queued"
    elif drift == "deleted":
        product.deleted_at = utc_now()
    elif drift == "site":
        product.site = "mercari"
    channel.session.commit()
    response = post(channel)
    assert response.status_code == 409
    assert response.json == {"error": "stale_request"}
    assert not channel.root.exists()


def test_unbound_owned_product_is_supported(channel):
    channel.product.shop_id = None
    channel.product.detail_scope_key = _scope_key(channel.owner.id, None)
    channel.session.commit()
    assert post(channel).status_code == 200


@pytest.mark.parametrize("content,content_type", [(b"<svg>fake</svg>", "image/png"), (b"", "image/png"), (None, "image/jpeg")])
def test_actual_image_validation_rejects_corrupt_empty_or_mismatched_content(channel, content, content_type):
    assert post(channel, body=channel.body if content is None else content, content_type=content_type).status_code == 400
    assert not channel.root.exists()


def test_fixed_byte_limit_applies_before_parsing_or_persistence(channel):
    assert post(channel, body=b"x" * (delivery.MAX_DELIVERY_BYTES + 1)).status_code == 413
    assert not channel.root.exists()


@pytest.mark.filterwarnings("ignore:Image size.*:PIL.Image.DecompressionBombWarning")
def test_fixed_pixel_limit_rejects_a_valid_oversized_compressed_image(channel):
    stream = BytesIO()
    Image.new("1", (5000, 4001)).save(stream, format="PNG")
    assert post(channel, body=stream.getvalue()).status_code == 400
    assert not channel.root.exists()


def test_shared_secret_absence_fails_closed(channel, monkeypatch):
    timestamp = str(int(time.time()))
    signature = delivery.compute_delivery_signature(
        product_id=channel.product.id, token=channel.token, index=0,
        kind="detail", timestamp=timestamp, body=channel.body,
    )
    monkeypatch.delenv("SECRET_KEY")
    assert channel.client.post(
        f"/internal/product-images/detail/{channel.product.id}/{channel.token}/0",
        data=channel.body, headers={delivery.TIMESTAMP_HEADER: timestamp, delivery.SIGNATURE_HEADER: signature},
    ).status_code == 401


def test_final_expiry_cas_discards_the_new_file(channel, monkeypatch):
    import routes.internal_product_images as route
    real_persist = route.persist_delivery_bytes
    real_now = utc_now()
    advanced = []

    def persist(*args, **kwargs):
        result = real_persist(*args, **kwargs)
        advanced.append(True)
        return result

    monkeypatch.setattr(route, "persist_delivery_bytes", persist)
    monkeypatch.setattr(route, "utc_now", lambda: real_now + timedelta(hours=1) if advanced else real_now)
    assert post(channel).status_code == 409
    assert not list(channel.root.rglob("*.png"))


def test_rejected_cleanup_finishes_before_a_retry_can_accept_the_slot(channel, monkeypatch):
    import routes.internal_product_images as route
    real_create = route.create_isolated_session
    real_persist = route.persist_delivery_bytes
    now = utc_now()
    state = {"expired": False, "first_session": True}
    retry = []
    retry_channel = SimpleNamespace(**channel.__dict__)
    # A separate client models the waiting request without sharing Flask's
    # preserved context stack with the outer request.
    retry_channel.client = channel.client.application.test_client()

    def persist(*args, **kwargs):
        result = real_persist(*args, **kwargs)
        if not retry:
            state["expired"] = True
        return result

    class RetryAfterRollback:
        def __init__(self, session):
            self.session = session

        def __getattr__(self, name):
            return getattr(self.session, name)

        def rollback(self):
            self.session.rollback()
            # Simulate the waiting request proceeding as soon as the database
            # fence releases. The accepted retry must keep a servable file.
            state["expired"] = False
            retry.append(None)
            retry[0] = post(retry_channel)

    def create_session():
        session = real_create()
        if state["first_session"]:
            state["first_session"] = False
            return RetryAfterRollback(session)
        return session

    monkeypatch.setattr(route, "create_isolated_session", create_session)
    monkeypatch.setattr(route, "persist_delivery_bytes", persist)
    monkeypatch.setattr(route, "utc_now", lambda: now + timedelta(hours=1) if state["expired"] else now)
    first = post(channel)
    assert first.status_code == 409
    assert retry[0].status_code == 200
    assert channel.client.get(retry[0].json["image_url"]).status_code == 200


def test_commit_acknowledgement_loss_retains_the_accepted_thumbnail_file(channel, monkeypatch):
    import routes.internal_product_images as route
    from models import ProductThumbnailJob
    snapshot = channel.product.snapshots[0]
    thumbnail = ProductThumbnailJob(
        product_id=channel.product.id, user_id=channel.owner.id, shop_id=channel.shop.id,
        product_source_url=channel.product.source_url, source_snapshot_id=snapshot.id,
        source_image_url=snapshot.image_urls, state="running", job_id="opaque-batch-id",
        claim_token=channel.token, lease_expires_at=utc_now() + timedelta(minutes=10), attempts=1,
    )
    channel.session.add(thumbnail)
    channel.session.commit()
    real_create = route.create_isolated_session

    class LostAcknowledgement:
        def __init__(self, session):
            self.session = session

        def __getattr__(self, name):
            return getattr(self.session, name)

        def commit(self):
            self.session.commit()
            raise OSError("Commit acknowledgement lost")

    monkeypatch.setattr(route, "create_isolated_session", lambda: LostAcknowledgement(real_create()))
    response = post(channel, kind="thumbnail")
    assert response.status_code == 503
    channel.session.expire_all()
    assert thumbnail.state == "complete"
    assert snapshot.image_urls == thumbnail.managed_image_url
    assert channel.client.get(thumbnail.managed_image_url).status_code == 200
    monkeypatch.setattr(route, "create_isolated_session", real_create)
    assert post(channel, kind="thumbnail").status_code == 200


def test_expired_replay_does_not_remove_an_already_accepted_slot(channel, monkeypatch):
    import routes.internal_product_images as route
    first = post(channel)
    url = first.json["image_url"]
    real_persist = route.persist_delivery_bytes
    now = utc_now()
    expired = []

    def persist(*args, **kwargs):
        result = real_persist(*args, **kwargs)
        assert result[1] is None
        expired.append(True)
        return result

    monkeypatch.setattr(route, "persist_delivery_bytes", persist)
    monkeypatch.setattr(route, "utc_now", lambda: now + timedelta(hours=1) if expired else now)
    assert post(channel).status_code == 409
    assert channel.client.get(url).status_code == 200


def test_storage_failure_is_generic_and_does_not_change_product(channel, monkeypatch):
    def denied(*args, **kwargs):
        raise OSError("private filesystem path")
    monkeypatch.setattr("routes.internal_product_images.persist_delivery_bytes", denied)
    response = post(channel)
    assert response.status_code == 503
    assert response.json == {"error": "image_delivery_failed"}
    channel.session.expire_all()
    assert channel.product.detail_fetch_state == "running"


def test_upload_client_reuses_existing_internal_host_and_requires_exact_managed_url(channel, monkeypatch):
    monkeypatch.setenv("WEB_INTERNAL_HOST", "esp-web-existing")
    monkeypatch.setenv("WEB_INTERNAL_PORT", "8080")
    calls = []
    expected = f"/media/product-delivery/detail/{channel.product.id}/{channel.token}/0.png"
    response = SimpleNamespace(status_code=200, json=lambda: {"image_url": expected}, close=lambda: None)
    monkeypatch.setattr(delivery.requests, "post", lambda url, **kwargs: calls.append((url, kwargs)) or response)
    assert delivery.deliver_image_bytes(channel.product.id, channel.token, 0, channel.body) == expected
    url, args = calls[0]
    assert url == f"http://esp-web-existing:8080/internal/product-images/detail/{channel.product.id}/{channel.token}/0"
    assert args["allow_redirects"] is False
    assert args["headers"]["X-Forwarded-Proto"] == "https"
    assert delivery.verify_delivery_signature(
        product_id=channel.product.id, token=channel.token, index=0, kind="detail", body=args["data"],
        timestamp=args["headers"][delivery.TIMESTAMP_HEADER], signature=args["headers"][delivery.SIGNATURE_HEADER],
    )


@pytest.mark.parametrize("status,payload", [(302, {}), (503, {}), (200, {"image_url": "https://private.example/source"}), (200, {"image_url": "/media/other-job.png"})])
def test_upload_client_rejects_redirect_errors_and_foreign_response_urls(channel, monkeypatch, status, payload):
    response = SimpleNamespace(status_code=status, json=lambda: payload, close=lambda: None)
    monkeypatch.setattr(delivery.requests, "post", lambda *args, **kwargs: response)
    with pytest.raises(delivery.ImageDeliveryError):
        delivery.deliver_image_bytes(channel.product.id, channel.token, 0, channel.body)


def test_detail_cache_only_returns_web_delivered_urls_and_keeps_eight_image_bound(channel, monkeypatch):
    from services import product_service
    calls = []
    monkeypatch.setattr(product_service, "_IMAGE_CACHE_ENABLED", True)
    monkeypatch.setattr("services.image_service.download_external_image", lambda url, **kw: (channel.body, ".png"))
    monkeypatch.setattr(delivery, "deliver_image_bytes", lambda pid, token, index, content: calls.append(index) or f"/media/accepted-{index}.png")
    result = product_service.cache_deferred_detail_images(
        {"url": channel.product.source_url, "image_urls": [f"https://files.recordcity.jp/image/{index}.png" for index in range(12)]},
        channel.product.id, channel.token,
    )
    assert calls == list(range(8))
    assert result == [f"/media/accepted-{index}.png" for index in range(8)]


def test_scraped_local_paths_cannot_choose_another_owners_managed_images(channel, monkeypatch):
    from services import product_service
    monkeypatch.setattr(product_service, "_IMAGE_CACHE_ENABLED", True)
    monkeypatch.setattr(delivery, "deliver_image_bytes", lambda *args: pytest.fail("local scraped path must not be delivered"))
    assert product_service.cache_deferred_detail_images(
        {"url": channel.product.source_url, "image_urls": ["/media/product-delivery/detail/999/foreign-token/0.png", "/media/../../private"]},
        channel.product.id, channel.token,
    ) == []


@pytest.mark.parametrize("status", [403, 429])
def test_cdn_rejection_pauses_the_shared_marketplace_before_more_images_are_fetched(channel, monkeypatch, status):
    from services import product_service
    from services.image_service import ImageValidationError
    from services.marketplace_access import marketplace_access
    from services.scrape_safety import ScrapeBlockedError
    calls = []

    def rejected(url, **kwargs):
        calls.append(url)
        failure = ImageValidationError("Image unavailable")
        failure.status_code = status
        failure.response_headers = {"Retry-After": "120"}
        raise failure

    monkeypatch.setattr(product_service, "_IMAGE_CACHE_ENABLED", True)
    monkeypatch.setattr("services.image_service.download_external_image", rejected)
    assert product_service.cache_deferred_detail_images(
        {"url": channel.product.source_url, "image_urls": ["https://files.recordcity.jp/one.png", "https://files.recordcity.jp/two.png"]},
        channel.product.id, channel.token,
    ) == []
    assert len(calls) == 1
    with pytest.raises(ScrapeBlockedError):
        with marketplace_access("recordcity", timeout_seconds=0):
            pytest.fail("site cooldown must prevent another request")


def test_cdn_downloads_consume_the_owning_detail_job_request_budget(channel, monkeypatch):
    from services import product_service
    from services.marketplace_access import request_budget
    calls = []
    def download(url, **kwargs):
        with kwargs["request_admission"](url):
            calls.append(url)
            return channel.body, ".png"

    monkeypatch.setattr(product_service, "_IMAGE_CACHE_ENABLED", True)
    monkeypatch.setattr("services.image_service.download_external_image", download)
    monkeypatch.setattr(delivery, "deliver_image_bytes", lambda pid, token, index, content: f"/media/accepted-{index}.png")
    with request_budget(max_requests=2) as budget:
        result = product_service.cache_deferred_detail_images(
            {"url": channel.product.source_url, "image_urls": [f"https://files.recordcity.jp/{index}.png" for index in range(4)]},
            channel.product.id, channel.token,
        )
    assert budget.requests == 2
    assert len(calls) == 2
    assert result == ["/media/accepted-0.png", "/media/accepted-1.png"]


def physical_image_responses(channel, monkeypatch, statuses):
    """Exercise the real image downloader while replacing only its sockets."""
    from services import image_service, product_service
    opened, closed, starts = [], [], []
    responses = []
    for position, status in enumerate(statuses):
        headers = {"Content-Type": "image/png", "Retry-After": "120"}
        if status == 302:
            headers["Location"] = f"/hop-{position + 1}.png"
        responses.append(SimpleNamespace(status=status, headers=headers, close=lambda position=position: closed.append(position)))

    def open_response(url, headers):
        opened.append(url)
        starts.append(time.monotonic())
        return responses[len(opened) - 1]

    monkeypatch.setattr(product_service, "_IMAGE_CACHE_ENABLED", True)
    monkeypatch.setattr(image_service, "validate_image_url", lambda url: url)
    monkeypatch.setattr(image_service, "_open_pinned_image_response", open_response)
    monkeypatch.setattr(image_service, "_iter_response_chunks", lambda response: iter([channel.body]))
    monkeypatch.setattr(delivery, "deliver_image_bytes", lambda pid, token, index, content: f"/media/accepted-{index}.png")
    return opened, closed, starts


def test_each_selected_image_redirect_hop_consumes_and_paces_one_physical_request(channel, monkeypatch):
    from services import product_service
    from services.marketplace_access import request_budget
    monkeypatch.setenv("RECORDCITY_ACCESS_INTERVAL_SECONDS", "0.05")
    opened, closed, starts = physical_image_responses(channel, monkeypatch, [302, 302, 200])
    with request_budget(max_requests=10) as budget:
        result = product_service.cache_deferred_detail_images(
            {"url": channel.product.source_url, "image_urls": ["https://files.recordcity.jp/first.png"]},
            channel.product.id, channel.token,
        )
    assert result == ["/media/accepted-0.png"]
    assert len(opened) == 3 and closed == [0, 1, 2]
    assert budget.requests == 3
    assert starts[1] - starts[0] >= 0.04
    assert starts[2] - starts[1] >= 0.04


def test_selected_image_redirects_stop_at_the_physical_request_budget(channel, monkeypatch):
    from services import product_service
    from services.marketplace_access import request_budget
    opened, closed, _starts = physical_image_responses(channel, monkeypatch, [302, 302, 200])
    with request_budget(max_requests=2) as budget:
        result = product_service.cache_deferred_detail_images(
            {"url": channel.product.source_url, "image_urls": ["https://files.recordcity.jp/first.png"]},
            channel.product.id, channel.token,
        )
    assert result == []
    assert budget.requests == 2
    assert len(opened) == 2 and closed == [0, 1]


@pytest.mark.parametrize("status", [403, 429])
def test_blocked_selected_image_redirect_is_closed_and_prevents_following_image_requests(channel, monkeypatch, status):
    from services import product_service
    from services.marketplace_access import request_budget
    opened, closed, _starts = physical_image_responses(channel, monkeypatch, [302, status])
    with request_budget(max_requests=10) as budget:
        result = product_service.cache_deferred_detail_images(
            {"url": channel.product.source_url, "image_urls": ["https://files.recordcity.jp/first.png", "https://files.recordcity.jp/second.png"]},
            channel.product.id, channel.token,
        )
    assert result == []
    assert len(opened) == 2 and closed == [0, 1]
    assert budget.requests == 2


def test_image_delivery_failure_leaves_no_external_url_in_completed_snapshot(channel, monkeypatch):
    from services import product_detail_jobs as jobs, product_service
    channel.product.detail_fetch_state = "queued"
    channel.session.commit()
    monkeypatch.setattr(product_service, "_IMAGE_CACHE_ENABLED", True)
    monkeypatch.setattr("services.image_service.download_external_image", lambda url, **kw: (channel.body, ".png"))
    monkeypatch.setattr(delivery, "deliver_image_bytes", lambda *args, **kwargs: (_ for _ in ()).throw(delivery.ImageDeliveryError("failed")))
    monkeypatch.setattr(jobs, "_scrape_detail", lambda site, url: {
        "url": url, "title": "Verified", "price": 1400, "status": "on_sale", "description": "Detail",
        "image_urls": ["https://files.recordcity.jp/image/detail.png"],
    })
    monkeypatch.setattr(jobs, "record_observation_safely", lambda **kw: True)
    result = jobs.run_product_detail_job(channel.product.id, channel.owner.id, channel.product.source_url, channel.token, expected_shop_id=channel.shop.id)
    assert result["status"] == "complete"
    channel.session.expire_all()
    latest = channel.session.query(ProductSnapshot).filter_by(product_id=channel.product.id).order_by(ProductSnapshot.scraped_at.desc(), ProductSnapshot.id.desc()).first()
    assert latest.image_urls == ""
    assert channel.product.variants[0].inventory_qty == 1


def test_selected_detail_worker_snapshot_uses_the_actual_web_disk_response(channel, monkeypatch):
    from services import product_detail_jobs as jobs, product_service
    channel.product.detail_fetch_state = "queued"
    channel.session.commit()
    monkeypatch.setattr(product_service, "_IMAGE_CACHE_ENABLED", True)
    monkeypatch.setattr("services.image_service.download_external_image", lambda url, **kw: (channel.body, ".png"))
    monkeypatch.setattr(jobs, "_scrape_detail", lambda site, url: {
        "url": url, "title": "Verified", "price": 1400, "status": "on_sale", "description": "Detail",
        "image_urls": ["https://files.recordcity.jp/image/detail.png"],
    })
    monkeypatch.setattr(jobs, "record_observation_safely", lambda **kw: True)

    def private_web_post(url, **kwargs):
        from urllib.parse import urlparse
        response = channel.client.post(urlparse(url).path, data=kwargs["data"], headers=kwargs["headers"])
        return SimpleNamespace(status_code=response.status_code, json=lambda: response.json, close=lambda: None)

    monkeypatch.setattr(delivery.requests, "post", private_web_post)
    result = jobs.run_product_detail_job(channel.product.id, channel.owner.id, channel.product.source_url, channel.token, expected_shop_id=channel.shop.id)
    assert result["status"] == "complete"
    channel.session.expire_all()
    latest = channel.session.query(ProductSnapshot).filter_by(product_id=channel.product.id).order_by(ProductSnapshot.scraped_at.desc(), ProductSnapshot.id.desc()).first()
    expected = f"/media/product-delivery/detail/{channel.product.id}/{channel.token}/0.png"
    assert latest.image_urls == expected
    assert channel.client.get(expected).status_code == 200
    assert channel.product.detail_fetch_state == "complete"


def test_only_the_hmac_ingress_is_csrf_exempt(channel):
    channel.client.application.config["WTF_CSRF_ENABLED"] = True
    assert post(channel).status_code == 200
    assert channel.client.post("/catalog/not-a-real-token/products/1/details").status_code == 400


@pytest.mark.parametrize("kind,index,token", [("detail", 8, "safe-token"), ("thumbnail", 1, "safe-token"), ("other", 0, "safe-token"), ("detail", 0, "bad.token")])
def test_only_bounded_managed_slots_are_accepted(channel, kind, index, token):
    response = channel.client.post(
        f"/internal/product-images/{kind}/{channel.product.id}/{token}/{index}",
        data=channel.body, content_type="image/png",
        headers={delivery.TIMESTAMP_HEADER: str(int(time.time())), delivery.SIGNATURE_HEADER: "irrelevant"},
    )
    assert response.status_code == 401
    assert not channel.root.exists()
