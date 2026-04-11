"""
Product service for database operations related to scraped items.
"""
import hashlib
from flask import has_request_context, session

from database import SessionLocal
from models import Product, ProductSnapshot, Variant
from services.pricing_service import update_product_selling_price
from services.scrape_result_policy import (
    evaluate_persistence,
    normalize_item_for_persistence,
)
from time_utils import utc_now
from utils import normalize_url


def save_scraped_items_to_db(items, user_id: int, site: str = "mercari", shop_id=None):
    """
    mercari_db.scrape_search_result() が返した items(list[dict]) を
    Product / ProductSnapshot に保存する。
    """
    if not items:
        return 0, 0

    session_db = SessionLocal()
    new_count = 0
    updated_count = 0
    now = utc_now()
    repricing_product_ids = set()

    resolved_shop_id = shop_id
    if resolved_shop_id is None and has_request_context():
        resolved_shop_id = session.get('current_shop_id')

    try:
        for item in items:
            raw_url = item.get("url", "")
            if not raw_url:
                continue

            url = normalize_url(raw_url)
            normalized_item = normalize_item_for_persistence(item)
            scrape_meta = item.get("_scrape_meta") or {}

            title = normalized_item.get("title") or ""
            price = normalized_item.get("price")
            status = normalized_item.get("status") or ""
            description = normalized_item.get("description") or ""
            image_urls = normalized_item.get("image_urls") or []
            image_urls_str = "|".join(image_urls)

            product = session_db.query(Product).filter_by(source_url=url, user_id=user_id).one_or_none()
            persistence_action = evaluate_persistence(site, normalized_item, scrape_meta, product)
            if persistence_action == "reject":
                continue

            if product is None:
                if persistence_action != "allow_full":
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

                scraped_variants = normalized_item.get("variants")
                if scraped_variants:
                    product.option1_name = normalized_item.get("option1_name", "Variation")
                    product.option2_name = normalized_item.get("option2_name")
                    product.option3_name = normalized_item.get("option3_name")

                    for i, v_data in enumerate(scraped_variants, 1):
                        inventory_qty = v_data.get("inventory_qty", 1)
                        if status in {"sold", "deleted"}:
                            inventory_qty = 0
                        new_variant = Variant(
                            product_id=product.id,
                            option1_value=v_data.get("option1_value", f"Option {i}"),
                            option2_value=v_data.get("option2_value"),
                            option3_value=v_data.get("option3_value"),
                            sku=f"{generated_sku}-{i}",
                            price=v_data.get("price", price),
                            taxable=False,
                            inventory_qty=inventory_qty,
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
                        inventory_qty=0 if status in {"sold", "deleted"} else 1,
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
                    elif existing_variant.option1_value == "Default Title":
                        existing_variant.inventory_qty = existing_variant.inventory_qty or 1

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

        return new_count, updated_count
    except Exception as e:
        session_db.rollback()
        print("DB 保存エラー:", e)
        return 0, 0
    finally:
        session_db.close()
