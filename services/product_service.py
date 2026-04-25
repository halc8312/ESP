"""
Product service for database operations related to scraped items.
"""
import hashlib
import logging
from flask import has_request_context, session

from database import SessionLocal
from models import Product, ProductSnapshot, Variant
from services.pricing_service import update_product_selling_price
from services.scrape_result_policy import (
    evaluate_persistence,
    normalize_item_for_persistence,
    normalize_price_for_persistence,
)
from time_utils import utc_now
from utils import normalize_url


logger = logging.getLogger(__name__)


def _empty_save_summary(input_count: int = 0):
    return {
        "input_count": input_count,
        "processed_count": 0,
        "new_count": 0,
        "updated_count": 0,
        "rejected_count": 0,
    }


def _normalize_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_image_urls(value) -> list[str]:
    if isinstance(value, str):
        candidates = value.replace("\r", "\n").replace("|", "\n").split("\n")
    elif isinstance(value, (list, tuple, set)):
        candidates = value
    else:
        candidates = []

    normalized_urls = []
    seen_urls = set()
    for candidate in candidates:
        url = _normalize_text(candidate)
        if not url:
            continue
        if url in seen_urls:
            continue
        normalized_urls.append(url)
        seen_urls.add(url)
    return normalized_urls


def _normalize_non_negative_int(value, default: int = 0) -> int:
    normalized = normalize_price_for_persistence(value)
    if normalized is None or normalized < 0:
        return default
    return normalized


def _default_inventory_for_status(status: str) -> int:
    return 1 if status == "on_sale" else 0


def _normalize_scraped_variants(raw_variants, *, fallback_price, status: str):
    if not isinstance(raw_variants, (list, tuple)):
        return []

    normalized_variants = []
    for index, variant in enumerate(raw_variants, 1):
        if not isinstance(variant, dict):
            continue

        variant_price = normalize_price_for_persistence(variant.get("price"))
        if variant_price is None:
            variant_price = fallback_price

        inventory_default = _default_inventory_for_status(status)
        inventory_qty = _normalize_non_negative_int(variant.get("inventory_qty"), inventory_default)
        if status in {"sold", "deleted"}:
            inventory_qty = 0

        normalized_variants.append(
            {
                "option1_name": _normalize_text(variant.get("option1_name")),
                "option1_value": _normalize_text(variant.get("option1_value")) or f"Option {index}",
                "option2_name": _normalize_text(variant.get("option2_name")),
                "option2_value": _normalize_text(variant.get("option2_value")) or None,
                "option3_name": _normalize_text(variant.get("option3_name")),
                "option3_value": _normalize_text(variant.get("option3_value")) or None,
                "price": variant_price,
                "inventory_qty": inventory_qty,
            }
        )
    return normalized_variants


def _find_product_for_source(session_db, *, url: str, user_id: int, shop_id):
    base_query = session_db.query(Product).filter_by(source_url=url, user_id=user_id)

    if shop_id is None:
        return base_query.filter(Product.shop_id.is_(None)).order_by(Product.id.asc()).first()

    scoped_product = (
        base_query
        .filter(Product.shop_id == shop_id)
        .order_by(Product.id.asc())
        .first()
    )
    if scoped_product is not None:
        return scoped_product

    return (
        base_query
        .filter(Product.shop_id.is_(None))
        .order_by(Product.id.asc())
        .first()
    )


def save_scraped_items_to_db(
    items,
    user_id: int,
    site: str = "mercari",
    shop_id=None,
    manual_selection: bool = False,
    return_summary: bool = False,
    raise_on_error: bool = False,
):
    """
    mercari_db.scrape_search_result() が返した items(list[dict]) を
    Product / ProductSnapshot に保存する。
    """
    input_count = len(items or [])
    if not items:
        summary = _empty_save_summary(input_count)
        return summary if return_summary else (0, 0)

    session_db = SessionLocal()
    new_count = 0
    updated_count = 0
    processed_count = 0
    rejected_count = 0
    now = utc_now()
    repricing_product_ids = set()

    resolved_shop_id = shop_id
    if resolved_shop_id is None and has_request_context():
        resolved_shop_id = session.get('current_shop_id')

    try:
        for item in items:
            raw_url = item.get("url", "")
            if not raw_url:
                rejected_count += 1
                continue

            url = normalize_url(raw_url)
            normalized_item = normalize_item_for_persistence(item, manual_selection=manual_selection)
            scrape_meta = item.get("_scrape_meta") or {}

            title = _normalize_text(normalized_item.get("title"))
            price = normalized_item.get("price")
            status = normalized_item.get("status") or ""
            description = _normalize_text(normalized_item.get("description"))
            image_urls = _normalize_image_urls(normalized_item.get("image_urls"))
            image_urls_str = "|".join(image_urls)

            product = _find_product_for_source(
                session_db,
                url=url,
                user_id=user_id,
                shop_id=resolved_shop_id,
            )
            persistence_action = evaluate_persistence(
                site,
                normalized_item,
                scrape_meta,
                product,
                manual_selection=manual_selection,
            )
            if persistence_action == "reject":
                rejected_count += 1
                continue

            if product is None:
                if persistence_action != "allow_full":
                    rejected_count += 1
                    continue

                sku_hash = hashlib.md5(url.encode('utf-8')).hexdigest()[:10].upper()
                generated_sku = f"MER-{sku_hash}"

                product = Product(
                    user_id=user_id,
                    site=site,
                    shop_id=resolved_shop_id,
                    source_url=url,
                    last_title=title,
                    last_price=price,
                    last_status=status,
                    created_at=now,
                    updated_at=now,
                )
                session_db.add(product)
                session_db.flush()
                new_count += 1
                processed_count += 1

                scraped_variants = _normalize_scraped_variants(
                    normalized_item.get("variants"),
                    fallback_price=price,
                    status=status,
                )
                if scraped_variants:
                    product.option1_name = (
                        _normalize_text(normalized_item.get("option1_name"))
                        or scraped_variants[0].get("option1_name")
                        or "Variation"
                    )
                    product.option2_name = (
                        _normalize_text(normalized_item.get("option2_name"))
                        or scraped_variants[0].get("option2_name")
                    )
                    product.option3_name = (
                        _normalize_text(normalized_item.get("option3_name"))
                        or scraped_variants[0].get("option3_name")
                    )

                    for i, v_data in enumerate(scraped_variants, 1):
                        new_variant = Variant(
                            product_id=product.id,
                            option1_value=v_data.get("option1_value") or f"Option {i}",
                            option2_value=v_data.get("option2_value"),
                            option3_value=v_data.get("option3_value"),
                            sku=f"{generated_sku}-{i}",
                            price=v_data.get("price", price),
                            taxable=False,
                            inventory_qty=v_data.get("inventory_qty", _default_inventory_for_status(status)),
                            position=i,
                        )
                        session_db.add(new_variant)
                else:
                    default_variant = Variant(
                        product_id=product.id,
                        option1_value="Default Title",
                        sku=generated_sku,
                        price=price,
                        taxable=False,
                        inventory_qty=_default_inventory_for_status(status),
                        position=1,
                    )
                    session_db.add(default_variant)

            else:
                if product.shop_id is None and resolved_shop_id is not None:
                    product.shop_id = resolved_shop_id

                if persistence_action == "allow_status_only":
                    status_changed = bool(status) and product.last_status != status
                    product.last_status = status or product.last_status
                    product.updated_at = now
                    existing_variants = session_db.query(Variant).filter_by(product_id=product.id).all()
                    if status in {"sold", "deleted"}:
                        for existing_variant in existing_variants:
                            existing_variant.inventory_qty = 0
                    if status_changed:
                        updated_count += 1
                    processed_count += 1
                    continue

                title_changed = bool(title.strip()) and product.last_title != title
                price_changed = price is not None and product.last_price != price
                status_changed = bool(status) and product.last_status != status

                if title.strip():
                    product.last_title = title
                if price is not None:
                    product.last_price = price
                if status:
                    product.last_status = status
                product.updated_at = now

                if title_changed or price_changed or status_changed:
                    updated_count += 1
                if price_changed and product.pricing_rule_id:
                    repricing_product_ids.add(product.id)

                existing_variants = session_db.query(Variant).filter_by(product_id=product.id).all()
                for existing_variant in existing_variants:
                    if price is not None and (existing_variant.option1_value == "Default Title" or len(existing_variants) == 1):
                        existing_variant.price = price
                    if status in {"sold", "deleted"}:
                        existing_variant.inventory_qty = 0
                    elif status == "on_sale" and existing_variant.option1_value == "Default Title":
                        existing_variant.inventory_qty = existing_variant.inventory_qty or 1
                processed_count += 1

            snapshot = ProductSnapshot(
                product_id=product.id,
                scraped_at=now,
                title=title,
                price=price,
                status=status,
                description=description,
                image_urls=image_urls_str,
            )
            session_db.add(snapshot)

        session_db.commit()

        for product_id in repricing_product_ids:
            update_product_selling_price(product_id, session=session_db)

        if repricing_product_ids:
            session_db.commit()

        summary = {
            "input_count": input_count,
            "processed_count": processed_count,
            "new_count": new_count,
            "updated_count": updated_count,
            "rejected_count": rejected_count,
        }
        return summary if return_summary else (new_count, updated_count)
    except Exception:
        session_db.rollback()
        logger.exception("DB 保存エラー")
        if raise_on_error:
            raise
        summary = _empty_save_summary(input_count)
        return summary if return_summary else (0, 0)
    finally:
        session_db.close()
