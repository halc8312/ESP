"""
Shop and template management routes.
"""
from flask import Blueprint, render_template, request, redirect, url_for, session
from flask_login import login_required, current_user

from database import SessionLocal
from models import Shop, Product, DescriptionTemplate

shops_bp = Blueprint('shops', __name__)


@shops_bp.route("/shops", methods=["GET", "POST"])
@login_required
def manage_shops():
    session_db = SessionLocal()
    try:
        if request.method == "POST":
            name = request.form.get("name")
            if name:
                # Check duplication for this user
                existing = session_db.query(Shop).filter_by(user_id=current_user.id, name=name).first()
                if not existing:
                    new_shop = Shop(name=name, user_id=current_user.id)
                    session_db.add(new_shop)
                    try:
                        session_db.commit()
                    except Exception:
                        session_db.rollback()
            return redirect(url_for('shops.manage_shops'))

        # Filter by user
        shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        shop_data = []
        for s in shops:
            count = session_db.query(Product).filter_by(shop_id=s.id).count()
            s.product_count = count
            shop_data.append(s)

        current_shop_id = session.get('current_shop_id')
        
        return render_template(
            "shops.html",
            shops=shop_data,
            all_shops=shops,
            current_shop_id=current_shop_id
        )
    finally:
        session_db.close()


@shops_bp.route("/shops/<int:shop_id>/delete", methods=["POST"])
@login_required
def delete_shop(shop_id):
    session_db = SessionLocal()
    try:
        # User constraint
        shop = session_db.query(Shop).filter_by(id=shop_id, user_id=current_user.id).one_or_none()
        if shop:
            products = session_db.query(Product).filter_by(shop_id=shop_id).all()
            for p in products:
                p.shop_id = None
            session_db.delete(shop)
            session_db.commit()
            if session.get('current_shop_id') == shop_id:
                session.pop('current_shop_id', None)
    finally:
        session_db.close()
    return redirect(url_for('shops.manage_shops'))


@shops_bp.route("/set_current_shop", methods=["POST"])
@login_required
def set_current_shop():
    shop_id = request.form.get("shop_id")
    # Verify ownership before setting session
    if shop_id:
        session_db = SessionLocal()
        try:
            shop = session_db.query(Shop).filter_by(id=shop_id, user_id=current_user.id).first()
            if shop:
                session['current_shop_id'] = int(shop_id)
        finally:
            session_db.close()
    else:
        session.pop('current_shop_id', None)
    return redirect(request.referrer or url_for('main.index'))


@shops_bp.route("/templates", methods=["GET", "POST"])
@login_required
def manage_templates():
    session_db = SessionLocal()
    try:
        if request.method == "POST":
            name = request.form.get("name")
            content = request.form.get("content")
            if name and content:
                new_template = DescriptionTemplate(name=name, content=content)
                session_db.add(new_template)
                session_db.commit()
            return redirect(url_for('shops.manage_templates'))

        templates = session_db.query(DescriptionTemplate).order_by(DescriptionTemplate.id).all()
        # Only show user's shops for context if needed, but template is global for now? 
        # Requirement was Shop/Product isolation. Let's filter Shop list in dropdown.
        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')

        return render_template(
            "manage_templates.html",
            templates=templates,
            all_shops=all_shops,
            current_shop_id=current_shop_id
        )
    finally:
        session_db.close()


@shops_bp.route("/templates/<int:template_id>/delete", methods=["POST"])
@login_required
def delete_template(template_id):
    session_db = SessionLocal()
    try:
        template = session_db.query(DescriptionTemplate).filter_by(id=template_id).one_or_none()
        if template:
            session_db.delete(template)
            session_db.commit()
    finally:
        session_db.close()
    return redirect(url_for('shops.manage_templates'))
