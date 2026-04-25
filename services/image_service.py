"""
Image caching and validation service for external product images.
"""
from __future__ import annotations

import os
from io import BytesIO
from urllib.parse import urlparse

import requests
from PIL import Image, UnidentifiedImageError

# 画像保存設定
IMAGE_STORAGE_PATH = os.environ.get("IMAGE_STORAGE_PATH", os.path.join('static', 'images'))
os.makedirs(IMAGE_STORAGE_PATH, exist_ok=True)

MAX_IMAGE_DOWNLOAD_BYTES = int(os.environ.get("MAX_IMAGE_DOWNLOAD_BYTES", str(5 * 1024 * 1024)))
MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", str(20_000_000)))
ALLOWED_IMAGE_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


class ImageValidationError(ValueError):
    """Raised when an image is missing, too large, or not a supported image."""


def _allowed_hosts():
    raw_hosts = os.environ.get("ALLOWED_IMAGE_HOSTS", "")
    return {host.strip().lower() for host in raw_hosts.split(",") if host.strip()}


def validate_image_url(url: str) -> str:
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ImageValidationError("Image URL must be HTTP(S).")

    allowed_hosts = _allowed_hosts()
    if allowed_hosts and parsed.hostname and parsed.hostname.lower() not in allowed_hosts:
        raise ImageValidationError("Image host is not allowed.")

    return url


def _content_type_ext(content_type: str) -> str:
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized not in ALLOWED_IMAGE_CONTENT_TYPES:
        raise ImageValidationError("Unsupported image content type.")
    return ALLOWED_IMAGE_CONTENT_TYPES[normalized]


def validate_image_bytes(data: bytes, content_type: str | None = None) -> tuple[str, tuple[int, int]]:
    if not data:
        raise ImageValidationError("Image is empty.")
    if len(data) > MAX_IMAGE_DOWNLOAD_BYTES:
        raise ImageValidationError("Image exceeds the configured byte limit.")

    expected_ext = _content_type_ext(content_type) if content_type else None

    try:
        with Image.open(BytesIO(data)) as img:
            img.verify()
            width, height = img.size
            detected_format = (img.format or "").upper()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageValidationError("Downloaded file is not a valid image.") from exc

    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise ImageValidationError("Image pixel count exceeds the configured limit.")

    format_ext = {
        "JPEG": ".jpg",
        "PNG": ".png",
        "WEBP": ".webp",
        "GIF": ".gif",
    }.get(detected_format)
    if not format_ext:
        raise ImageValidationError("Unsupported image format.")
    if expected_ext and expected_ext != format_ext:
        raise ImageValidationError("Image content does not match its Content-Type.")

    return format_ext, (width, height)


def download_external_image(url: str, headers: dict | None = None) -> tuple[bytes, str]:
    validate_image_url(url)
    request_headers = headers or {}
    with requests.get(url, headers=request_headers, stream=True, timeout=(3.05, 10)) as resp:
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        _content_type_ext(content_type)

        content_length = resp.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_IMAGE_DOWNLOAD_BYTES:
            raise ImageValidationError("Image exceeds the configured byte limit.")

        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_IMAGE_DOWNLOAD_BYTES:
                raise ImageValidationError("Image exceeds the configured byte limit.")
            chunks.append(chunk)

    data = b"".join(chunks)
    ext, _size = validate_image_bytes(data, content_type=content_type)
    return data, ext


def cache_mercari_image(mercari_url, product_id, index):
    """
    Download and cache a Mercari image locally.
    Returns the local filename if successful, None otherwise.
    """
    if not mercari_url:
        return None
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': 'https://jp.mercari.com/'
        }
        data, ext = download_external_image(mercari_url, headers=headers)
        filename = f"mercari_{product_id}_{index}{ext}"
        local_path = os.path.join(IMAGE_STORAGE_PATH, filename)
        if os.path.exists(local_path):
            return filename
        with open(local_path, 'wb') as f:
            f.write(data)
        return filename
    except Exception as e:
        print(f"Image download failed: {e}")
    return None
