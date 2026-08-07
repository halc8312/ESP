"""
Price List management routes: create, edit, delete, manage items.
"""
import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import login_required, current_user
from sqlalchemy.orm import subqueryload

from database import SessionLocal
from models import Shop, Product, ProductSnapshot, Variant, PriceList, PriceListItem
from services.image_service import split_image_url_string
from services.pricing_service import resolve_product_display_price
from services.rich_text import normalize_rich_text
from time_utils import utc_now

pricelist_bp = Blueprint('pricelist', __name__)

PRICE_LIST_LAYOUTS = {"grid", "editorial", "list"}
PRICE_LIST_THEMES = {"dark", "light"}
PRICE_LIST_TYPES = {"permanent", "limited"}
JAPAN_TIMEZONE = ZoneInfo("Asia/Tokyo")
DATETIME_LOCAL_FORMAT = "%Y-%m-%dT%H:%M"


def _sanitize_notes(raw_html: str) -> str:
    """Normalize pricelist notes into the shared safe rich-text subset."""
    return normalize_rich_text(raw_html)


def _normalize_layout(value):
    layout = (value or "").strip().lower()
    if layout in PRICE_LIST_LAYOUTS:
        return layout
    return "grid"


def _normalize_theme(value):
    theme = (value or "").strip().lower()
    if theme in PRICE_LIST_THEMES:
        return theme
    return "dark"


def _normalize_list_type(value):
    list_type = (value or "").strip().lower()
    if list_type in PRICE_LIST_TYPES:
        return list_type
    return "permanent"


def _describe_remaining_time(unpublish_at, now=None):
    """Return e.g. "あと2日" for an upcoming deadline, or None."""
    if unpublish_at is None:
        return None

    remaining = unpublish_at - (now or utc_now())
    total_seconds = remaining.total_seconds()
    if total_seconds <= 0:
        return None

    days = int(total_seconds // 86400)
    if days >= 1:
        return f"あと{days}日"

    hours = int(total_seconds // 3600)
    if hours >= 1:
        return f"あと{hours}時間"

    minutes = max(1, int(total_seconds // 60))
    return f"あと{minutes}分"


def _parse_currency_rate(raw_value):
    """Return a positive JPY/USD conversion rate or a form error."""
    value = str(raw_value if raw_value is not None else "150").strip()
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None, "通貨換算レートは1以上の整数で入力してください"
    if parsed <= 0:
        return None, "通貨換算レートは1以上の整数で入力してください"
    return parsed, None


def _parse_optional_custom_price(raw_value):
    """Parse an optional non-negative customer-facing item price."""
    value = str(raw_value or "").strip()
    if not value:
        return None, None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None, "カスタム価格は0以上の整数で入力してください"
    if parsed < 0:
        return None, "カスタム価格は0以上の整数で入力してください"
    return parsed, None


def _parse_unpublish_at_jst(raw_value):
    """Parse a datetime-local value as Japan time and return naive UTC."""
    value = (raw_value or "").strip()
    if not value:
        return None, None

    try:
        local_naive = datetime.strptime(value, DATETIME_LOCAL_FORMAT)
    except (TypeError, ValueError):
        return None, "自動非公開日時を正しく入力してください"

    utc_naive = (
        local_naive.replace(tzinfo=JAPAN_TIMEZONE)
        .astimezone(UTC)
        .replace(tzinfo=None)
    )
    if utc_naive <= utc_now():
        return None, "自動非公開日時は現在より後の日時を指定してください"
    return utc_naive, None


def _format_unpublish_at_jst(value):
    """Format a stored naive-UTC timestamp for a datetime-local control."""
    if value is None:
        return ""
    return (
        value.replace(tzinfo=UTC)
        .astimezone(JAPAN_TIMEZONE)
        .strftime(DATETIME_LOCAL_FORMAT)
    )


def _resolve_owned_shop(session_db, raw_shop_id):
    shop_id_raw = (raw_shop_id or "").strip()
    if not shop_id_raw:
        return None, None

    try:
        shop_id = int(shop_id_raw)
    except (TypeError, ValueError):
        return None, "ショップを正しく選択してください"

    shop = session_db.query(Shop).filter_by(id=shop_id, user_id=current_user.id).first()
    if not shop:
        return None, "選択したショップが見つかりません"

    return shop, None


@pricelist_bp.route("/pricelists")
@login_required
def pricelist_list():
    """価格表一覧"""
    session_db = SessionLocal()
    try:
        pricelists = (
            session_db.query(PriceList)
            .filter(PriceList.user_id == current_user.id)
            .order_by(PriceList.updated_at.desc())
            .all()
        )
        # Count items for each pricelist
        now = utc_now()
        for pl in pricelists:
            pl.item_count = (
                session_db.query(PriceListItem)
                .filter(PriceListItem.price_list_id == pl.id)
                .count()
            )
            pl.publication_state = pl.publication_state_at(now)
            pl.remaining_label = _describe_remaining_time(pl.unpublish_at, now)

        permanent_lists = [pl for pl in pricelists if pl.list_type != "limited"]
        limited_lists = [pl for pl in pricelists if pl.list_type == "limited"]

        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')

        return render_template(
            "pricelist_list.html",
            pricelists=pricelists,
            permanent_lists=permanent_lists,
            limited_lists=limited_lists,
            all_shops=all_shops,
            current_shop_id=current_shop_id,
        )
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


@pricelist_bp.route("/pricelists/create", methods=["GET", "POST"])
@login_required
def pricelist_create():
    """新規価格表作成"""
    session_db = SessionLocal()
    try:
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            notes = _sanitize_notes(request.form.get("notes", "").strip())
            selected_currency_rate = (
                request.form.get("currency_rate", "150") or ""
            ).strip()
            currency_rate, currency_error = _parse_currency_rate(
                selected_currency_rate
            )
            layout = _normalize_layout(request.form.get("layout"))
            theme = _normalize_theme(request.form.get("theme"))
            selected_shop_id = (request.form.get("shop_id") or "").strip()
            shop, shop_error = _resolve_owned_shop(session_db, selected_shop_id)
            selected_unpublish_at = (request.form.get("unpublish_at") or "").strip()
            unpublish_at, unpublish_error = _parse_unpublish_at_jst(selected_unpublish_at)
            list_type = _normalize_list_type(request.form.get("list_type"))

            validation_error = (
                "名前を入力してください"
                if not name
                else shop_error or currency_error or unpublish_error
            )
            if validation_error:
                all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
                current_shop_id = session.get('current_shop_id')
                response = render_template(
                    "pricelist_edit.html",
                    pricelist=None,
                    error=validation_error,
                    selected_layout=layout,
                    selected_theme=theme,
                    selected_shop_id=selected_shop_id,
                    selected_unpublish_at=selected_unpublish_at,
                    selected_list_type=list_type,
                    selected_name=name,
                    selected_notes=notes,
                    selected_currency_rate=selected_currency_rate,
                    all_shops=all_shops,
                    current_shop_id=current_shop_id,
                )
                return response, 400

            new_pl = PriceList(
                user_id=current_user.id,
                shop_id=shop.id if shop else None,
                name=name,
                token=str(uuid.uuid4()),
                notes=notes,
                currency_rate=currency_rate,
                layout=layout,
                theme=theme,
                list_type=list_type,
                unpublish_at=unpublish_at,
            )
            session_db.add(new_pl)
            session_db.commit()
            return redirect(url_for("pricelist.pricelist_items", pricelist_id=new_pl.id))

        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')
        return render_template(
            "pricelist_edit.html",
            pricelist=None,
            selected_layout="grid",
            selected_theme="dark",
            selected_shop_id=str(current_shop_id) if current_shop_id else "",
            selected_unpublish_at="",
            selected_list_type="permanent",
            all_shops=all_shops,
            current_shop_id=current_shop_id,
        )
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


@pricelist_bp.route("/pricelists/<int:pricelist_id>/edit", methods=["GET", "POST"])
@login_required
def pricelist_edit(pricelist_id):
    """価格表の編集"""
    session_db = SessionLocal()
    try:
        pl = (
            session_db.query(PriceList)
            .filter(PriceList.id == pricelist_id, PriceList.user_id == current_user.id)
            .first()
        )
        if not pl:
            return redirect(url_for("pricelist.pricelist_list"))

        if request.method == "POST":
            selected_name = (request.form.get("name") or "").strip()
            selected_notes = _sanitize_notes(request.form.get("notes", "").strip())
            selected_currency_rate = (
                request.form.get("currency_rate", str(pl.currency_rate or 150))
                or ""
            ).strip()
            currency_rate, currency_error = _parse_currency_rate(
                selected_currency_rate
            )
            selected_shop_id = (request.form.get("shop_id") or "").strip()
            shop, shop_error = _resolve_owned_shop(session_db, selected_shop_id)
            selected_unpublish_at = (request.form.get("unpublish_at") or "").strip()
            unpublish_at, unpublish_error = _parse_unpublish_at_jst(selected_unpublish_at)
            selected_list_type = _normalize_list_type(request.form.get("list_type"))
            validation_error = (
                "名前を入力してください"
                if not selected_name
                else shop_error or currency_error or unpublish_error
            )
            if validation_error:
                all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
                current_shop_id = session.get('current_shop_id')
                response = render_template(
                    "pricelist_edit.html",
                    pricelist=pl,
                    error=validation_error,
                    selected_layout=_normalize_layout(request.form.get("layout")),
                    selected_theme=_normalize_theme(request.form.get("theme")),
                    selected_shop_id=selected_shop_id,
                    selected_unpublish_at=selected_unpublish_at,
                    selected_list_type=selected_list_type,
                    selected_name=selected_name,
                    selected_notes=selected_notes,
                    selected_currency_rate=selected_currency_rate,
                    selected_is_active="is_active" in request.form,
                    publication_state=pl.publication_state_at(),
                    all_shops=all_shops,
                    current_shop_id=current_shop_id,
                )
                return response, 400

            pl.name = selected_name
            pl.notes = selected_notes
            pl.currency_rate = currency_rate
            pl.layout = _normalize_layout(request.form.get("layout"))
            pl.theme = _normalize_theme(request.form.get("theme"))
            pl.shop_id = shop.id if shop else None
            pl.is_active = "is_active" in request.form
            pl.list_type = selected_list_type
            pl.unpublish_at = unpublish_at
            pl.updated_at = utc_now()
            session_db.commit()
            return redirect(url_for("pricelist.pricelist_list"))

        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')
        return render_template(
            "pricelist_edit.html",
            pricelist=pl,
            selected_layout=pl.layout or "grid",
            selected_theme=pl.theme or "dark",
            selected_shop_id=str(pl.shop_id) if pl.shop_id else "",
            selected_unpublish_at=_format_unpublish_at_jst(pl.unpublish_at),
            selected_list_type=_normalize_list_type(pl.list_type),
            selected_is_active=pl.is_active,
            publication_state=pl.publication_state_at(),
            all_shops=all_shops,
            current_shop_id=current_shop_id,
        )
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


@pricelist_bp.route("/pricelists/<int:pricelist_id>/items", methods=["GET", "POST"])
@login_required
def pricelist_items(pricelist_id):
    """価格表の商品管理"""
    session_db = SessionLocal()
    try:
        pl = (
            session_db.query(PriceList)
            .filter(PriceList.id == pricelist_id, PriceList.user_id == current_user.id)
            .first()
        )
        if not pl:
            return redirect(url_for("pricelist.pricelist_list"))

        items = (
            session_db.query(PriceListItem)
            .filter(PriceListItem.price_list_id == pl.id)
            .join(Product)
            .options(subqueryload(PriceListItem.product).subqueryload(Product.snapshots))
            .options(subqueryload(PriceListItem.product).subqueryload(Product.variants))
            .order_by(PriceListItem.sort_order)
            .all()
        )
        form_error = None
        submitted_custom_prices = {}

        if request.method == "POST":
            action = request.form.get("action")

            if action == "update_items":
                pending_updates = []
                for item in items:
                    raw_custom_price = request.form.get(
                        f"custom_price_{item.id}",
                        "",
                    ).strip()
                    submitted_custom_prices[item.id] = raw_custom_price
                    custom_price, custom_price_error = _parse_optional_custom_price(
                        raw_custom_price
                    )
                    if custom_price_error and form_error is None:
                        form_error = custom_price_error
                    pending_updates.append(
                        (
                            item,
                            f"visible_{item.id}" in request.form,
                            custom_price,
                        )
                    )

                if form_error is None:
                    for item, visible, custom_price in pending_updates:
                        item.visible = visible
                        item.custom_price = custom_price
                    pl.updated_at = utc_now()
                    session_db.commit()

            elif action == "remove_items":
                raw_item_ids = request.form.getlist("remove_ids")
                if any(not str(item_id).isdigit() for item_id in raw_item_ids):
                    form_error = "削除する商品を選び直してください"
                elif raw_item_ids:
                    session_db.query(PriceListItem).filter(
                        PriceListItem.id.in_([int(i) for i in raw_item_ids]),
                        PriceListItem.price_list_id == pl.id,
                    ).delete(synchronize_session=False)
                    pl.updated_at = utc_now()
                    session_db.commit()

            if form_error is None:
                return redirect(url_for("pricelist.pricelist_items", pricelist_id=pl.id))

        # Process items for display
        for item in items:
            p = item.product
            snapshot = (
                sorted(p.snapshots, key=lambda s: s.scraped_at, reverse=True)[0]
                if p.snapshots else None
            )
            image_urls = split_image_url_string(snapshot.image_urls if snapshot else None)
            item.thumb_url = image_urls[0] if image_urls else ""
            item.display_title = p.custom_title or p.last_title or "(タイトルなし)"
            item.display_price = (
                item.custom_price
                if item.custom_price is not None
                else resolve_product_display_price(p, p.variants)
            )
            item.total_stock = sum(v.inventory_qty or 0 for v in p.variants)

        # Selling below cost is the one mistake worth surfacing above the table.
        loss_count = sum(
            1
            for item in items
            if item.display_price is not None
            and item.product.last_price is not None
            and item.display_price < item.product.last_price
        )

        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')

        return render_template(
            "pricelist_items.html",
            pricelist=pl,
            items=items,
            loss_count=loss_count,
            error=form_error,
            submitted_custom_prices=submitted_custom_prices,
            all_shops=all_shops,
            current_shop_id=current_shop_id,
        ), (400 if form_error else 200)
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


@pricelist_bp.route("/pricelists/<int:pricelist_id>/add-products", methods=["POST"])
@login_required
def pricelist_add_products(pricelist_id):
    """商品を価格表に一括追加"""
    session_db = SessionLocal()
    try:
        pl = (
            session_db.query(PriceList)
            .filter(PriceList.id == pricelist_id, PriceList.user_id == current_user.id)
            .first()
        )
        if not pl:
            return redirect(url_for("pricelist.pricelist_list"))

        product_ids = request.form.getlist("product_ids")
        if not product_ids:
            return redirect(url_for("pricelist.pricelist_items", pricelist_id=pl.id))

        # Get existing product IDs in this price list
        existing_product_ids = set(
            row[0] for row in
            session_db.query(PriceListItem.product_id)
            .filter(PriceListItem.price_list_id == pl.id)
            .all()
        )

        # Get max sort_order
        max_order = (
            session_db.query(PriceListItem.sort_order)
            .filter(PriceListItem.price_list_id == pl.id)
            .order_by(PriceListItem.sort_order.desc())
            .first()
        )
        next_order = (max_order[0] + 1) if max_order else 0

        added = 0
        for pid in product_ids:
            try:
                pid_int = int(pid)
            except (TypeError, ValueError):
                continue
            if pid_int not in existing_product_ids:
                # Verify ownership
                product = session_db.query(Product).filter(
                    Product.id == pid_int,
                    Product.user_id == current_user.id,
                ).first()
                if product:
                    item = PriceListItem(
                        price_list_id=pl.id,
                        product_id=pid_int,
                        sort_order=next_order,
                    )
                    session_db.add(item)
                    next_order += 1
                    added += 1

        if added > 0:
            pl.updated_at = utc_now()
            session_db.commit()

        return redirect(url_for("pricelist.pricelist_items", pricelist_id=pl.id))
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


@pricelist_bp.route("/pricelists/<int:pricelist_id>/add-products-page")
@login_required
def pricelist_add_products_page(pricelist_id):
    """商品追加ページ（商品一覧から選択）"""
    session_db = SessionLocal()
    try:
        pl = (
            session_db.query(PriceList)
            .filter(PriceList.id == pricelist_id, PriceList.user_id == current_user.id)
            .first()
        )
        if not pl:
            return redirect(url_for("pricelist.pricelist_list"))

        # Get existing product IDs
        existing_ids = set(
            row[0] for row in
            session_db.query(PriceListItem.product_id)
            .filter(PriceListItem.price_list_id == pl.id)
            .all()
        )

        # Get available products (not already in list, not archived/deleted)
        search = request.args.get("search", "").strip()
        query = (
            session_db.query(Product)
            .filter(
                Product.user_id == current_user.id,
                Product.archived != True,
                Product.deleted_at == None,
            )
            .options(subqueryload(Product.snapshots))
        )
        if search:
            query = query.filter(
                (Product.last_title.ilike(f"%{search}%")) |
                (Product.custom_title.ilike(f"%{search}%"))
            )

        products = query.order_by(Product.updated_at.desc()).limit(100).all()

        # Attach thumbnail and mark already-added
        for p in products:
            snapshot = (
                sorted(p.snapshots, key=lambda s: s.scraped_at, reverse=True)[0]
                if p.snapshots else None
            )
            image_urls = split_image_url_string(snapshot.image_urls if snapshot else None)
            p.thumb_url = image_urls[0] if image_urls else ""
            p.already_added = p.id in existing_ids

        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')

        return render_template(
            "pricelist_add_products.html",
            pricelist=pl,
            products=products,
            search=search,
            all_shops=all_shops,
            current_shop_id=current_shop_id,
        )
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


@pricelist_bp.route("/pricelists/<int:pricelist_id>/delete", methods=["POST"])
@login_required
def pricelist_delete(pricelist_id):
    """価格表の削除"""
    session_db = SessionLocal()
    try:
        pl = (
            session_db.query(PriceList)
            .filter(PriceList.id == pricelist_id, PriceList.user_id == current_user.id)
            .first()
        )
        if pl:
            session_db.delete(pl)
            session_db.commit()
        return redirect(url_for("pricelist.pricelist_list"))
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()
