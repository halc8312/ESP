"""Strict, network-free contract for shallow marketplace listing cards.

A valid card establishes identity and display fields, never verified stock or
complete detail. Callers must preserve that distinction when persisting cards,
reporting search quality, and deciding whether a public request can be added.
"""
from __future__ import annotations

import math
from urllib.parse import urlsplit

from services.search_result_quality import search_item_identity


LISTING_CARD_SITES = frozenset({"recordcity"})
_RECORDCITY_IMAGE_HOSTS = frozenset({"files.recordcity.jp"})
_CARD_STATUSES = frozenset({"unknown", "on_sale", "sold"})
_MAX_PERSISTED_JPY_PRICE = 2_147_483_647  # Product/Variant use SQL Integer.


def _valid_recordcity_image(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme == "https"
            and parsed.hostname in _RECORDCITY_IMAGE_HOSTS
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and parsed.path.startswith("/")
            and not any(ord(char) < 32 for char in value)
        )
    except ValueError:
        return False


def validate_listing_card(item: object, site: str = "recordcity") -> bool:
    """Return whether a card satisfies the explicitly supported shallow shape.

    No price coercion, guessed currency, title-based identity, or inferred
    availability is allowed here. Images are only validated syntactically;
    downloading/caching them must still use the guarded image service.
    """
    if site not in LISTING_CARD_SITES or not isinstance(item, dict):
        return False
    if item.get("_listing_card") is not True or item.get("currency") != "JPY":
        return False
    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        return False
    price = item.get("price")
    if (
        isinstance(price, bool)
        or not isinstance(price, (int, float))
        or (isinstance(price, float) and not math.isfinite(price))
        or price <= 0
        or price > _MAX_PERSISTED_JPY_PRICE
        or price != int(price)
    ):
        return False
    identity = search_item_identity(item, site=site)
    source_id = item.get("source_id")
    if not isinstance(source_id, str) or identity != f"recordcity:{source_id}":
        return False
    if item.get("status") not in _CARD_STATUSES or item.get("description") != "":
        return False
    images = item.get("image_urls")
    return bool(
        isinstance(images, list)
        and len(images) == 1
        and _valid_recordcity_image(images[0])
    )
