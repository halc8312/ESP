"""
Product detail routes.
"""
import json
import os
import uuid
from flask import Blueprint, render_template, request, redirect, url_for, session
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from database import SessionLocal
from models import Shop, Product, Variant, ProductSnapshot, DescriptionTemplate
from services.image_service import IMAGE_STORAGE_PATH
from services.rich_text import normalize_rich_text
from time_utils import utc_now

products_bp = Blueprint('products', __name__)

PRODUCT_IMAGE_SUBDIR = "product_images"
PRODUCT_IMAGE_URL_PREFIX = f"/media/{PRODUCT_IMAGE_SUBDIR}/"
ALLOWED_PRODUCT_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _latest_snapshot_for_product(session_db, product_id):
    return (
        session_db.query(ProductSnapshot)
        .filter_by(product_id=product_id)
        .order_by(ProductSnapshot.scraped_at.desc())
        .first()
    )


def _split_snapshot_images(snapshot):
    if not snapshot or not snapshot.image_urls:
        return []
    return [url.strip() for url in snapshot.image_urls.split("|") if url.strip()]


def _is_allowed_image_url(url):
    lower_url = url.lower()
    return lower_url.startswith("http://") or lower_url.startswith("https://") or url.startswith("/")


def _product_image_upload_dir():
    path = os.path.join(IMAGE_STORAGE_PATH, PRODUCT_IMAGE_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def _save_uploaded_product_image(file_storage, *, product_id):
    if not file_storage or not file_storage.filename:
        return None, None

    safe_name = secure_filename(file_storage.filename)
    ext = os.path.splitext(safe_name)[1].lower()
    if ext not in ALLOWED_PRODUCT_IMAGE_EXTENSIONS:
        return None, "商品画像は PNG / JPG / GIF / WEBP のみアップロードできます"

    content_type = (file_storage.mimetype or "").lower()
    if content_type and not content_type.startswith("image/"):
        return None, "画像ファイルのみアップロードできます"

    filename = f"product_{current_user.id}_{product_id}_{uuid.uuid4().hex}{ext}"
    upload_path = os.path.join(_product_image_upload_dir(), filename)
    file_storage.save(upload_path)
    return f"{PRODUCT_IMAGE_URL_PREFIX}{filename}", None


def _managed_product_image_path(image_url):
    if not image_url or not image_url.startswith(PRODUCT_IMAGE_URL_PREFIX):
        return None

    relative_path = image_url[len("/media/"):]
    candidate = os.path.abspath(os.path.join(IMAGE_STORAGE_PATH, relative_path))
    storage_root = os.path.abspath(IMAGE_STORAGE_PATH)

    try:
        if os.path.commonpath([candidate, storage_root]) != storage_root:
            return None
    except ValueError:
        return None

    return candidate


def _remove_managed_product_image_file(image_url):
    path = _managed_product_image_path(image_url)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _parse_image_urls_json(raw_value, fallback_urls):
    if not raw_value:
        return list(fallback_urls)

    try:
        payload = json.loads(raw_value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return list(fallback_urls)

    if not isinstance(payload, list):
        return list(fallback_urls)

    normalized_urls = []
    seen_urls = set()

    for item in payload:
        if not isinstance(item, str):
            continue

        candidate = item.strip()
        if not candidate or candidate in seen_urls:
            continue
        if not _is_allowed_image_url(candidate):
            continue

        normalized_urls.append(candidate)
        seen_urls.add(candidate)

    return normalized_urls


def _build_image_snapshot(product, base_snapshot, image_urls):
    if not base_snapshot and not image_urls:
        return None

    return ProductSnapshot(
        product_id=product.id,
        scraped_at=utc_now(),
        title=(
            base_snapshot.title
            if base_snapshot and base_snapshot.title
            else (product.custom_title or product.last_title)
        ),
        price=(
            base_snapshot.price
            if base_snapshot and base_snapshot.price is not None
            else product.last_price
        ),
        status=(
            base_snapshot.status
            if base_snapshot and base_snapshot.status
            else product.last_status
        ),
        description=(
            base_snapshot.description
            if base_snapshot and base_snapshot.description
            else (product.custom_description or "")
        ),
        image_urls="|".join(image_urls),
    )


def _render_product_detail(session_db, product, snapshot, images, *, error=None, status_code=200):
    from services.translator import compute_source_hash

    templates = session_db.query(DescriptionTemplate).order_by(DescriptionTemplate.name).all()
    all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
    current_shop_id = session.get('current_shop_id')
    variants = session_db.query(Variant).filter_by(product_id=product.id).order_by(Variant.position).all()

    current_title_hash = compute_source_hash(product.custom_title or product.last_title or "")
    current_description_hash = compute_source_hash(
        product.custom_description
        or (snapshot.description if snapshot is not None else "")
        or ""
    )
    translation_state = {
        "title_stale": bool(
            product.custom_title_en
            and product.custom_title_en_source_hash
            and product.custom_title_en_source_hash != current_title_hash
        ),
        "description_stale": bool(
            product.custom_description_en
            and product.custom_description_en_source_hash
            and product.custom_description_en_source_hash != current_description_hash
        ),
        "title_has_translation": bool(product.custom_title_en),
        "description_has_translation": bool(product.custom_description_en),
    }

    return render_template(
        "product_detail.html",
        product=product,
        snapshot=snapshot,
        images=images,
        templates=templates,
        all_shops=all_shops,
        current_shop_id=current_shop_id,
        variants=variants,
        error=error,
        translation_state=translation_state,
    ), status_code


@products_bp.route("/product/<int:product_id>", methods=["GET", "POST"])
@login_required
def product_detail(product_id):
    session_db = SessionLocal()
    uploaded_image_urls = []
    try:
        # User constraint
        product = session_db.query(Product).filter_by(id=product_id, user_id=current_user.id).one_or_none()
        if not product:
            return "Product not found or access denied", 404

        snapshot = _latest_snapshot_for_product(session_db, product.id)
        current_images = _split_snapshot_images(snapshot)

        if request.method == "POST":
            # --- 所属ショップ ---
            shop_id_str = request.form.get("shop_id")
            if shop_id_str:
                # Verify shop ownership
                s = session_db.query(Shop).filter_by(id=int(shop_id_str), user_id=current_user.id).first()
                product.shop_id = s.id if s else None
            else:
                product.shop_id = None

            # --- 基本情報 (Product) ---
            product.custom_title = request.form.get("title")
            product.custom_description = normalize_rich_text(request.form.get("description")) or None
            product.custom_title_en = request.form.get("title_en")
            product.custom_description_en = normalize_rich_text(request.form.get("description_en")) or None
            product.status = request.form.get("status")

            # --- オプション名 (Product) ---
            product.option1_name = request.form.get("option1_name")
            product.option2_name = request.form.get("option2_name")
            product.option3_name = request.form.get("option3_name")

            # --- 分類 (Product) ---
            product.custom_vendor = request.form.get("vendor")
            product.tags = request.form.get("tags")

            # --- SEO (Product) ---
            product.custom_handle = request.form.get("handle")
            product.seo_title = request.form.get("seo_title")
            product.seo_description = request.form.get("seo_description")

            submitted_images = _parse_image_urls_json(
                request.form.get("image_urls_json"),
                current_images,
            )
            upload_error = None
            for file_storage in request.files.getlist("image_files"):
                uploaded_image_url, upload_error = _save_uploaded_product_image(
                    file_storage,
                    product_id=product.id,
                )
                if upload_error:
                    break
                if uploaded_image_url:
                    uploaded_image_urls.append(uploaded_image_url)

            if upload_error:
                for image_url in uploaded_image_urls:
                    _remove_managed_product_image_file(image_url)
                uploaded_image_urls = []
                return _render_product_detail(
                    session_db,
                    product,
                    snapshot,
                    current_images,
                    error=upload_error,
                    status_code=400,
                )

            for uploaded_image_url in uploaded_image_urls:
                if uploaded_image_url not in submitted_images:
                    submitted_images.append(uploaded_image_url)

            if submitted_images != current_images:
                next_snapshot = _build_image_snapshot(product, snapshot, submitted_images)
                if next_snapshot is not None:
                    session_db.add(next_snapshot)
                    snapshot = next_snapshot
            
            # --- バリエーション削除 ---
            delete_ids_str = request.form.get("delete_v_ids", "")
            if delete_ids_str:
                for del_id in delete_ids_str.split(","):
                    if del_id.isdigit():
                        v_to_del = session_db.query(Variant).filter_by(id=int(del_id), product_id=product.id).first()
                        if v_to_del:
                            session_db.delete(v_to_del)

            # --- バリエーション更新 (既存) ---
            v_ids = request.form.getlist("v_ids")
            for v_id_str in v_ids:
                try:
                    v_id = int(v_id_str)
                    variant = session_db.query(Variant).filter_by(id=v_id, product_id=product.id).first()
                    if variant:
                        variant.option1_value = request.form.get(f"v_opt1_{v_id}")
                        variant.option2_value = request.form.get(f"v_opt2_{v_id}")
                        
                        p_val = request.form.get(f"v_price_{v_id}")
                        variant.price = int(p_val) if p_val and p_val.isdigit() else None
                        
                        variant.sku = request.form.get(f"v_sku_{v_id}")
                        
                        q_val = request.form.get(f"v_qty_{v_id}")
                        variant.inventory_qty = int(q_val) if q_val and q_val.isdigit() else 0
                        
                        g_val = request.form.get(f"v_grams_{v_id}")
                        variant.grams = int(g_val) if g_val and g_val.isdigit() else None
                        
                        variant.taxable = (request.form.get(f"v_tax_{v_id}") == 'on')
                        variant.hs_code = request.form.get(f"v_hs_{v_id}")
                        variant.country_of_origin = request.form.get(f"v_org_{v_id}")
                except ValueError:
                    continue

            # --- バリエーション新規作成 ---
            new_indices = request.form.getlist("new_v_indices")
            for idx in new_indices:
                try:
                    new_variant = Variant(
                        product_id=product.id,
                        option1_value=request.form.get(f"new_v_opt1_{idx}"),
                        option2_value=request.form.get(f"new_v_opt2_{idx}"),
                        option3_value=request.form.get(f"new_v_opt3_{idx}"),
                        sku=request.form.get(f"new_v_sku_{idx}"),
                        hs_code=request.form.get(f"new_v_hs_{idx}"),
                        country_of_origin=request.form.get(f"new_v_org_{idx}"),
                        taxable=(request.form.get(f"new_v_tax_{idx}") == 'on')
                    )
                    
                    p_val = request.form.get(f"new_v_price_{idx}")
                    if p_val and p_val.isdigit():
                        new_variant.price = int(p_val)
                        
                    q_val = request.form.get(f"new_v_qty_{idx}")
                    if q_val and q_val.isdigit():
                        new_variant.inventory_qty = int(q_val)
                    else:
                        new_variant.inventory_qty = 0
                        
                    g_val = request.form.get(f"new_v_grams_{idx}")
                    if g_val and g_val.isdigit():
                        new_variant.grams = int(g_val)
                    
                    session_db.add(new_variant)
                except Exception as e:
                    print(f"Error adding variant {idx}: {e}")
                    continue

            # --- 販売価格の同期 ---
            # バリエーションの価格をProduct.selling_priceに反映
            # （商品一覧やカタログで表示される価格）
            all_variants = session_db.query(Variant).filter_by(product_id=product.id).all()
            if all_variants:
                # 最初のバリエーションの価格を代表として使用
                primary_variant = all_variants[0]
                if primary_variant.price is not None:
                    product.selling_price = primary_variant.price

            product.updated_at = utc_now()
            session_db.commit()
            uploaded_image_urls = []
            return redirect(url_for('products.product_detail', product_id=product.id))

        images = _split_snapshot_images(snapshot)
        return _render_product_detail(session_db, product, snapshot, images)
    except Exception:
        session_db.rollback()
        for image_url in uploaded_image_urls:
            _remove_managed_product_image_file(image_url)
        raise
    finally:
        session_db.close()
