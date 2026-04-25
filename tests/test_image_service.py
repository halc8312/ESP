from io import BytesIO

import pytest
from PIL import Image

from services import image_service
from services.image_service import ImageValidationError, download_external_image, validate_image_bytes


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
        headers = {"Content-Type": "image/png"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            del chunk_size
            yield b"x" * 4
            yield b"x" * 4

    monkeypatch.setattr(image_service, "MAX_IMAGE_DOWNLOAD_BYTES", 6)
    monkeypatch.setattr(image_service.requests, "get", lambda *args, **kwargs: FakeResponse())

    with pytest.raises(ImageValidationError, match="byte limit"):
        download_external_image("https://img.example.com/test.png")
