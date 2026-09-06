"""Validate and atomically retain public catalog inquiries, without reserving stock."""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from models import CatalogRequest, CatalogRequestItem, PriceListItem, Product
from routes.catalog import (
    _attach_latest_snapshots,
    _build_catalog_item,
    _normalize_instagram_username,
    _pricelist_by_token,
)
from services.rate_limit_service import get_rate_limiter

MAX_ITEMS = 50
REQUEST_LIMIT = 5
REQUEST_WINDOW_SECONDS = 15 * 60


class CatalogRequestError(Exception):
    def __init__(self, message, *, code="validation_error", status=400, items=None):
        super().__init__(message)
        self.status = status
        self.payload = {"error": message, "code": code}
        if items is not None:
            self.payload["items"] = items


def _optional_text(payload, field, limit):
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise CatalogRequestError(f"{field} must be text of at most {limit} characters.")
    return value.strip() or None


def validate_submission(payload):
    if not isinstance(payload, dict):
        raise CatalogRequestError("Please send a valid request.")
    key = payload.get("submission_key")
    if not isinstance(key, str) or not re.fullmatch(
        r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})", key
    ):
        raise CatalogRequestError("Please refresh the page before sending your request.")
    raw_instagram = payload.get("buyer_instagram")
    if not isinstance(raw_instagram, str) or len(raw_instagram) > 200:
        raise CatalogRequestError("Please enter a valid Instagram username.")
    instagram = _normalize_instagram_username(raw_instagram).lower()
    if not instagram or instagram.startswith(".") or instagram.endswith(".") or ".." in instagram:
        raise CatalogRequestError("Please enter a valid Instagram username.")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= MAX_ITEMS:
        raise CatalogRequestError(f"Please select between 1 and {MAX_ITEMS} products.")
    items, seen = [], set()
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise CatalogRequestError("Please check the selected products.")
        product_id = raw_item.get("product_id")
        quantity = raw_item.get("quantity")
        price = raw_item.get("expected_price_jpy")
        if type(product_id) is not int or not 1 <= product_id <= 2_147_483_647:
            raise CatalogRequestError("Please check the selected products.")
        if product_id in seen:
            raise CatalogRequestError("Each product must appear only once.")
        if type(quantity) is not int or not 1 <= quantity <= 99:
            raise CatalogRequestError("Quantity must be between 1 and 99.")
        if "expected_price_jpy" not in raw_item or (
            price is not None and (type(price) is not int or not 0 <= price <= 2_147_483_647)
        ):
            raise CatalogRequestError("Please refresh the selected product prices.")
        seen.add(product_id)
        items.append({"product_id": product_id, "quantity": quantity, "expected_price_jpy": price})
    return {
        "submission_key": uuid.UUID(key).hex,
        "buyer_instagram": instagram,
        "buyer_name": _optional_text(payload, "buyer_name", 100),
        "message": _optional_text(payload, "message", 2000),
        "items": items,
    }


def _payload_hash(pricelist_id, payload):
    # A key belongs to one owner's request, but cannot be replayed through a
    # different list. Equivalent item ordering/Instagram spelling is harmless.
    canonical = {
        **payload, "pricelist_id": pricelist_id,
        "items": sorted(payload["items"], key=lambda item: item["product_id"]),
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _retry_response(existing, payload_hash):
    if existing.payload_hash != payload_hash:
        raise CatalogRequestError(
            "This submission was already used. Please review your request and send it again.",
            code="duplicate_submission", status=409,
        )
    return {"ok": True, "reference": existing.reference}, 200


def _consume_attempt(token, client_ip):
    try:
        # increment is atomic in both shared Redis/Valkey and local memory.
        # Checking then incrementing would admit simultaneous excess requests.
        count = get_rate_limiter().increment(
            "catalog_request", f"{token}:{client_ip}", REQUEST_WINDOW_SECONDS,
        )
    except Exception:
        raise CatalogRequestError(
            "Requests are temporarily unavailable. Please try again shortly.",
            code="service_unavailable", status=503,
        ) from None
    if count > REQUEST_LIMIT:
        raise CatalogRequestError(
            "Too many requests. Please try again in 15 minutes.",
            code="rate_limited", status=429,
        )


def submit_catalog_request(session_db, token, raw_payload, client_ip):
    pricelist = _pricelist_by_token(session_db, token)
    if pricelist is None:
        raise CatalogRequestError("This catalog is unavailable.", code="catalog_unavailable", status=404)
    payload = validate_submission(raw_payload)
    payload_hash = _payload_hash(pricelist.id, payload)
    owner_id = pricelist.user_id
    submission_key = payload["submission_key"]
    existing = session_db.query(CatalogRequest).filter_by(
        user_id=owner_id, submission_key=submission_key,
    ).first()
    if existing is not None:
        return _retry_response(existing, payload_hash)

    _consume_attempt(token, client_ip)
    requested_ids = [item["product_id"] for item in payload["items"]]
    rows = (
        session_db.query(PriceListItem)
        .join(Product)
        .filter(
            PriceListItem.price_list_id == pricelist.id,
            PriceListItem.visible.is_(True),
            Product.user_id == owner_id,
            Product.id.in_(requested_ids),
        )
        .options(joinedload(PriceListItem.product).subqueryload(Product.variants))
        .options(joinedload(PriceListItem.product).joinedload(Product.shop))
        .all()
    )
    _attach_latest_snapshots(session_db, [row.product for row in rows])
    public_items = {}
    for row in rows:
        dto = _build_catalog_item(row)
        if dto is not None:
            public_items[dto["product_id"]] = dto
    refreshed = [public_items[product_id] for product_id in requested_ids if product_id in public_items]
    if any(
        item["product_id"] not in public_items
        or not public_items[item["product_id"]]["in_stock"]
        or item["quantity"] > public_items[item["product_id"]]["stock"]
        for item in payload["items"]
    ):
        raise CatalogRequestError(
            "Some products or quantities are no longer available. Please review your selection.",
            code="items_unavailable", status=409, items=refreshed,
        )
    if any(
        item["expected_price_jpy"] != public_items[item["product_id"]]["price"]
        for item in payload["items"]
    ):
        raise CatalogRequestError(
            "Some prices have changed. Please review the updated prices before sending.",
            code="catalog_changed", status=409, items=refreshed,
        )

    shop = pricelist.shop
    if shop is None or shop.user_id != owner_id:
        # A legacy list can be unbound. Keep a shop only when its selected
        # products consistently belong to the same shop of this owner.
        shops = {row.product.shop.id: row.product.shop for row in rows if (
            row.product.shop is not None and row.product.shop.user_id == owner_id
        )}
        shop = next(iter(shops.values())) if len(shops) == 1 and all(
            row.product.shop_id in shops for row in rows
        ) else None
    catalog_request = CatalogRequest(
        reference=uuid.uuid4().hex[:20], user_id=owner_id,
        pricelist_id=pricelist.id, pricelist_name=pricelist.name,
        shop_id=shop.id if shop else None, shop_name=shop.name if shop else None,
        buyer_instagram=payload["buyer_instagram"], buyer_name=payload["buyer_name"],
        message=payload["message"], submission_key=submission_key, payload_hash=payload_hash,
        items=[CatalogRequestItem(
            product_id=item["product_id"],
            title_snapshot=public_items[item["product_id"]]["title"],
            price_jpy_snapshot=public_items[item["product_id"]]["price"],
            quantity=item["quantity"],
        ) for item in payload["items"]],
    )
    session_db.add(catalog_request)
    try:
        session_db.commit()
    except IntegrityError:
        # The database uniqueness constraint also protects simultaneous retries.
        session_db.rollback()
        existing = session_db.query(CatalogRequest).filter_by(
            user_id=owner_id, submission_key=submission_key,
        ).first()
        if existing is not None:
            return _retry_response(existing, payload_hash)
        raise
    return {"ok": True, "reference": catalog_request.reference}, 201
