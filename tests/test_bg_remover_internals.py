"""Unit tests for the bg_remover service internals (no rembg runtime required)."""
from __future__ import annotations

import time

import pytest

from services.bg_remover.internal_auth import (
    compute_signature,
    verify_signature,
)


def test_compute_signature_is_stable_for_same_inputs():
    sig_a = compute_signature(job_id="abc", timestamp="1700000000", body=b"hello")
    sig_b = compute_signature(job_id="abc", timestamp="1700000000", body=b"hello")
    assert sig_a == sig_b


def test_compute_signature_changes_when_body_changes():
    ts = "1700000000"
    sig_a = compute_signature(job_id="abc", timestamp=ts, body=b"hello")
    sig_b = compute_signature(job_id="abc", timestamp=ts, body=b"world")
    assert sig_a != sig_b


def test_verify_signature_accepts_fresh_signature():
    ts = str(int(time.time()))
    sig = compute_signature(job_id="job-1", timestamp=ts, body=b"payload")
    assert verify_signature(
        job_id="job-1",
        timestamp=ts,
        body=b"payload",
        signature=sig,
    )


def test_verify_signature_rejects_stale_timestamp():
    old_ts = str(int(time.time()) - 10_000)
    sig = compute_signature(job_id="job-1", timestamp=old_ts, body=b"payload")
    assert not verify_signature(
        job_id="job-1",
        timestamp=old_ts,
        body=b"payload",
        signature=sig,
    )


def test_verify_signature_rejects_tampered_body():
    ts = str(int(time.time()))
    sig = compute_signature(job_id="job-1", timestamp=ts, body=b"payload")
    assert not verify_signature(
        job_id="job-1",
        timestamp=ts,
        body=b"tampered",
        signature=sig,
    )


def test_verify_signature_rejects_wrong_job_id():
    ts = str(int(time.time()))
    sig = compute_signature(job_id="job-1", timestamp=ts, body=b"payload")
    assert not verify_signature(
        job_id="job-2",
        timestamp=ts,
        body=b"payload",
        signature=sig,
    )


def test_verify_signature_rejects_missing_fields():
    assert not verify_signature(
        job_id=None, timestamp="1", body=b"", signature="abc"
    )
    assert not verify_signature(
        job_id="j", timestamp=None, body=b"", signature="abc"
    )
    assert not verify_signature(
        job_id="j", timestamp="1", body=b"", signature=None
    )


def test_registry_resolves_rembg_backend_name(monkeypatch):
    from services.bg_remover.registry import resolve_backend_name

    monkeypatch.delenv("BG_REMOVAL_BACKEND", raising=False)
    assert resolve_backend_name() == "rembg"

    monkeypatch.setenv("BG_REMOVAL_BACKEND", "REMBG")
    assert resolve_backend_name() == "rembg"


def test_registry_rejects_unknown_backend(monkeypatch):
    from services.bg_remover import base, registry

    monkeypatch.setenv("BG_REMOVAL_BACKEND", "does-not-exist")
    registry.reset_bg_remover_backend_for_tests()
    with pytest.raises(base.BackgroundRemoverUnavailableError):
        registry.get_bg_remover_backend()
    registry.reset_bg_remover_backend_for_tests()


def test_resolve_web_base_url_default_avoids_render_blocked_port(monkeypatch):
    """Worker->web hop must NOT default to port 10000: Render's private
    network blocks 10000/18012/18013/19099 for internal communication, and
    the previous default caused NameResolutionError in production."""
    from jobs import bg_removal_tasks

    for var in (
        "ESP_WEB_INTERNAL_URL",
        "WEB_INTERNAL_URL",
        "WEB_PUBLIC_URL",
        "WEB_INTERNAL_HOST",
        "WEB_INTERNAL_PORT",
    ):
        monkeypatch.delenv(var, raising=False)

    base = bg_removal_tasks._resolve_web_base_url()
    assert ":10000" not in base, (
        "Default internal URL must not use port 10000 (blocked by Render "
        f"private network). Got: {base}"
    )
    assert base.startswith("http://"), base


def test_resolve_web_base_url_respects_explicit_env(monkeypatch):
    from jobs import bg_removal_tasks

    monkeypatch.setenv("WEB_INTERNAL_URL", "http://esp-web:9090")
    assert bg_removal_tasks._resolve_web_base_url() == "http://esp-web:9090"

    monkeypatch.setenv("ESP_WEB_INTERNAL_URL", "http://alt-web:7000")
    assert bg_removal_tasks._resolve_web_base_url() == "http://alt-web:7000"


def test_resolve_web_base_url_uses_internal_host_env(monkeypatch):
    """When Render injects ``WEB_INTERNAL_HOST`` via ``fromService``, the
    worker must build ``http://<host>:<port>`` using it — not the bare
    service name, which does not resolve on Render's private network."""
    from jobs import bg_removal_tasks

    for var in ("ESP_WEB_INTERNAL_URL", "WEB_INTERNAL_URL", "WEB_PUBLIC_URL"):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setenv("WEB_INTERNAL_HOST", "esp-web-ne5j")
    monkeypatch.setenv("WEB_INTERNAL_PORT", "8080")
    assert (
        bg_removal_tasks._resolve_web_base_url() == "http://esp-web-ne5j:8080"
    )

    # Port defaults to 8080 when only the host is set.
    monkeypatch.delenv("WEB_INTERNAL_PORT", raising=False)
    assert (
        bg_removal_tasks._resolve_web_base_url() == "http://esp-web-ne5j:8080"
    )


def test_resolve_web_base_url_explicit_url_wins_over_host(monkeypatch):
    """An explicit full URL must win over host+port composition so operators
    can override the Render-injected hostname for debugging."""
    from jobs import bg_removal_tasks

    monkeypatch.setenv("WEB_INTERNAL_HOST", "esp-web-ne5j")
    monkeypatch.setenv("WEB_INTERNAL_PORT", "8080")
    monkeypatch.setenv("WEB_INTERNAL_URL", "http://debug-host:7777")
    assert bg_removal_tasks._resolve_web_base_url() == "http://debug-host:7777"


def test_resolve_web_base_url_downgrades_https_internal_port(monkeypatch):
    from jobs import bg_removal_tasks

    monkeypatch.setenv("WEB_INTERNAL_HOST", "esp-1-kend")
    monkeypatch.setenv("WEB_INTERNAL_PORT", "8080")
    monkeypatch.setenv("WEB_INTERNAL_URL", "https://esp-1-kend:8080")

    assert bg_removal_tasks._resolve_web_base_url() == "http://esp-1-kend:8080"


def test_resolve_source_url_downgrades_https_internal_port(monkeypatch):
    from jobs import bg_removal_tasks

    monkeypatch.setenv("WEB_INTERNAL_HOST", "esp-1-kend")
    monkeypatch.setenv("WEB_INTERNAL_PORT", "8080")

    assert (
        bg_removal_tasks._resolve_source_url(
            "https://esp-1-kend:8080/media/product_images/source.jpg"
        )
        == "http://esp-1-kend:8080/media/product_images/source.jpg"
    )


def test_resolve_source_url_keeps_public_https(monkeypatch):
    from jobs import bg_removal_tasks

    monkeypatch.setenv("WEB_INTERNAL_HOST", "esp-1-kend")
    monkeypatch.setenv("WEB_INTERNAL_PORT", "8080")

    source_url = "https://example.com:8080/media/product_images/source.jpg"
    assert bg_removal_tasks._resolve_source_url(source_url) == source_url


def test_fetch_source_bytes_marks_internal_request_secure(monkeypatch):
    from jobs import bg_removal_tasks

    monkeypatch.setenv("WEB_INTERNAL_HOST", "esp-1-kend")
    monkeypatch.setenv("WEB_INTERNAL_PORT", "8080")

    captured = {}

    class Response:
        is_redirect = False
        headers = {}
        content = b"image-bytes"

        def raise_for_status(self):
            return None

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        return Response()

    monkeypatch.setattr(bg_removal_tasks.requests, "get", fake_get)

    assert (
        bg_removal_tasks._fetch_source_bytes(
            "https://esp-1-kend:8080/media/product_images/source.jpg"
        )
        == b"image-bytes"
    )
    assert captured["url"] == "http://esp-1-kend:8080/media/product_images/source.jpg"
    assert captured["headers"]["X-Forwarded-Proto"] == "https"


def test_fetch_source_bytes_rewrites_internal_redirect(monkeypatch):
    from jobs import bg_removal_tasks

    monkeypatch.setenv("WEB_INTERNAL_HOST", "esp-1-kend")
    monkeypatch.setenv("WEB_INTERNAL_PORT", "8080")

    seen_urls = []

    class RedirectResponse:
        is_redirect = True
        headers = {
            "Location": "https://esp-1-kend:8080/media/product_images/final.jpg"
        }
        content = b""

        def raise_for_status(self):
            return None

    class FinalResponse:
        is_redirect = False
        headers = {}
        content = b"final-image"

        def raise_for_status(self):
            return None

    def fake_get(url, **kwargs):
        seen_urls.append(url)
        return RedirectResponse() if len(seen_urls) == 1 else FinalResponse()

    monkeypatch.setattr(bg_removal_tasks.requests, "get", fake_get)

    assert (
        bg_removal_tasks._fetch_source_bytes(
            "/media/product_images/source.jpg"
        )
        == b"final-image"
    )
    assert seen_urls == [
        "http://esp-1-kend:8080/media/product_images/source.jpg",
        "http://esp-1-kend:8080/media/product_images/final.jpg",
    ]


# --- image_fetch header builder tests ---


class TestBuildImageFetchHeaders:
    """Verify that ``build_image_fetch_headers`` supplies browser-like
    headers so marketplace CDNs do not reject source-image fetches."""

    def test_mercari_cdn_gets_referer(self):
        from services.bg_remover.image_fetch import build_image_fetch_headers

        h = build_image_fetch_headers(
            "https://static.mercdn.net/item/detail/orig/photos/m123_1.jpg"
        )
        assert "User-Agent" in h
        assert h["Referer"] == "https://jp.mercari.com/"

    def test_snkrdunk_gets_referer(self):
        from services.bg_remover.image_fetch import build_image_fetch_headers

        h = build_image_fetch_headers("https://cdn.snkrdunk.com/img/abc.jpg")
        assert h["Referer"] == "https://snkrdunk.com/"

    def test_surugaya_gets_referer(self):
        from services.bg_remover.image_fetch import build_image_fetch_headers

        h = build_image_fetch_headers(
            "https://www.suruga-ya.jp/database/photo/123.jpg"
        )
        assert h["Referer"] == "https://www.suruga-ya.jp/"

    def test_unknown_domain_still_gets_user_agent(self):
        from services.bg_remover.image_fetch import build_image_fetch_headers

        h = build_image_fetch_headers("https://example.com/photo.png")
        assert "User-Agent" in h
        assert "Referer" not in h

    def test_local_media_url_gets_user_agent(self):
        from services.bg_remover.image_fetch import build_image_fetch_headers

        h = build_image_fetch_headers("/media/product_images/test.jpg")
        assert "User-Agent" in h
