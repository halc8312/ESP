"""
Shop and template management routes.
"""
import os
import uuid

from flask import Blueprint, render_template, request, redirect, url_for, session
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from database import SessionLocal
from models import Shop, Product, DescriptionTemplate
from services.image_service import IMAGE_STORAGE_PATH
from services.rich_text import normalize_rich_text

shops_bp = Blueprint('shops', __name__)

SHOP_LOGO_SUBDIR = "shop_logos"
SHOP_LOGO_URL_PREFIX = f"/media/{SHOP_LOGO_SUBDIR}/"
ALLOWED_LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


def _logo_upload_dir():
    path = os.path.join(IMAGE_STORAGE_PATH, SHOP_LOGO_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def _build_manage_shops_context(session_db, error=None, edit_error_shop_id=None):
    shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
    for shop in shops:
        shop.product_count = session_db.query(Product).filter_by(shop_id=shop.id).count()

    current_shop_id = session.get('current_shop_id')
    return {
        "shops": shops,
        "all_shops": shops,
        "current_shop_id": current_shop_id,
        "error": error,
        "edit_error_shop_id": edit_error_shop_id,
    }


def _render_manage_shops(session_db, error=None, edit_error_shop_id=None):
    return render_template(
        "shops.html",
        **_build_manage_shops_context(
            session_db,
            error=error,
            edit_error_shop_id=edit_error_shop_id,
        ),
    )


def _save_uploaded_logo(file_storage):
    if not file_storage or not file_storage.filename:
        return None, None

    safe_name = secure_filename(file_storage.filename)
    ext = os.path.splitext(safe_name)[1].lower()
    if ext not in ALLOWED_LOGO_EXTENSIONS:
        return None, "ロゴ画像は PNG / JPG / GIF / WEBP / SVG のみアップロードできます"

    content_type = (file_storage.mimetype or "").lower()
    if content_type and not content_type.startswith("image/"):
        return None, "画像ファイルのみアップロードできます"

    filename = f"shop_logo_{current_user.id}_{uuid.uuid4().hex}{ext}"
    upload_path = os.path.join(_logo_upload_dir(), filename)
    file_storage.save(upload_path)
    return f"{SHOP_LOGO_URL_PREFIX}{filename}", None


def _managed_logo_path(logo_url):
    if not logo_url or not logo_url.startswith(SHOP_LOGO_URL_PREFIX):
        return None

    relative_path = logo_url[len("/media/"):]
    candidate = os.path.abspath(os.path.join(IMAGE_STORAGE_PATH, relative_path))
    storage_root = os.path.abspath(IMAGE_STORAGE_PATH)

    try:
        if os.path.commonpath([candidate, storage_root]) != storage_root:
            return None
    except ValueError:
        return None

    return candidate


def _remove_managed_logo_file(logo_url):
    path = _managed_logo_path(logo_url)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@shops_bp.route("/shops", methods=["GET", "POST"])
@login_required
def manage_shops():
    session_db = SessionLocal()
    try:
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            logo_url = request.form.get("logo_url", "").strip()
            logo_file = request.files.get("logo_file")

            if not name:
                return _render_manage_shops(session_db, error="ショップ名を入力してください")

            existing = session_db.query(Shop).filter_by(user_id=current_user.id, name=name).first()
            if existing:
                return _render_manage_shops(session_db, error="同じショップ名が既に登録されています")

            uploaded_logo_url, upload_error = _save_uploaded_logo(logo_file)
            if upload_error:
                return _render_manage_shops(session_db, error=upload_error)

            saved_logo_url = uploaded_logo_url or logo_url
            new_shop = Shop(name=name, logo_url=saved_logo_url, user_id=current_user.id)
            session_db.add(new_shop)
            try:
                session_db.commit()
            except Exception:
                session_db.rollback()
                if uploaded_logo_url:
                    _remove_managed_logo_file(uploaded_logo_url)
                return _render_manage_shops(session_db, error="ショップの保存に失敗しました")

            return redirect(url_for('shops.manage_shops'))

        return _render_manage_shops(session_db)
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
            logo_url_to_remove = shop.logo_url
            has_shared_logo = False
            if logo_url_to_remove:
                has_shared_logo = session_db.query(Shop).filter(
                    Shop.user_id == current_user.id,
                    Shop.logo_url == logo_url_to_remove,
                    Shop.id != shop.id,
                ).count() > 0
            products = session_db.query(Product).filter_by(shop_id=shop_id).all()
            for p in products:
                p.shop_id = None
            session_db.delete(shop)
            session_db.commit()
            if logo_url_to_remove and not has_shared_logo:
                _remove_managed_logo_file(logo_url_to_remove)
            if session.get('current_shop_id') == shop_id:
                session.pop('current_shop_id', None)
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()
    return redirect(url_for('shops.manage_shops'))


@shops_bp.route("/shops/<int:shop_id>/edit", methods=["POST"])
@login_required
def edit_shop(shop_id):
    session_db = SessionLocal()
    try:
        shop = session_db.query(Shop).filter_by(id=shop_id, user_id=current_user.id).one_or_none()
        if shop:
            name = (request.form.get("name") or "").strip()
            logo_url = request.form.get("logo_url", "").strip()
            logo_file = request.files.get("logo_file")

            if not name:
                return _render_manage_shops(
                    session_db,
                    error="ショップ名を入力してください",
                    edit_error_shop_id=shop.id,
                )

            duplicate = session_db.query(Shop).filter(
                Shop.user_id == current_user.id,
                Shop.name == name,
                Shop.id != shop.id,
            ).first()
            if duplicate:
                return _render_manage_shops(
                    session_db,
                    error="同じショップ名が既に登録されています",
                    edit_error_shop_id=shop.id,
                )

            uploaded_logo_url, upload_error = _save_uploaded_logo(logo_file)
            if upload_error:
                return _render_manage_shops(
                    session_db,
                    error=upload_error,
                    edit_error_shop_id=shop.id,
                )

            previous_logo_url = shop.logo_url
            next_logo_url = uploaded_logo_url or logo_url
            remove_previous_logo = (
                previous_logo_url
                and previous_logo_url != next_logo_url
                and session_db.query(Shop).filter(
                    Shop.user_id == current_user.id,
                    Shop.logo_url == previous_logo_url,
                    Shop.id != shop.id,
                ).count() == 0
            )

            shop.name = name
            shop.logo_url = next_logo_url

            try:
                session_db.commit()
            except Exception:
                session_db.rollback()
                if uploaded_logo_url:
                    _remove_managed_logo_file(uploaded_logo_url)
                return _render_manage_shops(
                    session_db,
                    error="ショップ情報の更新に失敗しました",
                    edit_error_shop_id=shop.id,
                )

            if remove_previous_logo:
                _remove_managed_logo_file(previous_logo_url)
    except Exception:
        session_db.rollback()
        raise
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
        except Exception:
            session_db.rollback()
            raise
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
            content = normalize_rich_text(request.form.get("content"))
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
    except Exception:
        session_db.rollback()
        raise
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
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()
    return redirect(url_for('shops.manage_templates'))
