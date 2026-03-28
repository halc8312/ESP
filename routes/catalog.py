"""
Public catalog routes: token-based access for overseas customers.
No login required.
"""
import hashlib
from collections import Counter
from datetime import timedelta
from urllib.parse import urlparse

from flask import Blueprint, render_template, abort, jsonify, request, session
from flask_login import login_required, current_user
from sqlalchemy.orm import subqueryload

from database import SessionLocal
from models import Shop, PriceList, PriceListItem, Product, CatalogPageView
from time_utils import utc_now

catalog_bp = Blueprint('catalog', __name__)

SEARCH_REFERRERS = ("google.", "bing.", "yahoo.", "duckduckgo.", "baidu.", "ecosia.")
SOCIAL_REFERRERS = ("facebook.", "instagram.", "tiktok.", "twitter.", "x.com", "youtube.", "line.", "pinterest.")


def _latest_snapshot(product):
    if not product.snapshots:
        return None
    return sorted(product.snapshots, key=lambda s: s.scraped_at, reverse=True)[0]


def _pricelist_by_token(session_db, token):
    return (
        session_db.query(PriceList)
        .filter(PriceList.token == token, PriceList.is_active == True)
        .first()
    )


def _build_catalog_item(item):
    p = item.product
    if p.archived or p.deleted_at:
        return None

    snapshot = _latest_snapshot(p)
    image_urls = []
    if snapshot and snapshot.image_urls:
        image_urls = [url.strip() for url in snapshot.image_urls.split("|") if url.strip()]

    display_title = p.custom_title or p.last_title or "(No Title)"
    display_price = item.custom_price or p.selling_price or p.last_price or 0
    total_stock = sum(v.inventory_qty or 0 for v in p.variants)

    return {
        "product_id": p.id,
        "title": display_title,
        "title_en": p.custom_title_en or "",
        "price": display_price,
        "thumb_url": image_urls[0] if image_urls else "",
        "image_urls": image_urls,
        "stock": total_stock,
        "in_stock": total_stock > 0,
        "source_url": p.source_url,
        "site": p.site,
        "description": p.custom_description or (snapshot.description if snapshot else "") or "",
        "description_en": p.custom_description_en or "",
    }


def _hash_ip(request_obj):
    raw_ip = (request_obj.headers.get("X-Forwarded-For") or request_obj.remote_addr or "").split(",")[0].strip()
    if not raw_ip:
        return "unknown"
    return hashlib.sha256(raw_ip.encode("utf-8")).hexdigest()[:16]


def _user_agent_label(request_obj):
    user_agent = (request_obj.user_agent.string or "").lower()
    if any(token in user_agent for token in ("mobile", "android", "iphone")):
        return "Mobile"
    return "Desktop"


def _referrer_domain(request_obj):
    referrer = request_obj.referrer or ""
    if not referrer:
        return "direct"
    return (urlparse(referrer).netloc or "direct").lower()


def _referrer_group(domain):
    if not domain or domain == "direct":
        return "Direct"
    if any(token in domain for token in SEARCH_REFERRERS):
        return "Search"
    if any(token in domain for token in SOCIAL_REFERRERS):
        return "Social"
    return "Other"


def record_page_view(pricelist_id, request_obj, product_id=None):
    """アクセス記録の失敗で公開画面を止めない。"""
    session_db = SessionLocal()
    try:
        session_db.add(
            CatalogPageView(
                pricelist_id=pricelist_id,
                ip_hash=_hash_ip(request_obj),
                user_agent_short=_user_agent_label(request_obj),
                referrer_domain=_referrer_domain(request_obj),
                product_id=product_id,
            )
        )
        session_db.commit()
    except Exception:
        session_db.rollback()
    finally:
        session_db.close()


@catalog_bp.route("/catalog/<token>")
def catalog_view(token):
    """公開カタログ表示"""
    session_db = SessionLocal()
    try:
        pl = _pricelist_by_token(session_db, token)
        if not pl:
            abort(404)

        record_page_view(pl.id, request)

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
        shop_logo = None
        for item in items:
            catalog_item = _build_catalog_item(item)
            if catalog_item is not None:
                catalog_items.append(catalog_item)
            
            # Find the first available shop logo
            if shop_logo is None and item.product and item.product.shop and item.product.shop.logo_url:
                shop_logo = item.product.shop.logo_url

        return render_template(
            "catalog.html",
            pricelist=pl,
            items=catalog_items,
            currency_rate=pl.currency_rate,
            shop_logo=shop_logo,
        )
    finally:
        session_db.close()


@catalog_bp.route("/catalog/<token>/product/<int:product_id>")
def catalog_product_detail(token, product_id):
    """公開カタログ用の商品詳細 JSON."""
    session_db = SessionLocal()
    try:
        pl = _pricelist_by_token(session_db, token)
        if not pl:
            return jsonify({"error": "Not found"}), 404

        item = (
            session_db.query(PriceListItem)
            .filter(
                PriceListItem.price_list_id == pl.id,
                PriceListItem.product_id == product_id,
                PriceListItem.visible == True,
            )
            .join(Product)
            .options(subqueryload(PriceListItem.product).subqueryload(Product.snapshots))
            .options(subqueryload(PriceListItem.product).subqueryload(Product.variants))
            .first()
        )
        if not item:
            return jsonify({"error": "Not found"}), 404

        catalog_item = _build_catalog_item(item)
        if catalog_item is None:
            return jsonify({"error": "Not found"}), 404

        record_page_view(pl.id, request, product_id=product_id)

        return jsonify(catalog_item)
    finally:
        session_db.close()


@catalog_bp.route("/pricelists/<int:pricelist_id>/analytics")
@login_required
def pricelist_analytics(pricelist_id):
    """価格表アクセス解析ページ"""
    session_db = SessionLocal()
    try:
        pl = (
            session_db.query(PriceList)
            .filter(PriceList.id == pricelist_id, PriceList.user_id == current_user.id)
            .first()
        )
        if not pl:
            abort(404)

        views = (
            session_db.query(CatalogPageView)
            .filter(CatalogPageView.pricelist_id == pl.id)
            .order_by(CatalogPageView.viewed_at.desc())
            .all()
        )

        now = utc_now()
        seven_days_ago = now - timedelta(days=7)
        thirty_days_ago = now - timedelta(days=30)
        fourteen_days_ago = now - timedelta(days=13)

        total_views = len(views)
        unique_visitors = len({view.ip_hash for view in views if view.ip_hash})
        last_7d_views = sum(1 for view in views if view.viewed_at and view.viewed_at >= seven_days_ago)
        last_30d_views = sum(1 for view in views if view.viewed_at and view.viewed_at >= thirty_days_ago)
        product_detail_views = sum(1 for view in views if view.product_id is not None)

        device_counter = Counter(view.user_agent_short or "Unknown" for view in views)
        referrer_counter = Counter(_referrer_group(view.referrer_domain) for view in views)
        referrer_domain_counter = Counter(view.referrer_domain or "direct" for view in views)

        daily_map = {}
        daily_labels = []
        for offset in range(14):
            day = (fourteen_days_ago + timedelta(days=offset)).date()
            daily_map[day.isoformat()] = 0
            daily_labels.append(day.strftime("%m/%d"))
        for view in views:
            if not view.viewed_at:
                continue
            key = view.viewed_at.date().isoformat()
            if key in daily_map:
                daily_map[key] += 1

        top_product_ids = [view.product_id for view in views if view.product_id is not None]
        top_product_counter = Counter(top_product_ids)
        top_product_map = {}
        if top_product_counter:
            products = (
                session_db.query(Product)
                .filter(Product.id.in_(list(top_product_counter.keys())))
                .all()
            )
            top_product_map = {
                product.id: (product.custom_title or product.last_title or f"Product #{product.id}")
                for product in products
            }

        top_products = []
        for product_id, count in top_product_counter.most_common(5):
            top_products.append({
                "product_id": product_id,
                "title": top_product_map.get(product_id, f"Product #{product_id}"),
                "views": count,
            })

        recent_views = []
        for view in views[:20]:
            recent_views.append({
                "viewed_at": view.viewed_at,
                "device": view.user_agent_short or "Unknown",
                "referrer_domain": view.referrer_domain or "direct",
                "referrer_group": _referrer_group(view.referrer_domain),
                "product_title": top_product_map.get(view.product_id, f"Product #{view.product_id}") if view.product_id else "",
            })

        chart_data = {
            "daily_labels": daily_labels,
            "daily_values": list(daily_map.values()),
            "device_labels": list(device_counter.keys()) or ["No Data"],
            "device_values": list(device_counter.values()) or [0],
            "referrer_labels": ["Direct", "Search", "Social", "Other"],
            "referrer_values": [referrer_counter.get(label, 0) for label in ("Direct", "Search", "Social", "Other")],
        }

        top_referrers = [
            {"domain": domain, "views": count}
            for domain, count in referrer_domain_counter.most_common(8)
        ]

        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')

        return render_template(
            "pricelist_analytics.html",
            pricelist=pl,
            total_views=total_views,
            unique_visitors=unique_visitors,
            last_7d_views=last_7d_views,
            last_30d_views=last_30d_views,
            product_detail_views=product_detail_views,
            top_products=top_products,
            top_referrers=top_referrers,
            recent_views=recent_views,
            chart_data=chart_data,
            all_shops=all_shops,
            current_shop_id=current_shop_id,
        )
    finally:
        session_db.close()
