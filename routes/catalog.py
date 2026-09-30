"""
Public catalog routes: token-based access for overseas customers.
No login required.
"""
import hashlib
import re
from collections import Counter
from datetime import timedelta
from urllib.parse import unquote, urlparse

from flask import Blueprint, render_template, abort, current_app, jsonify, request, session
from flask_login import login_required, current_user
from sqlalchemy import func, or_, select
from sqlalchemy.orm import joinedload, subqueryload

from database import SessionLocal, _session_factory
from models import Shop, PriceList, PriceListItem, Product, ProductSnapshot, ProductThumbnailJob, CatalogPageView, User
from services.exchange_rate_service import apply_safety_margin, get_exchange_rates
from services.image_service import split_image_url_string
from services.pricing_service import resolve_product_display_price
from services.rich_text import build_rich_text_excerpt, normalize_rich_text, rich_text_to_plain_text
from services.rate_limit_service import get_client_ip, get_rate_limiter
from time_utils import utc_now

catalog_bp = Blueprint('catalog', __name__)

SEARCH_REFERRERS = ("google.", "bing.", "yahoo.", "duckduckgo.", "baidu.", "ecosia.")
SOCIAL_REFERRERS = ("facebook.", "instagram.", "tiktok.", "twitter.", "x.com", "youtube.", "line.", "pinterest.")
DEFAULT_CURRENCY_RATE = 150
DETAIL_START_WINDOW_SECONDS = 900
DETAIL_START_LIMIT = 20
DETAIL_OWNER_START_LIMIT = 300
DETAIL_POLL_WINDOW_SECONDS = 300
DETAIL_POLL_LIMIT = 120
THUMBNAIL_POLL_LIMIT = 90
THUMBNAIL_BATCH_LIMIT = 50


def _latest_snapshot(product):
    if hasattr(product, "latest_snapshot"):
        return product.latest_snapshot
    if not product.snapshots:
        return None
    return sorted(product.snapshots, key=lambda s: s.scraped_at, reverse=True)[0]


def _safe_currency_rate(raw_value):
    """Keep legacy/corrupt rows from producing Infinity in catalog JavaScript."""
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return DEFAULT_CURRENCY_RATE
    return parsed if parsed > 0 else DEFAULT_CURRENCY_RATE


def _build_server_rates(session_db, owner_user_id):
    """
    Return {currency: units per 1 JPY} from the daily refresh.

    The owner's safety margin shades each rate so a market move between two
    morning refreshes cannot make the foreign price undercut the yen price.
    Returns an empty dict when nothing has been fetched yet, which leaves the
    catalog on its own client-side lookup.
    """
    owner = session_db.query(User).filter(User.id == owner_user_id).first()
    margin = owner.exchange_rate_margin if owner else 0

    server_rates = {}
    for rate in get_exchange_rates(session_db):
        jpy_per_unit = apply_safety_margin(rate["jpy_per_unit"], margin)
        if jpy_per_unit > 0:
            server_rates[rate["code"]] = 1 / jpy_per_unit
    if server_rates:
        server_rates["JPY"] = 1
    return server_rates


def _attach_latest_snapshots(session_db, products):
    product_ids = [product.id for product in products if product is not None]
    if not product_ids:
        return

    ranked_snapshots = (
        select(
            ProductSnapshot.id.label("snapshot_id"),
            ProductSnapshot.product_id.label("product_id"),
            func.row_number()
            .over(
                partition_by=ProductSnapshot.product_id,
                order_by=(ProductSnapshot.scraped_at.desc(), ProductSnapshot.id.desc()),
            )
            .label("snapshot_rank"),
        )
        .where(ProductSnapshot.product_id.in_(product_ids))
        .subquery()
    )
    snapshots = (
        session_db.query(ProductSnapshot)
        .join(ranked_snapshots, ProductSnapshot.id == ranked_snapshots.c.snapshot_id)
        .filter(ranked_snapshots.c.snapshot_rank == 1)
        .all()
    )
    snapshots_by_product = {snapshot.product_id: snapshot for snapshot in snapshots}
    for product in products:
        if product is not None:
            product.latest_snapshot = snapshots_by_product.get(product.id)


def _public_catalog_image_url(raw_url):
    """Return only an application-served media/static URL.

    Snapshot rows intentionally retain source CDN URLs for the operator UI.
    Public catalogs, however, must never make a customer's browser contact
    those procurement sources.
    """
    value = str(raw_url or "").strip()
    if not value:
        return None

    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or parsed.params:
        return None

    decoded_path = unquote(parsed.path or "")
    if "\\" in decoded_path or "\x00" in decoded_path:
        return None
    path_parts = decoded_path.split("/")
    if ".." in path_parts:
        return None
    if not decoded_path.startswith(("/media/", "/static/")):
        return None
    # Query strings/fragments are unnecessary for managed images and could
    # otherwise smuggle a source URL back into the public response.
    return parsed.path


def _public_catalog_image_urls(raw_urls):
    public_urls = []
    seen = set()
    for raw_url in raw_urls:
        public_url = _public_catalog_image_url(raw_url)
        if public_url and public_url not in seen:
            public_urls.append(public_url)
            seen.add(public_url)
    return public_urls


def _public_catalog_title(product):
    curated_title = str(product.custom_title or "").strip()
    if curated_title:
        return curated_title

    fallback_title = str(product.last_title or "").strip()
    if not fallback_title:
        return "(No Title)"

    normalized_title = fallback_title.lower()
    if "://" in normalized_title or normalized_title.startswith("//"):
        return "(No Title)"

    source_url = str(product.source_url or "").strip()
    source_parts = urlparse(source_url)
    source_identifiers = {
        source_url.lower(),
        str(source_parts.hostname or "").lower(),
    }
    source_path_parts = [
        part.strip().lower()
        for part in (source_parts.path or "").split("/")
        if part.strip()
    ]
    if source_path_parts and len(source_path_parts[-1]) >= 6:
        source_identifiers.add(source_path_parts[-1])
    source_identifiers.discard("")
    if any(identifier in normalized_title for identifier in source_identifiers):
        return "(No Title)"

    source_site = str(product.site or "").strip().lower()
    if source_site and normalized_title == source_site:
        return "(No Title)"
    return fallback_title


def _pricelist_by_token(session_db, token):
    now = utc_now()
    return (
        session_db.query(PriceList)
        .options(joinedload(PriceList.shop))
        .join(User, User.id == PriceList.user_id)
        # A suspended student's lists go dark with the account and come back
        # untouched when it resumes, so a break in attendance does not mean
        # rebuilding the catalog afterwards.
        .filter(
            PriceList.token == token,
            PriceList.is_active.is_(True),
            User.suspended_at.is_(None),
            or_(PriceList.unpublish_at.is_(None), PriceList.unpublish_at > now),
        )
        .first()
    )


#: Instagram path segments that are part of the site rather than a person.
_INSTAGRAM_RESERVED_PATHS = frozenset(
    {
        "p", "reel", "reels", "tv", "stories", "s", "explore", "direct",
        "accounts", "about", "developer", "legal", "privacy", "terms",
    }
)


def _instagram_username_from_url(value):
    """Pull the profile name out of an Instagram URL, or "" if it is not one."""
    parsed = urlparse(value if "//" in value else "//" + value)
    host = (parsed.hostname or "").lower()
    if host != "instagram.com" and not host.endswith(".instagram.com"):
        return ""

    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        return ""
    # Only a profile URL names a person; /p/<id> and friends do not.
    if len(segments) > 1 or segments[0].lower() in _INSTAGRAM_RESERVED_PATHS:
        return ""
    return segments[0]


def _normalize_instagram_username(raw_value):
    """
    Return a bare Instagram username, or "" if it does not look like one.

    Operators paste all sorts of things into this field — "@name", a profile
    URL, a name with spaces — and the value ends up as a direct message link
    on a public page. A wrong guess sends customers to a stranger's inbox, so
    anything that is not plainly a profile is dropped rather than rendered.
    """
    value = str(raw_value or "").strip()
    if not value:
        return ""

    if "/" in value or ":" in value:
        value = _instagram_username_from_url(value)
    else:
        value = value.split("?", 1)[0]
    value = value.lstrip("@").strip()

    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", value):
        return ""
    return value


def _resolve_catalog_contact(pricelist, items):
    """Find the Instagram handle to offer customers, the same way the logo is found."""
    owner_id = getattr(pricelist, "user_id", None)
    related_shop = getattr(pricelist, "shop", None)
    if (
        related_shop is not None
        and related_shop.user_id == owner_id
    ):
        username = _normalize_instagram_username(related_shop.instagram_username)
        if username:
            return username

    for item in items:
        product = getattr(item, "product", None)
        shop = getattr(product, "shop", None)
        if (
            product is not None
            and product.user_id == owner_id
            and shop is not None
            and shop.user_id == owner_id
        ):
            username = _normalize_instagram_username(shop.instagram_username)
            if username:
                return username
    return ""


def _resolve_catalog_shop_branding(pricelist, items):
    owner_id = getattr(pricelist, "user_id", None)
    related_shop = getattr(pricelist, "shop", None)
    explicit_shop = (
        related_shop
        if related_shop is not None and related_shop.user_id == owner_id
        else None
    )
    if explicit_shop:
        logo_url = _public_catalog_image_url(explicit_shop.logo_url)
        if logo_url:
            return logo_url, explicit_shop.name

    for item in items:
        product = getattr(item, "product", None)
        shop = getattr(product, "shop", None)
        if (
            product is not None
            and product.user_id == owner_id
            and shop is not None
            and shop.user_id == owner_id
        ):
            logo_url = _public_catalog_image_url(shop.logo_url)
            if logo_url:
                return logo_url, shop.name

    if explicit_shop:
        return None, explicit_shop.name
    return None, None


def _split_catalog_tags(raw_tags):
    if not raw_tags:
        return []

    normalized = []
    seen = set()
    for part in raw_tags.split(","):
        candidate = (part or "").strip()
        if not candidate:
            continue

        key = candidate.lower()
        if key in seen:
            continue

        seen.add(key)
        normalized.append(candidate)

    return normalized


def _public_detail_status(product):
    """Expose availability, never the worker's internal states or errors."""
    if product.last_status in {"sold", "deleted"}:
        return "unavailable"
    state = product.detail_fetch_state
    if state is None or state == "complete":
        stock = sum(variant.inventory_qty or 0 for variant in product.variants)
        return "ready" if stock > 0 else "unavailable"
    if state in {"queued", "running"}:
        return "pending"
    if state == "pending" and product.detail_source_url == product.source_url:
        # Admission can retain selected demand in the database until capacity
        # becomes available. Unselected cards have no captured source.
        return "pending"
    if product.detail_retry_at and product.detail_retry_at > utc_now():
        return "pending"
    return "none"


def _build_catalog_item(item):
    p = item.product
    if p.archived or p.deleted_at:
        return None

    snapshot = _latest_snapshot(p)
    image_urls = _public_catalog_image_urls(
        split_image_url_string(snapshot.image_urls if snapshot else None)
    )

    display_title = _public_catalog_title(p)
    if p.detail_fetch_state is None:
        # Only legacy products retain the source-price fallback.
        resolved_product_price = resolve_product_display_price(p, p.variants)
    elif p.selling_price is not None:
        # An explicit product sale price also supports the existing variant
        # pricing contract. Completing details never removes staged provenance.
        resolved_product_price = resolve_product_display_price(p, p.variants)
    else:
        explicit_prices = [variant.selling_price for variant in p.variants if variant.selling_price is not None]
        resolved_product_price = min(explicit_prices) if explicit_prices else None
    display_price = (
        item.custom_price
        if item.custom_price is not None
        else resolved_product_price
    )
    detail_status = _public_detail_status(p)
    # A list card's placeholder stock must not become an orderable quantity,
    # even if a manual edit has populated variants before verification.
    total_stock = sum(v.inventory_qty or 0 for v in p.variants) if detail_status == "ready" else 0

    # Public catalog: prefer curated custom content over raw scraped text.
    # Never expose source_url, site, or other internal sourcing details.
    description = p.custom_description or ""
    description_en = p.custom_description_en or ""
    description_html = normalize_rich_text(description)
    description_en_html = normalize_rich_text(description_en)
    description_text = rich_text_to_plain_text(description)
    description_en_text = rich_text_to_plain_text(description_en)
    tags = _split_catalog_tags(p.tags)

    return {
        "product_id": p.id,
        "title": display_title,
        "title_en": p.custom_title_en or "",
        "price": display_price,
        "thumb_url": image_urls[0] if image_urls else "",
        "image_urls": image_urls,
        "stock": total_stock,
        "in_stock": total_stock > 0,
        "detail_status": detail_status,
        "description_html": description_html,
        "description_en_html": description_en_html,
        "description_text": description_text,
        "description_en_text": description_en_text,
        "description_snippet": build_rich_text_excerpt(description_en or description, limit=80),
        "tags": tags,
    }


def _detail_catalog_item(session_db, pricelist, product_id):
    """Validate the public token's exact product/shop scope before queuing."""
    if pricelist.shop_id is not None and (
        pricelist.shop is None or pricelist.shop.user_id != pricelist.user_id
    ):
        return None
    query = (
        session_db.query(PriceListItem)
        .join(Product)
        .filter(
            PriceListItem.price_list_id == pricelist.id,
            PriceListItem.product_id == product_id,
            PriceListItem.visible.is_(True),
            Product.user_id == pricelist.user_id,
            Product.archived.is_(False),
            Product.deleted_at.is_(None),
        )
        .options(joinedload(PriceListItem.product).subqueryload(Product.variants))
        .options(joinedload(PriceListItem.product).joinedload(Product.shop))
    )
    if pricelist.shop_id is not None:
        query = query.filter(Product.shop_id == pricelist.shop_id)
    item = query.first()
    if item and item.product.shop_id is not None and (
        item.product.shop is None or item.product.shop.user_id != pricelist.user_id
    ):
        return None
    return item


def _detail_rate_limit(pricelist):
    """Bound public work requests with the existing atomic shared limiter."""
    starting = request.method == "POST"
    scope = "catalog_detail_start" if starting else "catalog_detail_poll"
    window = DETAIL_START_WINDOW_SECONDS if starting else DETAIL_POLL_WINDOW_SECONDS
    limit = DETAIL_START_LIMIT if starting else DETAIL_POLL_LIMIT
    try:
        limiter = get_rate_limiter()
        count = limiter.increment(scope, f"{pricelist.token}:{get_client_ip(request)}", window)
        owner_count = limiter.increment(
            "catalog_detail_owner_start", str(pricelist.user_id), window,
        ) if starting and count <= limit else 0
    except Exception:
        return jsonify(error="Availability checks are temporarily unavailable."), 503
    if count > limit or owner_count > DETAIL_OWNER_START_LIMIT:
        response = jsonify(error="Please wait before checking availability again.")
        response.headers["Retry-After"] = str(window)
        return response, 429
    return None


@catalog_bp.route("/catalog/<token>/thumbnails", methods=["GET"])
def catalog_thumbnails(token):
    """Read image availability for a bounded visible batch; never start work."""
    raw_ids = (request.args.get("product_ids") or "").split(",")
    if not raw_ids or len(raw_ids) > THUMBNAIL_BATCH_LIMIT or any(
        re.fullmatch(r"[1-9][0-9]{0,17}", value) is None for value in raw_ids
    ):
        return jsonify(error="Invalid image request."), 400
    product_ids = list(dict.fromkeys(int(value) for value in raw_ids))
    session_db = SessionLocal()
    try:
        pricelist = _pricelist_by_token(session_db, token)
        if pricelist is None or pricelist.shop_id is not None and (
            pricelist.shop is None or pricelist.shop.user_id != pricelist.user_id
        ):
            return jsonify(error="Not found"), 404
        try:
            count = get_rate_limiter().increment(
                "catalog_thumbnail_poll", f"{token}:{get_client_ip(request)}", DETAIL_POLL_WINDOW_SECONDS,
            )
        except Exception:
            return jsonify(error="Images are temporarily unavailable."), 503
        if count > THUMBNAIL_POLL_LIMIT:
            response = jsonify(error="Please wait before refreshing images.")
            response.headers["Retry-After"] = str(DETAIL_POLL_WINDOW_SECONDS)
            return response, 429
        ranked_snapshots = select(
            ProductSnapshot.id.label("snapshot_id"), ProductSnapshot.product_id.label("product_id"),
            func.row_number().over(partition_by=ProductSnapshot.product_id,
                order_by=(ProductSnapshot.scraped_at.desc(), ProductSnapshot.id.desc())).label("snapshot_rank"),
        ).where(ProductSnapshot.product_id.in_(product_ids)).subquery()
        # Read product scope, latest images and request state in one statement:
        # a transfer between separate product/image reads must not expose the
        # receiving owner's newly written snapshot to an old catalog token.
        query = session_db.query(Product, ProductSnapshot, ProductThumbnailJob).join(
            PriceListItem, PriceListItem.product_id == Product.id,
        ).join(PriceList, PriceList.id == PriceListItem.price_list_id).join(
            User, User.id == PriceList.user_id,
        ).outerjoin(ranked_snapshots, (ranked_snapshots.c.product_id == Product.id) & (ranked_snapshots.c.snapshot_rank == 1)).outerjoin(
            ProductSnapshot, ProductSnapshot.id == ranked_snapshots.c.snapshot_id,
        ).outerjoin(ProductThumbnailJob, ProductThumbnailJob.product_id == Product.id).filter(
            PriceListItem.price_list_id == pricelist.id,
            PriceListItem.product_id.in_(product_ids), PriceListItem.visible.is_(True),
            PriceList.token == token, PriceList.user_id == pricelist.user_id, PriceList.shop_id == pricelist.shop_id,
            PriceList.is_active.is_(True), User.suspended_at.is_(None),
            or_(PriceList.unpublish_at.is_(None), PriceList.unpublish_at > utc_now()),
            Product.user_id == pricelist.user_id, Product.archived.is_(False), Product.deleted_at.is_(None),
            or_(Product.shop_id.is_(None), Product.shop_id.in_(session_db.query(Shop.id).filter(Shop.user_id == pricelist.user_id))),
        )
        if pricelist.shop_id is not None:
            query = query.filter(Product.shop_id == pricelist.shop_id)
        rows = query.all()
        products = {product.id: (product, snapshot, job) for product, snapshot, job in rows}
        if set(products) != set(product_ids):
            return jsonify(error="Not found"), 404
        results = []
        for product_id in product_ids:
            product, snapshot, job = products[product_id]
            urls = _public_catalog_image_urls(split_image_url_string(snapshot.image_urls if snapshot else None))
            status = "ready" if urls else "unavailable"
            if not urls and job is not None and snapshot is not None and (
                job.state in ("pending", "queued", "running")
                and job.user_id == product.user_id and job.shop_id == product.shop_id
                and job.product_source_url == product.source_url
                and job.source_snapshot_id == snapshot.id and job.source_image_url == snapshot.image_urls
                and product.site == "recordcity"
            ):
                status = "pending"
            results.append({"product_id": product_id, "status": status, "thumb_url": urls[0] if urls else ""})
        response = jsonify(items=results)
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception as error:
        # Remote source URLs and worker error details are never public output.
        current_app.logger.warning("Catalog image refresh failed (%s)", type(error).__name__)
        return jsonify(error="Images are temporarily unavailable."), 503
    finally:
        session_db.close()


@catalog_bp.route("/catalog/<token>/products/<int:product_id>/details", methods=["GET", "POST"])
def catalog_product_details(token, product_id):
    """Start deferred details or poll only the token's visible product."""
    session_db = SessionLocal()
    try:
        pricelist = _pricelist_by_token(session_db, token)
        item = _detail_catalog_item(session_db, pricelist, product_id) if pricelist else None
        if item is None:
            return jsonify(error="Not found"), 404
        limited = _detail_rate_limit(pricelist)
        if limited is not None:
            return limited
        if request.method == "POST" and _public_detail_status(item.product) not in {"ready", "unavailable"}:
            owner_id, shop_id = pricelist.user_id, item.product.shop_id
            # Release the read transaction before the queue service claims the
            # product in its own transaction. It validates ownership again.
            session_db.rollback()
            from services.product_detail_jobs import enqueue_product_details
            result = enqueue_product_details([product_id], owner_id, shop_id=shop_id, respect_backoff=True)
            session_db.expire_all()
            pricelist = _pricelist_by_token(session_db, token)
            item = _detail_catalog_item(session_db, pricelist, product_id) if pricelist else None
            if item is None:
                return jsonify(error="Not found"), 404
            if result.get("failed") and _public_detail_status(item.product) == "none":
                return jsonify(status="none", retry_after_seconds=30), 503
        _attach_latest_snapshots(session_db, [item.product])
        public_item = _build_catalog_item(item)
        status = public_item["detail_status"]
        payload = {"status": status, "item": public_item}
        if status == "pending":
            retry_at = item.product.detail_retry_at
            default_retry = 30 if item.product.detail_fetch_state == "pending" else 3
            payload["retry_after_seconds"] = max(
                3, min(3600, int((retry_at - utc_now()).total_seconds()) + 1),
            ) if retry_at and retry_at > utc_now() else default_retry
        return jsonify(payload), 202 if status == "pending" else 200
    except Exception as error:
        session_db.rollback()
        # Worker exceptions can contain source URLs, credentials or raw HTML.
        current_app.logger.warning("Catalog availability check failed (%s)", type(error).__name__)
        return jsonify(error="Availability checks are temporarily unavailable.", status="none"), 503
    finally:
        session_db.close()


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
    session_db = _session_factory()
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
            .filter(Product.user_id == pl.user_id)
            .options(joinedload(PriceListItem.product).subqueryload(Product.variants))
            .options(joinedload(PriceListItem.product).joinedload(Product.shop))
            .order_by(PriceListItem.sort_order)
            .all()
        )
        _attach_latest_snapshots(session_db, [item.product for item in items])

        # Process items for display
        catalog_items = []
        for item in items:
            catalog_item = _build_catalog_item(item)
            if catalog_item is not None:
                catalog_items.append(catalog_item)
        available_tags = sorted(
            {tag for catalog_item in catalog_items for tag in catalog_item["tags"]},
            key=str.lower,
        )
        shop_logo, shop_name = _resolve_catalog_shop_branding(pl, items)

        return render_template(
            "catalog.html",
            pricelist=pl,
            catalog_notes=normalize_rich_text(pl.notes),
            items=catalog_items,
            available_tags=available_tags,
            currency_rate=_safe_currency_rate(pl.currency_rate),
            server_rates=_build_server_rates(session_db, pl.user_id),
            shop_logo=shop_logo,
            shop_name=shop_name,
            instagram_username=_resolve_catalog_contact(pl, items),
            shipping_note=(pl.shipping_note or "").strip(),
        )
    except Exception:
        session_db.rollback()
        raise
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
            .filter(Product.user_id == pl.user_id)
            .options(joinedload(PriceListItem.product).subqueryload(Product.variants))
            .first()
        )
        if not item:
            return jsonify({"error": "Not found"}), 404

        _attach_latest_snapshots(session_db, [item.product])
        catalog_item = _build_catalog_item(item)
        if catalog_item is None:
            return jsonify({"error": "Not found"}), 404

        record_page_view(pl.id, request, product_id=product_id)

        return jsonify(catalog_item)
    except Exception:
        session_db.rollback()
        raise
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
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()
