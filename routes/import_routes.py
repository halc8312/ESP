"""
Import routes - CSV product import.
"""
import csv
import io
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from database import SessionLocal
from models import Product, Variant, Shop

import_bp = Blueprint('import', __name__)


@import_bp.route('/import')
@login_required
def import_form():
    """Show CSV import form."""
    session_db = SessionLocal()
    try:
        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        return render_template('import.html', all_shops=all_shops)
    finally:
        session_db.close()


@import_bp.route('/import/csv', methods=['POST'])
@login_required
def import_csv():
    """Handle CSV file upload and import."""
    session_db = SessionLocal()
    try:
        file = request.files.get('file')
        shop_id_str = request.form.get('shop_id')
        site = request.form.get('site', 'import')
        
        if not file or file.filename == '':
            flash('ファイルを選択してください', 'error')
            return redirect(url_for('import.import_form'))
        
        # Parse CSV
        content = file.stream.read().decode('utf-8-sig')  # Handle BOM
        reader = csv.DictReader(io.StringIO(content))
        
        shop_id = int(shop_id_str) if shop_id_str else None
        imported = 0
        errors = []
        
        for row_num, row in enumerate(reader, start=2):
            try:
                # Required fields
                title = row.get('title') or row.get('Title') or row.get('タイトル') or ''
                price_str = row.get('price') or row.get('Price') or row.get('価格') or '0'
                url = row.get('url') or row.get('URL') or row.get('商品URL') or ''
                
                if not title:
                    errors.append(f"Row {row_num}: タイトルがありません")
                    continue
                
                # Create product
                product = Product(
                    user_id=current_user.id,
                    shop_id=shop_id,
                    site=site,
                    source_url=url,
                    last_title=title,
                    last_price=int(float(price_str)) if price_str else 0,
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow()
                )
                session_db.add(product)
                session_db.flush()  # Get product ID
                
                # Create default variant
                sku = row.get('sku') or row.get('SKU') or f"IMP-{product.id}"
                variant = Variant(
                    product_id=product.id,
                    option1_value="Default Title",
                    sku=sku,
                    price=product.last_price,
                    inventory_qty=1,
                    position=1
                )
                session_db.add(variant)
                imported += 1
                
            except Exception as e:
                errors.append(f"Row {row_num}: {str(e)}")
        
        session_db.commit()
        
        msg = f'{imported}件のインポートが完了しました。'
        if errors:
            msg += f' {len(errors)}件のエラーがありました。'
        flash(msg, 'success' if imported > 0 else 'warning')
        
        return redirect(url_for('import.import_form'))
        
    except Exception as e:
        session_db.rollback()
        flash(f'インポートエラー: {str(e)}', 'error')
        return redirect(url_for('import.import_form'))
    finally:
        session_db.close()
