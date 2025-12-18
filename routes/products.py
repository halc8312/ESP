"""
Product detail routes.
"""
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, session
from flask_login import login_required, current_user

from database import SessionLocal
from models import Shop, Product, Variant, ProductSnapshot, DescriptionTemplate

products_bp = Blueprint('products', __name__)


@products_bp.route("/product/<int:product_id>", methods=["GET", "POST"])
@login_required
def product_detail(product_id):
    session_db = SessionLocal()
    try:
        # User constraint
        product = session_db.query(Product).filter_by(id=product_id, user_id=current_user.id).one_or_none()
        if not product:
            return "Product not found or access denied", 404

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
            product.custom_description = request.form.get("description")
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

            product.updated_at = datetime.utcnow()
            session_db.commit()
            return redirect(url_for('products.product_detail', product_id=product.id))

        snapshot = (
            session_db.query(ProductSnapshot)
            .filter_by(product_id=product.id)
            .order_by(ProductSnapshot.scraped_at.desc())
            .first()
        )
        templates = session_db.query(DescriptionTemplate).order_by(DescriptionTemplate.name).all()
        
        images = []
        if snapshot and snapshot.image_urls:
            images = snapshot.image_urls.split("|")
            
        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')
        
        variants = session_db.query(Variant).filter_by(product_id=product.id).order_by(Variant.position).all()

        return render_template(
            "product_detail.html", 
            product=product, 
            snapshot=snapshot, 
            images=images, 
            templates=templates,
            all_shops=all_shops,
            current_shop_id=current_shop_id,
            variants=variants
        )
    finally:
        session_db.close()
