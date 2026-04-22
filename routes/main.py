"""
Main routes: index and dashboard.
"""
from collections import Counter
from datetime import datetime

from flask import Blueprint, render_template, request, session, redirect, url_for
from flask_login import login_required, current_user
from sqlalchemy.orm import subqueryload
from sqlalchemy import func

from database import SessionLocal
from models import Shop, Product, Variant, ProductSnapshot
from services.rich_text import normalize_rich_text
from services.validation_service import validate_product, get_issue_summary
from time_utils import utc_now

main_bp = Blueprint('main', __name__)

PAGE_SIZE = 50

SOURCE_ON_SALE_STATUSES = {
    "active",
    "available",
    "on_sale",
    "selling",
    "在庫あり",
    "販売中",
    "出品中",
    "在庫○",
}
SOURCE_SOLD_STATUSES = {
    "sold",
    "sold_out",
    "soldout",
    "売り切れ",
}
SOURCE_DELETED_STATUSES = {
    "blocked",
    "deleted",
    "deleted_detail",
}
SITE_LABELS = {
    "manual": "手動",
    "mercari": "メルカリ",
    "offmall": "オフモール",
    "rakuma": "ラクマ",
    "snkrdunk": "スニダン",
    "surugaya": "駿河屋",
    "yahoo": "ヤフショ",
    "yahuoku": "ヤフオク",
}


def _manual_form_defaults(current_shop_id):
    return {
        "shop_id": str(current_shop_id) if current_shop_id else "",
        "title": "",
        "title_en": "",
        "description": "",
        "description_en": "",
        "cost_price": "",
        "selling_price": "",
        "inventory_qty": "1",
        "stock_state": "on_sale",
        "publish_status": "draft",
        "site": "manual",
        "source_url": "",
        "tags": "",
        "sku": "",
        "image_urls": "",
    }


def _render_manual_add(session_db, form_data=None, errors=None):
    current_shop_id = session.get('current_shop_id')
    all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
    return render_template(
        "product_manual_add.html",
        form_data=form_data or _manual_form_defaults(current_shop_id),
        errors=errors or [],
        all_shops=all_shops,
        current_shop_id=current_shop_id,
    )


def _parse_non_negative_int(raw_value, field_label, required=False):
    value = (raw_value or "").strip()
    if not value:
        if required:
            return None, f"{field_label}は必須です"
        return None, None

    try:
        parsed = int(value)
    except ValueError:
        return None, f"{field_label}は整数で入力してください"

    if parsed < 0:
        return None, f"{field_label}は0以上で入力してください"

    return parsed, None


def _normalize_manual_image_urls(raw_value):
    normalized_urls = []
    seen_urls = set()
    candidates = (raw_value or "").replace("\r", "\n").replace("|", "\n").split("\n")

    for candidate in candidates:
        url = candidate.strip()
        if not url:
            continue
        lower_url = url.lower()
        if not (
            lower_url.startswith("http://")
            or lower_url.startswith("https://")
            or url.startswith("/")
        ):
            continue
        if url in seen_urls:
            continue
        normalized_urls.append(url)
        seen_urls.add(url)

    return normalized_urls


def _normalize_publication_status(raw_status):
    normalized = str(raw_status or "").strip().lower()
    if normalized == "active":
        return "active"
    return "draft"


def _normalize_source_status(raw_status):
    normalized = str(raw_status or "").strip().lower()
    if normalized in SOURCE_ON_SALE_STATUSES:
        return "on_sale"
    if normalized in SOURCE_SOLD_STATUSES:
        return "sold"
    if normalized in SOURCE_DELETED_STATUSES:
        return "deleted"
    return "unknown"


def _latest_snapshot_for_dashboard(product):
    if not product.snapshots:
        return None

    return max(
        product.snapshots,
        key=lambda snapshot: (snapshot.scraped_at or datetime.min, snapshot.id or 0),
    )


def _split_snapshot_image_urls(snapshot):
    if not snapshot or not snapshot.image_urls:
        return []
    return [url.strip() for url in snapshot.image_urls.split("|") if url.strip()]


def _build_dashboard_product_row(product):
    latest_snapshot = _latest_snapshot_for_dashboard(product)
    image_urls = _split_snapshot_image_urls(latest_snapshot)
    issues = validate_product(product, latest_snapshot)
    error_count = sum(1 for issue in issues if issue["type"] == "error")
    warning_count = sum(1 for issue in issues if issue["type"] == "warning")
    publication_status = _normalize_publication_status(product.status)
    source_status = _normalize_source_status(product.last_status)

    return {
        "cost_price": product.last_price,
        "detail_url": url_for("products.product_detail", product_id=product.id),
        "error_count": error_count,
        "has_english_title": bool((product.custom_title_en or "").strip()),
        "id": product.id,
        "image_count": len(image_urls),
        "issues": issues,
        "publication_status": publication_status,
        "selling_price": product.selling_price,
        "site": product.site or "unknown",
        "site_label": SITE_LABELS.get(product.site, product.site or "未設定"),
        "source_status": source_status,
        "source_status_raw": product.last_status or "未設定",
        "source_url": product.source_url,
        "thumbnail_url": image_urls[0] if image_urls else None,
        "title": product.custom_title or product.last_title or f"Product #{product.id}",
        "updated_at": product.updated_at,
        "warning_count": warning_count,
    }


@main_bp.route("/dashboard")
@login_required
def dashboard():
    session_db = SessionLocal()
    try:
        current_shop_id = session.get('current_shop_id')
        
        # Align dashboard scope with the index view.
        base_query = session_db.query(Product).filter(
            Product.user_id == current_user.id,
            Product.archived != True,
            Product.deleted_at == None,
        )
        if current_shop_id:
            base_query = base_query.filter(Product.shop_id == current_shop_id)

        products = base_query.options(subqueryload(Product.snapshots)).all()
        dashboard_rows = [_build_dashboard_product_row(product) for product in products]
        total_items = len(dashboard_rows)

        publication_counts = Counter(row["publication_status"] for row in dashboard_rows)
        source_counts = Counter(row["source_status"] for row in dashboard_rows)

        zero_stock_variants_query = (
            session_db.query(func.count(Variant.id))
            .join(Product)
            .filter(
                Product.user_id == current_user.id,
                Product.archived != True,
                Product.deleted_at == None,
            )
            .filter(Variant.inventory_qty == 0)
        )
        if current_shop_id:
            zero_stock_variants_query = zero_stock_variants_query.filter(Product.shop_id == current_shop_id)

        zero_stock_variant_count = zero_stock_variants_query.scalar() or 0

        products_with_issues = [
            (row["title"], row["issues"])
            for row in dashboard_rows
        ]
        issue_summary = get_issue_summary(products_with_issues)

        ready_count = total_items - issue_summary["products_with_issues"]
        operational_notes = [
            {
                "count": source_counts.get("on_sale", 0),
                "label": "仕入先在庫あり",
            },
            {
                "count": source_counts.get("sold", 0),
                "label": "仕入先売切れ",
            },
            {
                "count": source_counts.get("unknown", 0),
                "label": "仕入先要確認",
            },
            {
                "count": zero_stock_variant_count,
                "label": "0在庫バリアント",
            },
            {
                "count": sum(1 for row in dashboard_rows if row["selling_price"] is not None and row["selling_price"] > 0),
                "label": "販売価格設定済み",
            },
            {
                "count": sum(1 for row in dashboard_rows if row["image_count"] > 0),
                "label": "画像登録済み",
            },
        ]

        recent_items = sorted(
            dashboard_rows,
            key=lambda row: row["updated_at"] or datetime.min,
            reverse=True,
        )[:8]
        attention_items = sorted(
            [row for row in dashboard_rows if row["issues"]],
            key=lambda row: (
                row["error_count"],
                row["warning_count"],
                row["updated_at"] or datetime.min,
            ),
            reverse=True,
        )[:6]

        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop = next((shop for shop in all_shops if shop.id == current_shop_id), None)
        last_updated_at = max(
            [row["updated_at"] for row in dashboard_rows if row["updated_at"]] or [None]
        )

        return render_template(
            "dashboard.html",
            attention_items=attention_items,
            current_scope_name=current_shop.name if current_shop else "全ショップ",
            generated_at=utc_now(),
            issue_summary=issue_summary,
            last_updated_at=last_updated_at,
            operational_notes=operational_notes,
            publication_counts=publication_counts,
            recent_items=recent_items,
            ready_count=ready_count,
            source_counts=source_counts,
            total_items=total_items,
            zero_stock_variant_count=zero_stock_variant_count,
            all_shops=all_shops,
            current_shop_id=current_shop_id,
        )
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()



@main_bp.route("/")
@login_required
def index():
    session_db = SessionLocal()
    try:
        raw_page = request.args.get("page", "1")
        page = int(raw_page) if str(raw_page).isdigit() else 1
        page = max(page, 1)
        selected_site = request.args.get("site")
        selected_status = request.args.get("status")
        selected_change_filter = request.args.get("change_filter")
        
        # New filter parameters
        search_keyword = request.args.get("search", "").strip()
        price_min = request.args.get("price_min", "").strip()
        price_max = request.args.get("price_max", "").strip()
        sort_by = request.args.get("sort", "updated_desc")

        # Filter query by user_id, exclude archived and deleted
        base_query = session_db.query(Product).filter(
            Product.user_id == current_user.id,
            Product.archived != True,  # Exclude archived products
            Product.deleted_at == None  # Exclude deleted products (trash)
        )

        sites = [s[0] for s in base_query.with_entities(Product.site).distinct().all()]
        statuses = [s[0] for s in base_query.with_entities(Product.last_status).distinct().all()]
        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')
        
        # Site statistics - count products per site
        site_stats = {}
        for site in sites:
            count = base_query.filter(Product.site == site).count()
            site_stats[site] = count
        total_count = base_query.count()

        query = base_query
        if current_shop_id:
            query = query.filter(Product.shop_id == current_shop_id)
        if selected_site:
            query = query.filter(Product.site == selected_site)
        if selected_status:
            query = query.filter(Product.last_status == selected_status)
        
        # Keyword search filter
        if search_keyword:
            search_pattern = f"%{search_keyword}%"
            query = query.filter(
                (Product.last_title.ilike(search_pattern)) | 
                (Product.custom_title.ilike(search_pattern))
            )
        
        # Price range filter
        if price_min:
            try:
                query = query.filter(Product.last_price >= int(price_min))
            except ValueError:
                pass
        if price_max:
            try:
                query = query.filter(Product.last_price <= int(price_max))
            except ValueError:
                pass
        
        # Sorting
        if sort_by == "price_asc":
            query = query.order_by(Product.last_price.asc().nullslast())
        elif sort_by == "price_desc":
            query = query.order_by(Product.last_price.desc().nullsfirst())
        elif sort_by == "created_desc":
            query = query.order_by(Product.created_at.desc())
        elif sort_by == "created_asc":
            query = query.order_by(Product.created_at.asc())
        else:  # default: updated_desc
            query = query.order_by(Product.updated_at.desc())

        all_products = query.options(subqueryload(Product.snapshots)).all()

        products_to_display = []
        for p in all_products:
            p.has_changed = False
            if len(p.snapshots) >= 2:
                sorted_snapshots = sorted(p.snapshots, key=lambda s: s.scraped_at, reverse=True)
                latest = sorted_snapshots[0]
                previous = sorted_snapshots[1]
                if latest.price != previous.price or latest.status != previous.status:
                    p.has_changed = True
            
            if selected_change_filter == 'changed':
                if p.has_changed:
                    products_to_display.append(p)
            else:
                products_to_display.append(p)

        total_items = len(products_to_display)
        total_pages = max(1, (total_items + PAGE_SIZE - 1) // PAGE_SIZE)
        if page > total_pages:
            page = total_pages
        offset = (page - 1) * PAGE_SIZE
        paginated_products = products_to_display[offset : offset + PAGE_SIZE]
        page_start = offset + 1 if total_items else 0
        page_end = min(offset + PAGE_SIZE, total_items)
        page_numbers = list(range(max(1, page - 2), min(total_pages, page + 2) + 1))

        has_prev = page > 1
        has_next = page < total_pages

        defaults = {
            "markup": request.args.get("markup", "1.2"),
            "qty": request.args.get("qty", "1"),
            "rate": request.args.get("rate", "155"),
        }

        return render_template(
            "index.html",
            products=paginated_products,
            sites=sites,
            statuses=statuses,
            selected_site=selected_site,
            selected_status=selected_status,
            selected_change_filter=selected_change_filter,
            # New filter values
            search_keyword=search_keyword,
            price_min=price_min,
            price_max=price_max,
            sort_by=sort_by,
            site_stats=site_stats,
            total_count=total_count,
            # Pagination
            page=page,
            page_end=page_end,
            page_numbers=page_numbers,
            page_start=page_start,
            total_items=total_items,
            total_pages=total_pages,
            has_prev=has_prev,
            has_next=has_next,
            default_markup=defaults["markup"],
            default_qty=defaults["qty"],
            default_rate=defaults["rate"],
            all_shops=all_shops,
            current_shop_id=current_shop_id
        )
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


@main_bp.route("/products/manual-add", methods=["GET", "POST"])
@login_required
def product_manual_add():
    session_db = SessionLocal()
    try:
        if request.method == "GET":
            return _render_manual_add(session_db)

        form_data = _manual_form_defaults(session.get('current_shop_id'))
        for key in form_data.keys():
            form_data[key] = request.form.get(key, "").strip()

        errors = []

        title = form_data["title"]
        if not title:
            errors.append("商品名は必須です")

        shop_id = None
        if form_data["shop_id"]:
            try:
                requested_shop_id = int(form_data["shop_id"])
            except ValueError:
                errors.append("ショップ指定が不正です")
            else:
                shop = session_db.query(Shop).filter_by(
                    id=requested_shop_id,
                    user_id=current_user.id,
                ).one_or_none()
                if not shop:
                    errors.append("選択したショップが見つかりません")
                else:
                    shop_id = shop.id

        cost_price, cost_error = _parse_non_negative_int(
            form_data["cost_price"],
            "仕入価格",
            required=True,
        )
        if cost_error:
            errors.append(cost_error)

        selling_price, sell_error = _parse_non_negative_int(
            form_data["selling_price"],
            "販売価格",
            required=False,
        )
        if sell_error:
            errors.append(sell_error)

        inventory_qty, qty_error = _parse_non_negative_int(
            form_data["inventory_qty"],
            "在庫数",
            required=False,
        )
        if qty_error:
            errors.append(qty_error)
        if inventory_qty is None:
            inventory_qty = 1

        stock_state = form_data["stock_state"] if form_data["stock_state"] in {"on_sale", "sold"} else "on_sale"
        publish_status = form_data["publish_status"] if form_data["publish_status"] in {"active", "draft"} else "draft"
        site_name = form_data["site"] or "manual"
        source_url = form_data["source_url"]

        if source_url:
            existing = session_db.query(Product).filter_by(
                user_id=current_user.id,
                source_url=source_url,
            ).first()
            if existing:
                errors.append("同じ元URLの商品が既に登録されています")

        if errors:
            return _render_manual_add(session_db, form_data=form_data, errors=errors)

        effective_inventory_qty = 0 if stock_state == "sold" else inventory_qty
        effective_last_status = "sold" if stock_state == "sold" else "on_sale"
        normalized_images = _normalize_manual_image_urls(form_data["image_urls"])
        normalized_description = normalize_rich_text(form_data["description"]) or None
        normalized_description_en = normalize_rich_text(form_data["description_en"]) or None
        now = utc_now()

        product = Product(
            user_id=current_user.id,
            shop_id=shop_id,
            site=site_name,
            source_url=source_url,
            last_title=title,
            last_price=cost_price,
            last_status=effective_last_status,
            custom_title=title,
            custom_description=normalized_description,
            custom_title_en=form_data["title_en"] or None,
            custom_description_en=normalized_description_en,
            status=publish_status,
            tags=form_data["tags"] or None,
            selling_price=selling_price,
            created_at=now,
            updated_at=now,
        )
        session_db.add(product)
        session_db.flush()

        snapshot = ProductSnapshot(
            product_id=product.id,
            scraped_at=now,
            title=title,
            price=cost_price,
            status=effective_last_status,
            description=normalized_description,
            image_urls="|".join(normalized_images),
        )
        session_db.add(snapshot)

        variant = Variant(
            product_id=product.id,
            option1_value="Default Title",
            sku=form_data["sku"] or None,
            price=selling_price if selling_price is not None else cost_price,
            inventory_qty=effective_inventory_qty,
            position=1,
        )
        session_db.add(variant)

        session_db.commit()
        return redirect(url_for('products.product_detail', product_id=product.id))
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


@main_bp.route("/batch-edit", methods=["POST"])
@login_required
def batch_edit():
    """Handle batch title editing operations."""
    action = request.form.get("action")
    input_value = request.form.get("input", "")
    input2_value = request.form.get("input2", "")
    product_ids = request.form.getlist("ids")
    
    if not product_ids or not action:
        return redirect(url_for('main.index'))
    
    session_db = SessionLocal()
    try:
        # Get products owned by current user
        products = session_db.query(Product).filter(
            Product.id.in_([int(pid) for pid in product_ids]),
            Product.user_id == current_user.id
        ).all()
        
        updated_count = 0
        for product in products:
            original_title = product.custom_title or product.last_title or ""
            new_title = original_title
            
            if action == "prefix":
                new_title = input_value + original_title
            elif action == "suffix":
                new_title = original_title + input_value
            elif action == "replace":
                new_title = original_title.replace(input_value, input2_value)
            
            if new_title != original_title:
                product.custom_title = new_title
                updated_count += 1
        
        session_db.commit()
        
        # Flash message would be ideal, but redirect with success param works too
        return redirect(url_for('main.index'))
    except Exception as e:
        session_db.rollback()
        print(f"Batch edit error: {e}")
        return redirect(url_for('main.index'))
    finally:
        session_db.close()

