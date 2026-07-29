"""
Image caching and validation service for external product images.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
from io import BytesIO
from urllib.parse import urljoin, urlparse, urlunparse

import requests
import urllib3
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger("services.image_service")

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
MAX_IMAGE_REDIRECTS = 5
_CROSS_HOST_REDIRECT_HEADERS = frozenset(
    {"accept", "accept-encoding", "accept-language", "user-agent"}
)

# Product pages may contain arbitrary third-party URLs.  Keep the default
# boundary deliberately narrow; deployments can add exact hosts through
# ALLOWED_IMAGE_HOSTS when a marketplace starts serving images elsewhere.
DEFAULT_ALLOWED_IMAGE_HOSTS = frozenset(
    {
        "static.mercdn.net",
        "item-shopping.c.yimg.jp",
        "auctions.c.yimg.jp",
        "img.fril.jp",
        "cdn.suruga-ya.jp",
        "www.suruga-ya.jp",
        "netmall.hardoff.co.jp",
        "cdn.snkrdunk.com",
    }
)

Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


class ImageValidationError(ValueError):
    """Raised when an image is missing, too large, or not a supported image."""


def split_image_url_string(pipe_separated: str | None) -> list[str]:
    """Return de-duplicated image URLs from a pipe-separated snapshot field."""
    if not pipe_separated:
        return []

    urls: list[str] = []
    seen: set[str] = set()
    for raw_url in pipe_separated.split("|"):
        url = raw_url.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _allowed_hosts():
    raw_hosts = os.environ.get("ALLOWED_IMAGE_HOSTS", "")
    configured = {
        host.strip().rstrip(".").lower()
        for host in raw_hosts.split(",")
        if host.strip()
    }
    return set(DEFAULT_ALLOWED_IMAGE_HOSTS) | configured


def validate_image_url(url: str) -> str:
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError as exc:
        raise ImageValidationError("Image URL is invalid.") from exc
    if parsed.scheme.lower() != "https" or not parsed.netloc or not parsed.hostname:
        raise ImageValidationError("Image URL must use HTTPS.")
    if parsed.username is not None or parsed.password is not None:
        raise ImageValidationError("Image URL must not contain credentials.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ImageValidationError("Image URL port is invalid.") from exc
    if port not in (None, 443):
        raise ImageValidationError("Image URL must use the standard HTTPS port.")
    try:
        host = parsed.hostname.rstrip(".").lower().encode("idna").decode("ascii")
    except (AttributeError, UnicodeError) as exc:
        raise ImageValidationError("Image host is invalid.") from exc
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ImageValidationError("Image URL must not use an IP address.")
    if host not in _allowed_hosts():
        raise ImageValidationError("Image host is not allowed.")

    netloc = host if port is None else f"{host}:443"
    return urlunparse(parsed._replace(scheme="https", netloc=netloc, fragment=""))


def _resolve_public_image_addresses(host: str) -> tuple[str, ...]:
    try:
        answers = socket.getaddrinfo(
            host,
            443,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise ImageValidationError("Image host could not be resolved.") from exc

    addresses = tuple(dict.fromkeys(answer[4][0] for answer in answers if answer[4]))
    if not addresses:
        raise ImageValidationError("Image host could not be resolved.")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ImageValidationError("Image host resolved to an invalid address.") from exc
        if not ip.is_global:
            raise ImageValidationError("Image host resolved to a non-public address.")
    return addresses


def _open_pinned_image_response(url: str, headers: dict | None = None):
    """Open one HTTPS hop against a DNS-pinned public IP.

    TLS still authenticates the original hostname.  Pinning the connection to
    the address checked above closes the DNS-rebinding gap between validation
    and connect.
    """
    parsed = urlparse(url)
    host = str(parsed.hostname or "")
    addresses = _resolve_public_image_addresses(host)
    connect_host = addresses[0]
    pool = urllib3.HTTPSConnectionPool(
        connect_host,
        port=443,
        cert_reqs="CERT_REQUIRED",
        ca_certs=requests.certs.where(),
        assert_hostname=host,
        server_hostname=host,
        maxsize=1,
        block=True,
    )
    request_target = parsed.path or "/"
    if parsed.query:
        request_target += f"?{parsed.query}"
    request_headers = {
        str(key): value
        for key, value in dict(headers or {}).items()
        if str(key).lower() != "host"
    }
    request_headers["Host"] = host
    response = pool.urlopen(
        "GET",
        request_target,
        headers=request_headers,
        redirect=False,
        retries=False,
        preload_content=False,
        timeout=urllib3.Timeout(connect=3.05, read=10),
    )
    # Retain the pool until the streaming response has been released.
    response._image_connection_pool = pool
    return response


def _close_image_response(response) -> None:
    try:
        response.close()
    except Exception:
        pass
    try:
        response.release_conn()
    except Exception:
        pass


def _response_status(response) -> int:
    status = getattr(response, "status", None)
    if not isinstance(status, int):
        status = getattr(response, "status_code", 0)
    try:
        return int(status)
    except (TypeError, ValueError):
        return 0


def _iter_response_chunks(response):
    streamer = getattr(response, "stream", None)
    if callable(streamer):
        yield from streamer(64 * 1024)
        return
    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        yield from iterator(chunk_size=64 * 1024)
        return
    raise ImageValidationError("Image response cannot be streamed.")


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
    current_url = validate_image_url(url)
    request_headers = dict(headers or {})
    response = None
    content_type = ""
    chunks: list[bytes] = []
    total = 0

    for redirect_count in range(MAX_IMAGE_REDIRECTS + 1):
        response = _open_pinned_image_response(current_url, request_headers)
        status = _response_status(response)
        if status in {301, 302, 303, 307, 308}:
            location = str(response.headers.get("Location", "") or "").strip()
            _close_image_response(response)
            response = None
            if not location:
                raise ImageValidationError("Image redirect is missing a destination.")
            if redirect_count >= MAX_IMAGE_REDIRECTS:
                raise ImageValidationError("Image redirect limit exceeded.")
            next_url = validate_image_url(urljoin(current_url, location))
            if urlparse(next_url).hostname != urlparse(current_url).hostname:
                request_headers = {
                    key: value
                    for key, value in request_headers.items()
                    if str(key).lower() in _CROSS_HOST_REDIRECT_HEADERS
                }
            current_url = next_url
            continue
        if status < 200 or status >= 300:
            _close_image_response(response)
            response = None
            raise ImageValidationError(f"Image download failed with HTTP {status}.")
        break
    else:  # pragma: no cover - loop always exits through return/raise
        raise ImageValidationError("Image redirect limit exceeded.")

    try:
        content_type = response.headers.get("Content-Type", "")
        _content_type_ext(content_type)

        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared_length = int(content_length)
            except (TypeError, ValueError) as exc:
                raise ImageValidationError("Image Content-Length is invalid.") from exc
            if declared_length > MAX_IMAGE_DOWNLOAD_BYTES:
                raise ImageValidationError("Image exceeds the configured byte limit.")

        for chunk in _iter_response_chunks(response):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_IMAGE_DOWNLOAD_BYTES:
                raise ImageValidationError("Image exceeds the configured byte limit.")
            chunks.append(bytes(chunk))
    finally:
        _close_image_response(response)

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


PRODUCT_IMAGES_SUBDIR = "product_images"


def cache_product_image(
    image_url: str,
    product_id: int,
    index: int,
) -> str | None:
    """Download an external image and cache it under ``IMAGE_STORAGE_PATH``.

    Returns the ``/media/…`` URL for the cached file on success, or
    ``None`` if the download or validation fails (the caller should keep
    the original external URL as a fallback).
    """
    if not image_url or not image_url.startswith(("http://", "https://")):
        return None

    from services.bg_remover.image_fetch import build_image_fetch_headers

    dest_dir = os.path.join(IMAGE_STORAGE_PATH, PRODUCT_IMAGES_SUBDIR)
    os.makedirs(dest_dir, exist_ok=True)

    filename_base = f"prod_{product_id}_{index}"

    try:
        headers = build_image_fetch_headers(image_url)
        data, ext = download_external_image(image_url, headers=headers)
    except Exception:
        logger.debug("image cache download failed for %s", image_url, exc_info=True)
        return None

    filename = f"{filename_base}{ext}"
    dest_path = os.path.join(dest_dir, filename)

    try:
        with open(dest_path, "wb") as fh:
            fh.write(data)
    except OSError:
        logger.debug("image cache write failed for %s", dest_path, exc_info=True)
        return None

    return f"/media/{PRODUCT_IMAGES_SUBDIR}/{filename}"
