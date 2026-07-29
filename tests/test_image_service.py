from io import BytesIO

import pytest
from PIL import Image

from services import image_service
from services.image_service import (
    ImageValidationError,
    download_external_image,
    validate_image_bytes,
    validate_image_url,
)


def _png_bytes(width=2, height=2):
    buffer = BytesIO()
    Image.new("RGB", (width, height), color="red").save(buffer, format="PNG")
    return buffer.getvalue()


def test_validate_image_bytes_accepts_real_image():
    ext, size = validate_image_bytes(_png_bytes(), content_type="image/png")

    assert ext == ".png"
    assert size == (2, 2)


def test_validate_image_bytes_rejects_non_image():
    with pytest.raises(ImageValidationError):
        validate_image_bytes(b"not an image", content_type="image/png")


def test_validate_image_bytes_rejects_pixel_bomb(monkeypatch):
    monkeypatch.setattr(image_service, "MAX_IMAGE_PIXELS", 1)

    with pytest.raises(ImageValidationError, match="pixel"):
        validate_image_bytes(_png_bytes(width=2, height=2), content_type="image/png")


def test_download_external_image_enforces_streamed_size_limit(monkeypatch):
    class FakeResponse:
        status = 200
        headers = {"Content-Type": "image/png"}

        def stream(self, chunk_size):
            del chunk_size
            yield b"x" * 4
            yield b"x" * 4

        def release_conn(self):
            return None

    monkeypatch.setattr(image_service, "MAX_IMAGE_DOWNLOAD_BYTES", 6)
    monkeypatch.setenv("ALLOWED_IMAGE_HOSTS", "img.example.com")
    monkeypatch.setattr(
        image_service,
        "_open_pinned_image_response",
        lambda *_args, **_kwargs: FakeResponse(),
    )

    with pytest.raises(ImageValidationError, match="byte limit"):
        download_external_image("https://img.example.com/test.png")


def test_validate_image_url_requires_https_exact_allowlisted_host(monkeypatch):
    monkeypatch.setenv("ALLOWED_IMAGE_HOSTS", "img.example.com")

    assert validate_image_url("https://img.example.com/a.png") == (
        "https://img.example.com/a.png"
    )
    with pytest.raises(ImageValidationError, match="HTTPS"):
        validate_image_url("http://img.example.com/a.png")
    with pytest.raises(ImageValidationError, match="not allowed"):
        validate_image_url("https://child.img.example.com/a.png")
    with pytest.raises(ImageValidationError, match="IP address"):
        validate_image_url("https://127.0.0.1/a.png")


def test_image_dns_resolution_rejects_any_private_address(monkeypatch):
    monkeypatch.setenv("ALLOWED_IMAGE_HOSTS", "img.example.com")
    monkeypatch.setattr(
        image_service.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (image_service.socket.AF_INET, image_service.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (image_service.socket.AF_INET, image_service.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ],
    )

    with pytest.raises(ImageValidationError, match="non-public"):
        image_service._open_pinned_image_response(
            "https://img.example.com/a.png"
        )


def test_image_connection_pins_validated_ip_but_authenticates_hostname(monkeypatch):
    monkeypatch.setenv("ALLOWED_IMAGE_HOSTS", "img.example.com")
    monkeypatch.setattr(
        image_service.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                image_service.socket.AF_INET,
                image_service.socket.SOCK_STREAM,
                6,
                "",
                ("93.184.216.34", 443),
            )
        ],
    )
    captured = {}

    class Response:
        pass

    class Pool:
        def __init__(self, host, **kwargs):
            captured["connect_host"] = host
            captured["pool_kwargs"] = kwargs

        def urlopen(self, method, target, **kwargs):
            captured["method"] = method
            captured["target"] = target
            captured["request_kwargs"] = kwargs
            return Response()

    monkeypatch.setattr(image_service.urllib3, "HTTPSConnectionPool", Pool)

    image_service._open_pinned_image_response(
        "https://img.example.com/path/a.png?size=large"
    )

    assert captured["connect_host"] == "93.184.216.34"
    assert captured["pool_kwargs"]["server_hostname"] == "img.example.com"
    assert captured["pool_kwargs"]["assert_hostname"] == "img.example.com"
    assert captured["request_kwargs"]["headers"]["Host"] == "img.example.com"
    assert captured["target"] == "/path/a.png?size=large"


def test_image_redirect_target_is_validated_before_second_connection(monkeypatch):
    monkeypatch.setenv("ALLOWED_IMAGE_HOSTS", "img.example.com")
    calls = []

    class RedirectResponse:
        status = 302
        headers = {"Location": "https://169.254.169.254/latest/meta-data/"}

        def release_conn(self):
            return None

    def fake_open(url, _headers):
        calls.append(url)
        return RedirectResponse()

    monkeypatch.setattr(image_service, "_open_pinned_image_response", fake_open)

    with pytest.raises(ImageValidationError, match="IP address"):
        download_external_image("https://img.example.com/test.png")

    assert calls == ["https://img.example.com/test.png"]


def test_image_download_accepts_valid_same_host_redirect(monkeypatch):
    monkeypatch.setenv("ALLOWED_IMAGE_HOSTS", "img.example.com")
    png = _png_bytes()
    calls = []

    class RedirectResponse:
        status = 302
        headers = {"Location": "/final.png"}

        def release_conn(self):
            return None

    class ImageResponse:
        status = 200
        headers = {
            "Content-Type": "image/png",
            "Content-Length": str(len(png)),
        }

        def stream(self, _chunk_size):
            yield png

        def release_conn(self):
            return None

    def fake_open(url, _headers):
        calls.append(url)
        return RedirectResponse() if len(calls) == 1 else ImageResponse()

    monkeypatch.setattr(image_service, "_open_pinned_image_response", fake_open)

    data, extension = download_external_image(
        "https://img.example.com/start.png"
    )

    assert data == png
    assert extension == ".png"
    assert calls == [
        "https://img.example.com/start.png",
        "https://img.example.com/final.png",
    ]


def test_image_cross_host_redirect_drops_untrusted_caller_headers(monkeypatch):
    monkeypatch.setenv(
        "ALLOWED_IMAGE_HOSTS",
        "img.example.com,cdn.example.com",
    )
    png = _png_bytes()
    calls = []

    class RedirectResponse:
        status = 302
        headers = {"Location": "https://cdn.example.com/final.png"}

        def release_conn(self):
            return None

    class ImageResponse:
        status = 200
        headers = {"Content-Type": "image/png"}

        def stream(self, _chunk_size):
            yield png

        def release_conn(self):
            return None

    def fake_open(url, headers):
        calls.append((url, dict(headers)))
        return RedirectResponse() if len(calls) == 1 else ImageResponse()

    monkeypatch.setattr(image_service, "_open_pinned_image_response", fake_open)

    download_external_image(
        "https://img.example.com/start.png",
        headers={
            "User-Agent": "test-agent",
            "Accept": "image/*",
            "Referer": "https://private.example/path?token=secret",
            "Authorization": "Bearer secret",
            "Cookie": "session=secret",
            "X-Api-Key": "secret",
        },
    )

    assert calls[1] == (
        "https://cdn.example.com/final.png",
        {"User-Agent": "test-agent", "Accept": "image/*"},
    )
