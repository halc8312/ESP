"""
CLI commands for the application.
"""
import time
import traceback
from datetime import datetime

import click

import offmall_db
import rakuma_db
import snkrdunk_db
import surugaya_db
import yahoo_db
import yahuoku_db
from database import SessionLocal
from mercari_db import scrape_single_item as scrape_mercari_single_item
from models import Product, ProductSnapshot, Variant
from services.pricing_service import update_product_selling_price
from services.scrape_result_policy import (
    build_policy_reason,
    evaluate_persistence,
    normalize_item_for_persistence,
)


def _get_single_item_scrapers() -> dict:
    return {
        "mercari": scrape_mercari_single_item,
        "yahoo": yahoo_db.scrape_single_item,
        "rakuma": rakuma_db.scrape_single_item,
        "surugaya": surugaya_db.scrape_single_item,
        "offmall": offmall_db.scrape_single_item,
        "yahuoku": yahuoku_db.scrape_single_item,
        "snkrdunk": snkrdunk_db.scrape_single_item,
    }


def register_cli_commands(app):
    """Register CLI commands with the Flask app."""

    @app.cli.command("update-products")
    @click.option("--site", "site_filter", type=click.Choice(sorted(_get_single_item_scrapers().keys())), default=None)
    @click.option("--user-id", type=int, default=None)
    @click.option("--limit", type=int, default=None)
    @click.option("--dry-run", is_flag=True, default=False)
    def update_products(site_filter, user_id, limit, dry_run):
        """Re-check stored product price/status with site-aware scrapers."""
        session_db = SessionLocal()
        scraper_map = _get_single_item_scrapers()
        repricing_product_ids = set()

        try:
            query = session_db.query(Product).filter(
                Product.deleted_at == None,
                Product.archived != True,
            )
            if site_filter:
                query = query.filter(Product.site == site_filter)
            if user_id is not None:
                query = query.filter(Product.user_id == user_id)
            query = query.order_by(Product.updated_at.asc())
            if limit is not None and limit > 0:
                query = query.limit(limit)

            products = query.all()
            total = len(products)
            print(f"Start updating {total} products... dry_run={dry_run}")

            updated_count = 0
            skipped_uncertain = 0

            for index, product in enumerate(products, 1):
                scraper_fn = scraper_map.get(product.site)
                if scraper_fn is None:
                    print(f"[{index}/{total}] Unsupported site: {product.site}")
                    continue

                url = product.source_url
                print(f"[{index}/{total}] User:{product.user_id} Site:{product.site} | Checking: {url}")

                try:
                    items = scraper_fn(url, headless=True)
                    if not items:
                        print("  -> Failed to scrape.")
                        continue

                    raw_item = items[0]
                    item = normalize_item_for_persistence(raw_item)
                    meta = raw_item.get("_scrape_meta") or {}
                    action = evaluate_persistence(product.site, item, meta, product)

                    if action == "reject":
                        skipped_uncertain += 1
                        print(
                            "  -> SKIP uncertain"
                            f" site={product.site}"
                            f" url={url}"
                            f" reason={build_policy_reason(item, meta)}"
                            f" old_price={product.last_price}"
                            f" new_price_candidate={item.get('price')}"
                            f" status_candidate={item.get('status')}"
                        )
                        continue

                    status = item.get("status") or product.last_status or "unknown"
                    if action == "allow_status_only":
                        existing_variants = session_db.query(Variant).filter_by(product_id=product.id).all()
                        status_changed = status != product.last_status
                        inventory_changed = False
                        if status in {"sold", "deleted"}:
                            inventory_changed = any((variant.inventory_qty or 0) != 0 for variant in existing_variants)

                        if not status_changed and not inventory_changed:
                            print("  -> No change.")
                            continue

                        if dry_run:
                            print(f"  -> WOULD UPDATE status-only: {product.last_status}->{status}")
                        else:
                            product.last_status = status
                            product.updated_at = datetime.utcnow()
                            if status in {"sold", "deleted"}:
                                for variant in existing_variants:
                                    variant.inventory_qty = 0
                            updated_count += 1
                        continue

                    new_price = item.get("price")
                    new_status = status
                    new_title = item.get("title") or product.last_title or ""
                    price_changed = new_price is not None and product.last_price != new_price
                    status_changed = new_status != product.last_status
                    title_changed = new_title and product.last_title != new_title

                    existing_variants = session_db.query(Variant).filter_by(product_id=product.id).all()
                    inventory_changed = False
                    if new_status in {"sold", "deleted"}:
                        inventory_changed = any((variant.inventory_qty or 0) != 0 for variant in existing_variants)
                    elif any(variant.option1_value == "Default Title" and (variant.inventory_qty or 0) == 0 for variant in existing_variants):
                        inventory_changed = True

                    if not any((price_changed, status_changed, title_changed, inventory_changed)):
                        print("  -> No change.")
                        continue

                    if dry_run:
                        print(
                            "  -> WOULD UPDATE"
                            f" title={product.last_title}->{new_title}"
                            f" price={product.last_price}->{new_price}"
                            f" status={product.last_status}->{new_status}"
                        )
                    else:
                        if new_title:
                            product.last_title = new_title
                        if new_price is not None:
                            product.last_price = new_price
                        product.last_status = new_status
                        product.updated_at = datetime.utcnow()

                        for variant in existing_variants:
                            if new_price is not None and (variant.option1_value == "Default Title" or len(existing_variants) == 1):
                                variant.price = new_price
                            if new_status in {"sold", "deleted"}:
                                variant.inventory_qty = 0
                            elif variant.option1_value == "Default Title":
                                variant.inventory_qty = variant.inventory_qty or 1

                        snapshot = ProductSnapshot(
                            product_id=product.id,
                            scraped_at=datetime.utcnow(),
                            title=new_title,
                            price=new_price,
                            status=new_status,
                            description=item.get("description") or "",
                            image_urls="|".join(item.get("image_urls") or []),
                        )
                        session_db.add(snapshot)
                        updated_count += 1

                        if price_changed and product.pricing_rule_id:
                            repricing_product_ids.add(product.id)

                    time.sleep(2)

                except Exception as exc:
                    print(f"  -> Error: {exc}")
                    traceback.print_exc()

            if dry_run:
                session_db.rollback()
                print(
                    f"Dry-run finished. Would update: {updated_count}, "
                    f"skipped_uncertain: {skipped_uncertain}"
                )
                return

            session_db.commit()
            for product_id in repricing_product_ids:
                update_product_selling_price(product_id)
            print(f"Finished. Total updated: {updated_count}, skipped_uncertain: {skipped_uncertain}")

        finally:
            session_db.close()
