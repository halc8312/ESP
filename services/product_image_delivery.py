"""Bounded, authenticated worker -> web delivery of staged product images.

The web service owns the media disk. A job-specific upload slot is immutable;
retries may repeat the same bytes, but cannot replace an earlier image. The
database ledger is checked by the web route before any file is accepted.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import tempfile
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image, ImageOps

from services.image_service import IMAGE_STORAGE_PATH, validate_image_bytes

SIGNATURE_HEADER = "X-ESP-Image-Signature"
TIMESTAMP_HEADER = "X-ESP-Image-Timestamp"
MAX_DELIVERY_BYTES = 5 * 1024 * 1024
MAX_DELIVERY_PIXELS = 20_000_000
DELIVERY_SUBDIR = "product-delivery"
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_KINDS = {"detail", "thumbnail"}


class ImageDeliveryError(RuntimeError):
    """Delivery did not produce a trusted managed image URL."""


class ImageSlotConflict(ImageDeliveryError):
    """An immutable upload slot already contains different bytes."""


def _valid_slot(product_id, token, index, kind):
    return (
        isinstance(product_id, int) and not isinstance(product_id, bool) and product_id > 0
        and isinstance(index, int) and not isinstance(index, bool)
        and kind in _KINDS and 0 <= index < (8 if kind == "detail" else 1)
        and isinstance(token, str) and _TOKEN_PATTERN.fullmatch(token) is not None
    )


def _shared_secret():
    # No development HMAC fallback: absence of the shared deployment secret
    # leaves this channel closed rather than accepting a known public key.
    secret = (os.environ.get("SECRET_KEY") or "").strip()
    if not secret:
        raise ImageDeliveryError("Shared image delivery secret is unavailable.")
    return secret.encode("utf-8")


def compute_delivery_signature(*, product_id, token, index, kind, timestamp, body):
    if not _valid_slot(product_id, token, index, kind):
        raise ImageDeliveryError("Invalid image delivery slot.")
    digest = hashlib.sha256(body).hexdigest()
    payload = f"esp-product-image-v1\n{kind}\n{product_id}\n{token}\n{index}\n{timestamp}\n{digest}".encode("utf-8")
    return hmac.new(_shared_secret(), payload, hashlib.sha256).hexdigest()


def verify_delivery_signature(*, product_id, token, index, kind, timestamp, body, signature):
    if not timestamp or not signature or not _valid_slot(product_id, token, index, kind):
        return False
    try:
        if abs(int(time.time()) - int(timestamp)) > 300:
            return False
        expected = compute_delivery_signature(
            product_id=product_id, token=token, index=index, kind=kind,
            timestamp=timestamp, body=body,
        )
        return hmac.compare_digest(expected, signature)
    except (TypeError, ValueError, ImageDeliveryError):
        return False


def validate_delivery_bytes(content, content_type=None):
    if not content or len(content) > MAX_DELIVERY_BYTES:
        raise ImageDeliveryError("Invalid image size.")
    try:
        ext, (width, height) = validate_image_bytes(content, content_type=content_type)
    except Image.DecompressionBombError as exc:
        raise ImageDeliveryError("Invalid image dimensions.") from exc
    if width * height > MAX_DELIVERY_PIXELS:
        raise ImageDeliveryError("Invalid image dimensions.")
    return ext


def canonical_delivery_bytes(content, ext):
    """Keep the first frame, apply orientation, and discard EXIF/other metadata."""
    try:
        with Image.open(BytesIO(content)) as original:
            image = ImageOps.exif_transpose(original)
            image.load()
            transparency = image.info.get("transparency")
            if ext == ".png" and transparency is not None:
                # Palette/color-key transparency is visual content, not EXIF.
                image = image.convert("RGBA")
            image.info.clear()
            image.getexif().clear()
            output = BytesIO()
            save_options = {}
            if ext == ".gif" and isinstance(transparency, int) and 0 <= transparency < 256:
                save_options["transparency"] = transparency
            image.save(output, format={".jpg": "JPEG", ".png": "PNG", ".webp": "WEBP", ".gif": "GIF"}[ext], **save_options)
        canonical = output.getvalue()
        validate_delivery_bytes(canonical)
        return canonical
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageDeliveryError("Image canonicalization failed.") from exc


def persist_delivery_bytes(product_id, token, index, content, ext, *, kind="detail"):
    """Return (managed URL, newly created path or None), never overwrite a slot.

    Caller holds the corresponding database write fence. Atomic linking also
    prevents an incomplete file becoming visible if the web process dies.
    """
    if not _valid_slot(product_id, token, index, kind) or ext not in {".jpg", ".png", ".webp", ".gif"}:
        raise ImageDeliveryError("Invalid image delivery slot.")
    directory = Path(IMAGE_STORAGE_PATH) / DELIVERY_SUBDIR / kind / str(product_id) / token
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{index}{ext}"
    existing = [candidate for candidate in directory.glob(f"{index}.*") if candidate.is_file()]
    if existing:
        if len(existing) != 1 or existing[0] != path or path.stat().st_size != len(content) or path.read_bytes() != content:
            raise ImageSlotConflict("Image upload slot is already occupied.")
        return f"/media/{DELIVERY_SUBDIR}/{kind}/{product_id}/{token}/{index}{ext}", None
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".upload-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.stat().st_size != len(content) or path.read_bytes() != content:
                raise ImageSlotConflict("Image upload slot is already occupied.")
            return f"/media/{DELIVERY_SUBDIR}/{kind}/{product_id}/{token}/{index}{ext}", None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return f"/media/{DELIVERY_SUBDIR}/{kind}/{product_id}/{token}/{index}{ext}", path


def deliver_image_bytes(product_id, token, index, content, *, kind="detail"):
    """Upload validated bytes over the existing private web-service channel.

    The response must identify this exact managed slot. Redirects are rejected
    so authentication headers and image bytes cannot leave the configured web
    origin. A retry uses the same immutable slot and body-bound signature.
    """
    ext = validate_delivery_bytes(content)
    if not _valid_slot(product_id, token, index, kind):
        raise ImageDeliveryError("Invalid image delivery slot.")
    from jobs.bg_removal_tasks import _headers_for_internal_web_request, _resolve_web_base_url

    base = _resolve_web_base_url().rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ImageDeliveryError("Invalid internal image delivery host.")
    target = f"{base}/internal/product-images/{kind}/{product_id}/{token}/{index}"
    timestamp = str(int(time.time()))
    headers = _headers_for_internal_web_request(target)
    headers.update({
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: compute_delivery_signature(
            product_id=product_id, token=token, index=index, kind=kind,
            timestamp=timestamp, body=content,
        ),
        "Content-Type": {".jpg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}[ext],
    })
    try:
        response = requests.post(target, data=content, headers=headers, timeout=(5, 30), allow_redirects=False)
        if response.status_code != 200:
            raise ImageDeliveryError("Internal image upload was not accepted.")
        expected = f"/media/{DELIVERY_SUBDIR}/{kind}/{product_id}/{token}/{index}{ext}"
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("image_url") != expected:
            raise ImageDeliveryError("Internal image upload returned an invalid URL.")
        return expected
    except (requests.RequestException, ValueError) as exc:
        raise ImageDeliveryError("Internal image upload failed.") from exc
    finally:
        if "response" in locals():
            response.close()
