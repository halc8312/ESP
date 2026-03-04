"""
Public catalog routes: token-based access for overseas customers.
No login required.
"""
from flask import Blueprint, render_template, abort
from sqlalchemy.orm import subqueryload

from database import SessionLocal
from models import PriceList, PriceListItem, Product, ProductSnapshot, Variant

catalog_bp = Blueprint('catalog', __name__)


@catalog_bp.route("/catalog/<token>")
def catalog_view(token):
    """公開カタログ表示"""
    session_db = SessionLocal()
    try:
        pl = (
            session_db.query(PriceList)
            .filter(PriceList.token == token, PriceList.is_active == True)
            .first()
        )
        if not pl:
            abort(404)

        # Get visible items with product data
        items = (
            session_db.query(PriceListItem)
            .filter(
                PriceListItem.price_list_id == pl.id,
                PriceListItem.visible == True,
            )
            .join(Product)
            .options(subqueryload(PriceListItem.product).subqueryload(Product.snapshots))
            .options(subqueryload(PriceListItem.product).subqueryload(Product.variants))
            .order_by(PriceListItem.sort_order)
            .all()
        )

        # Process items for display
        catalog_items = []
        for item in items:
            p = item.product
            # Skip archived/deleted products
            if p.archived or p.deleted_at:
                continue

            snapshot = (
                sorted(p.snapshots, key=lambda s: s.scraped_at, reverse=True)[0]
                if p.snapshots else None
            )
            image_urls = []
            if snapshot and snapshot.image_urls:
                image_urls = [url.strip() for url in snapshot.image_urls.split("|") if url.strip()]

            thumb_url = image_urls[0] if image_urls else ""
            display_title = p.custom_title or p.last_title or "(No Title)"
            display_price = item.custom_price or p.selling_price or p.last_price or 0
            total_stock = sum(v.inventory_qty or 0 for v in p.variants)

            catalog_items.append({
                "title": display_title,
                "price": display_price,
                "thumb_url": thumb_url,
                "image_urls": image_urls,
                "stock": total_stock,
                "in_stock": total_stock > 0,
                "source_url": p.source_url,
                "site": p.site,
            })

        return render_template(
            "catalog.html",
            pricelist=pl,
            items=catalog_items,
            currency_rate=pl.currency_rate,
        )
    finally:
        session_db.close()
