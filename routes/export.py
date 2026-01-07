"""
CSV Export routes: Shopify, eBay exports.
"""
import csv
import io
from flask import Blueprint, request, make_response, session
from flask_login import login_required, current_user

from database import SessionLocal
from models import Product, Variant, ProductSnapshot
from services.image_service import cache_mercari_image

export_bp = Blueprint('export', __name__)


def _parse_ids_and_params(session_db):
    """
    Export共通のパラメータ解析とProducts取得
    Always filter by current_user.id
    """
    product_ids = request.args.getlist("id", type=int)
    markup = request.args.get("markup", type=float) or 1.0
    qty = request.args.get("qty", type=int) or 1

    query = session_db.query(Product).filter(Product.user_id == current_user.id)
    
    current_shop_id = session.get('current_shop_id')
    if current_shop_id:
        query = query.filter(Product.shop_id == current_shop_id)
        
    if product_ids:
        query = query.filter(Product.id.in_(product_ids))
        
    products = query.all()
    return products, markup, qty


@export_bp.route("/export/shopify")
@login_required
def export_shopify():
    session_db = SessionLocal()
    try:
        products, markup, default_qty = _parse_ids_and_params(session_db)
        if not products:
             return "対象の商品がありません。", 400
        
        output = io.StringIO()
        fieldnames = [
            "Handle", "Title", "Body (HTML)", "Vendor", "Type", "Tags", "Published", 
            "Option1 Name", "Option1 Value",
            "Option2 Name", "Option2 Value",
            "Option3 Name", "Option3 Value",
            "Variant SKU", "Variant Grams", "Variant Inventory Tracker",
            "Variant Inventory Qty", "Variant Inventory Policy", "Variant Fulfillment Service",
            "Variant Price", "Variant Compare At Price", "Variant Requires Shipping", "Variant Taxable",
            "Variant Barcode", "Image Src", "Image Position", "Image Alt Text",
            "Gift Card", "SEO Title", "SEO Description",
            "Google Shopping / Google Product Category", "Google Shopping / Gender", "Google Shopping / Age Group",
            "Google Shopping / MPN", "Google Shopping / AdWords Grouping", "Google Shopping / AdWords Labels",
            "Google Shopping / Condition", "Google Shopping / Custom Product", "Google Shopping / Custom Label 0",
            "Google Shopping / Custom Label 1", "Google Shopping / Custom Label 2", "Google Shopping / Custom Label 3",
            "Google Shopping / Custom Label 4", "Variant Image", "Variant Weight Unit", "Variant Tax Code",
            "Cost per item", "Status"
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()

        for product in products:
            snapshot = (
                session_db.query(ProductSnapshot)
                .filter_by(product_id=product.id)
                .order_by(ProductSnapshot.scraped_at.desc())
                .first()
            )

            title = product.custom_title or product.last_title or ""
            # Description processing remains same
            description = product.custom_description or (snapshot.description if snapshot else "")
            vendor = product.custom_vendor or product.site.capitalize()
            handle = product.custom_handle or f"{product.site or 'product'}-{product.id}"
            
            image_urls = []
            if snapshot and snapshot.image_urls:
                base_url = request.url_root.rstrip('/')
                original_urls = snapshot.image_urls.split("|")
                for i, mercari_url in enumerate(original_urls):
                    local_filename = cache_mercari_image(mercari_url, product.id, i)
                    if local_filename:
                        full_url = f"{base_url}/media/{local_filename}"
                        image_urls.append(full_url)
            
            variants = session_db.query(Variant).filter_by(product_id=product.id).order_by(Variant.position).all()
            if not variants:
                continue

            for i, variant in enumerate(variants):
                row = {f: "" for f in fieldnames} # Initialize with empty strings
                row["Handle"] = handle
                
                # Common fields (Status needed for all rows per feedback, though standard is 1st row. We will put in all if safe, or follow standard strictly. Feedback says "most rows empty" is bad. Let's put Status in all rows to be safe as per feedback.)
                row["Status"] = product.status

                if i == 0:
                    row["Title"] = title
                    row["Body (HTML)"] = description.replace("\n", "<br>")
                    row["Vendor"] = vendor
                    row["Type"] = "Mercari Item" # Default Type
                    row["Published"] = "true" if product.status == 'active' else 'false'
                    row["Tags"] = product.tags or ""
                    row["SEO Title"] = product.seo_title or ""
                    row["SEO Description"] = product.seo_description or ""
                    if image_urls:
                        row["Image Src"] = image_urls[0]
                        row["Image Position"] = 1
                        row["Image Alt Text"] = title
                
                row["Option1 Name"] = product.option1_name or "Title"
                row["Option2 Name"] = product.option2_name or ""
                row["Option3 Name"] = product.option3_name or ""
                
                row["Option1 Value"] = variant.option1_value or ""
                row["Option2 Value"] = variant.option2_value or ""
                row["Option3 Value"] = variant.option3_value or ""
                row["Variant SKU"] = variant.sku or ""
                row["Variant Grams"] = variant.grams or ""
                row["Variant Inventory Tracker"] = "shopify"
                
                if product.last_status == 'sold':
                    final_qty = 0
                else:
                    final_qty = variant.inventory_qty if variant.inventory_qty is not None else default_qty
                    
                row["Variant Inventory Qty"] = final_qty
                row["Variant Inventory Policy"] = "deny"
                row["Variant Fulfillment Service"] = "manual"
                
                base_price = variant.price
                final_price = int(base_price * markup) if base_price is not None else 0
                row["Variant Price"] = final_price
                row["Variant Compare At Price"] = "" # Empty for now
                
                row["Variant Requires Shipping"] = "true"
                row["Variant Taxable"] = "true" if variant.taxable else "false"
                # Removed Country of Origin and HS Code per feedback
                row["Variant Barcode"] = "" # Empty
                
                writer.writerow(row)

            if len(image_urls) > 1:
                for i, img_url in enumerate(image_urls[1:], start=2):
                    img_row = {f: "" for f in fieldnames}
                    img_row["Handle"] = handle
                    img_row["Image Src"] = img_url
                    img_row["Image Position"] = i
                    img_row["Image Alt Text"] = title
                    writer.writerow(img_row)

        output.seek(0)
        response = make_response(output.getvalue())
        response.headers["Content-Disposition"] = "attachment; filename=shopify_products.csv"
        response.headers["Content-type"] = "text/csv"
        return response
    finally:
        session_db.close()


@export_bp.route("/export_ebay")
@login_required
def export_ebay():
    session_db = SessionLocal()
    try:
        products, markup, qty = _parse_ids_and_params(session_db)
        ebay_category_id = request.args.get("ebay_category_id", "").strip()
        ebay_condition_id = request.args.get("ebay_condition_id", "").strip() or "3000"
        paypal_email = request.args.get("ebay_paypal_email", "").strip()
        payment_profile = request.args.get("ebay_payment_profile", "").strip()
        return_profile = request.args.get("ebay_return_profile", "").strip()
        shipping_profile = request.args.get("ebay_shipping_profile", "").strip()

        if not ebay_condition_id.isdigit():
            ebay_condition_id = "3000"

        try:
            exchange_rate = float(request.args.get("rate", "155.0"))
        except ValueError:
            exchange_rate = 155.0

        output = io.StringIO()
        writer = csv.writer(output)

        header = [
            "Action(SiteID=US|Country=JP|Currency=USD|Version=1193|CC=UTF-8)",
            "CustomLabel",
            "StartPrice",
            "ConditionID",
            "Title",
            "Description",
            "PicURL",
            "Category",
            "Format",
            "Duration",
            "Location",
            "ShippingProfileName",
            "ReturnProfileName",
            "PaymentProfileName",
            "C:Brand",
            "C:Card Condition",
        ]
        writer.writerow(header)

        BRAND_DEFAULT = "Unbranded"
        CARD_CONDITION_DEFAULT = "Used"

        for p in products:
            snap = p.snapshots[-1] if p.snapshots else None
            title_src = snap.title if snap and snap.title else (p.last_title or "")
            title = (title_src or "")[:80]
            description_src = snap.description if snap and snap.description else ""
            if not description_src:
                description_src = title_src
            desc_clean = description_src.replace("\r\n", "\n").replace("\r", "\n")
            description_html = desc_clean.replace("\n", "<br>")

            base_price_yen = None
            if snap and snap.price is not None:
                base_price_yen = snap.price
            elif p.last_price is not None:
                base_price_yen = p.last_price

            start_price = ""
            if base_price_yen:
                try:
                    usd_val = (base_price_yen / exchange_rate) * markup
                    start_price = "{:.2f}".format(usd_val)
                except Exception:
                    start_price = ""

            image_urls = []
            if snap and snap.image_urls:
                image_urls = [u for u in snap.image_urls.split("|") if u]
            pic_url = "|".join(image_urls) if image_urls else ""

            custom_label = f"MERCARI-{p.id}"

            row = [
                "Add", custom_label, start_price, ebay_condition_id, title, description_html, pic_url,
                ebay_category_id, "FixedPriceItem", "GTC", "Japan", shipping_profile, return_profile,
                payment_profile, BRAND_DEFAULT, CARD_CONDITION_DEFAULT
            ]
            writer.writerow(row)

        data = "\ufeff" + output.getvalue()
        resp = make_response(data)
        resp.headers["Content-Type"] = "text/csv; charset=utf-8"
        resp.headers["Content-Disposition"] = 'attachment; filename="ebay_export.csv"'
        return resp
    finally:
        session_db.close()


@export_bp.route("/export_stock_update")
@login_required
def export_stock_update():
    session_db = SessionLocal()
    try:
        products, markup, default_qty = _parse_ids_and_params(session_db)
        if not products:
             return "対象の商品がありません。", 400

        output = io.StringIO()
        fieldnames = ["Handle", "Option1 Value", "Option2 Value", "Option3 Value", "Variant Inventory Qty"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()

        for product in products:
            handle = product.custom_handle or f"mercari-{product.id}"
            variants = session_db.query(Variant).filter_by(product_id=product.id).order_by(Variant.position).all()
            
            for variant in variants:
                if product.last_status == 'sold':
                    final_qty = 0
                else:
                    final_qty = variant.inventory_qty if variant.inventory_qty is not None else default_qty
                
                writer.writerow({
                    "Handle": handle,
                    "Option1 Value": variant.option1_value,
                    "Option2 Value": variant.option2_value,
                    "Option3 Value": variant.option3_value,
                    "Variant Inventory Qty": final_qty,
                })

        output.seek(0)
        response = make_response(output.getvalue())
        response.headers["Content-Disposition"] = "attachment; filename=shopify_stock_update.csv"
        response.headers["Content-type"] = "text/csv"
        return response
    finally:
        session_db.close()


@export_bp.route("/export_price_update")
@login_required
def export_price_update():
    session_db = SessionLocal()
    try:
        products, markup, default_qty = _parse_ids_and_params(session_db)
        if not products:
             return "対象の商品がありません。", 400

        output = io.StringIO()
        fieldnames = ["Handle", "Option1 Value", "Option2 Value", "Option3 Value", "Variant Price"]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()

        for product in products:
            handle = product.custom_handle or f"mercari-{product.id}"
            variants = session_db.query(Variant).filter_by(product_id=product.id).order_by(Variant.position).all()
            
            for variant in variants:
                price = variant.price
                final_price = int(price * markup) if price is not None else 0

                writer.writerow({
                    "Handle": handle,
                    "Option1 Value": variant.option1_value,
                    "Option2 Value": variant.option2_value,
                    "Option3 Value": variant.option3_value,
                    "Variant Price": final_price,
                })

        output.seek(0)
        response = make_response(output.getvalue())
        response.headers["Content-Disposition"] = "attachment; filename=shopify_price_update.csv"
        response.headers["Content-type"] = "text/csv"
        return response
    finally:
        session_db.close()


@export_bp.route("/export_images")
@login_required
def export_images():
    """Export product images as a ZIP file."""
    import zipfile
    import requests
    from io import BytesIO
    
    session_db = SessionLocal()
    try:
        products, _, _ = _parse_ids_and_params(session_db)
        if not products:
            return "対象の商品がありません。", 400

        # Create ZIP in memory
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for product in products:
                snapshot = (
                    session_db.query(ProductSnapshot)
                    .filter_by(product_id=product.id)
                    .order_by(ProductSnapshot.scraped_at.desc())
                    .first()
                )
                
                if not snapshot or not snapshot.image_urls:
                    continue
                
                image_urls = [u for u in snapshot.image_urls.split("|") if u]
                product_folder = f"product_{product.id}"
                
                for i, img_url in enumerate(image_urls):
                    try:
                        resp = requests.get(img_url, timeout=10)
                        if resp.status_code == 200:
                            # Determine extension from content type
                            content_type = resp.headers.get('Content-Type', '')
                            ext = '.jpg'
                            if 'png' in content_type:
                                ext = '.png'
                            elif 'webp' in content_type:
                                ext = '.webp'
                            
                            filename = f"{product_folder}/image_{i+1}{ext}"
                            zip_file.writestr(filename, resp.content)
                    except Exception as e:
                        print(f"Error downloading image: {e}")
                        continue

        zip_buffer.seek(0)
        response = make_response(zip_buffer.read())
        response.headers["Content-Disposition"] = "attachment; filename=product_images.zip"
        response.headers["Content-type"] = "application/zip"
        return response
    finally:
        session_db.close()

