"""
Price List management routes: create, edit, delete, manage items.
"""
import uuid
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import login_required, current_user
from sqlalchemy.orm import subqueryload

from database import SessionLocal
from models import Shop, Product, ProductSnapshot, Variant, PriceList, PriceListItem

pricelist_bp = Blueprint('pricelist', __name__)

PRICE_LIST_LAYOUTS = {"grid", "editorial"}


def _normalize_layout(value):
    layout = (value or "").strip().lower()
    if layout in PRICE_LIST_LAYOUTS:
        return layout
    return "grid"


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
        for pl in pricelists:
            pl.item_count = (
                session_db.query(PriceListItem)
                .filter(PriceListItem.price_list_id == pl.id)
                .count()
            )

        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')

        return render_template(
            "pricelist_list.html",
            pricelists=pricelists,
            all_shops=all_shops,
            current_shop_id=current_shop_id,
        )
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
            notes = request.form.get("notes", "").strip()
            currency_rate = int(request.form.get("currency_rate", 150))
            layout = _normalize_layout(request.form.get("layout"))

            if not name:
                all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
                current_shop_id = session.get('current_shop_id')
                return render_template(
                    "pricelist_edit.html",
                    pricelist=None,
                    error="名前を入力してください",
                    selected_layout=layout,
                    all_shops=all_shops,
                    current_shop_id=current_shop_id,
                )

            new_pl = PriceList(
                user_id=current_user.id,
                name=name,
                token=str(uuid.uuid4()),
                notes=notes,
                currency_rate=currency_rate,
                layout=layout,
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
            all_shops=all_shops,
            current_shop_id=current_shop_id,
        )
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
            pl.name = request.form.get("name", pl.name).strip()
            pl.notes = request.form.get("notes", "").strip()
            pl.currency_rate = int(request.form.get("currency_rate", 150))
            pl.layout = _normalize_layout(request.form.get("layout"))
            pl.is_active = "is_active" in request.form
            pl.updated_at = datetime.utcnow()
            session_db.commit()
            return redirect(url_for("pricelist.pricelist_list"))

        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')
        return render_template(
            "pricelist_edit.html",
            pricelist=pl,
            selected_layout=pl.layout or "grid",
            all_shops=all_shops,
            current_shop_id=current_shop_id,
        )
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

        if request.method == "POST":
            action = request.form.get("action")

            if action == "update_items":
                # Update visibility and custom prices for existing items
                for item in pl.items:
                    item.visible = f"visible_{item.id}" in request.form
                    custom_price = request.form.get(f"custom_price_{item.id}", "").strip()
                    item.custom_price = int(custom_price) if custom_price else None

                pl.updated_at = datetime.utcnow()
                session_db.commit()

            elif action == "remove_items":
                item_ids = request.form.getlist("remove_ids")
                if item_ids:
                    session_db.query(PriceListItem).filter(
                        PriceListItem.id.in_([int(i) for i in item_ids]),
                        PriceListItem.price_list_id == pl.id,
                    ).delete(synchronize_session=False)
                    pl.updated_at = datetime.utcnow()
                    session_db.commit()

            return redirect(url_for("pricelist.pricelist_items", pricelist_id=pl.id))

        # Get items with product data
        items = (
            session_db.query(PriceListItem)
            .filter(PriceListItem.price_list_id == pl.id)
            .join(Product)
            .options(subqueryload(PriceListItem.product).subqueryload(Product.snapshots))
            .options(subqueryload(PriceListItem.product).subqueryload(Product.variants))
            .order_by(PriceListItem.sort_order)
            .all()
        )

        # Process items for display
        for item in items:
            p = item.product
            snapshot = (
                sorted(p.snapshots, key=lambda s: s.scraped_at, reverse=True)[0]
                if p.snapshots else None
            )
            item.thumb_url = (
                snapshot.image_urls.split("|")[0]
                if snapshot and snapshot.image_urls else ""
            )
            item.display_title = p.custom_title or p.last_title or "(タイトルなし)"
            item.display_price = item.custom_price or p.selling_price or p.last_price
            item.total_stock = sum(v.inventory_qty or 0 for v in p.variants)

        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')

        return render_template(
            "pricelist_items.html",
            pricelist=pl,
            items=items,
            all_shops=all_shops,
            current_shop_id=current_shop_id,
        )
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
            pid_int = int(pid)
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
            pl.updated_at = datetime.utcnow()
            session_db.commit()

        return redirect(url_for("pricelist.pricelist_items", pricelist_id=pl.id))
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
            p.thumb_url = (
                snapshot.image_urls.split("|")[0]
                if snapshot and snapshot.image_urls else ""
            )
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
    finally:
        session_db.close()
