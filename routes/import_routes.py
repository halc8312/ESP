"""
Import routes - CSV product import with preview.
"""
import csv
import io
import json
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, session as flask_session
from flask_login import login_required, current_user
from database import SessionLocal
from models import Product, Variant, Shop

import_bp = Blueprint('import', __name__)


def _map_csv_row(row: dict, is_shopify: bool = False) -> dict:
    """
    Map CSV row to standard format.
    Supports both standard and Shopify CSV formats.
    """
    if is_shopify:
        # Shopify format mapping
        return {
            'title': row.get('Title', ''),
            'price': row.get('Variant Price', '') or row.get('Price', '0'),
            'url': '',  # Shopify doesn't have source URL
            'sku': row.get('Variant SKU', ''),
            'description': row.get('Body (HTML)', ''),
            'image_urls': row.get('Image Src', ''),
            'inventory': row.get('Variant Inventory Qty', '1')
        }
    else:
        # Standard format
        return {
            'title': row.get('title') or row.get('Title') or row.get('タイトル') or '',
            'price': row.get('price') or row.get('Price') or row.get('価格') or '0',
            'url': row.get('url') or row.get('URL') or row.get('商品URL') or '',
            'sku': row.get('sku') or row.get('SKU') or '',
            'description': row.get('description') or row.get('Description') or row.get('説明') or '',
            'image_urls': row.get('image_urls') or row.get('Image URLs') or row.get('画像URL') or '',
            'inventory': row.get('inventory') or row.get('Inventory') or row.get('在庫') or '1'
        }


@import_bp.route('/import')
@login_required
def import_form():
    """Show CSV import form."""
    session_db = SessionLocal()
    try:
        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        return render_template('import.html', all_shops=all_shops, preview_data=None)
    finally:
        session_db.close()


@import_bp.route('/import/preview', methods=['POST'])
@login_required
def import_preview():
    """Preview CSV contents before importing."""
    session_db = SessionLocal()
    try:
        file = request.files.get('file')
        shop_id_str = request.form.get('shop_id')
        site = request.form.get('site', 'import')
        
        if not file or file.filename == '':
            flash('ファイルを選択してください', 'error')
            return redirect(url_for('import.import_form'))
        
        # Parse CSV
        content = file.stream.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(content))
        
        # Detect Shopify format
        is_shopify = False
        fieldnames = reader.fieldnames or []
        if 'Handle' in fieldnames or 'Title' in fieldnames and 'Variant SKU' in fieldnames:
            is_shopify = True
        
        preview_rows = []
        warnings = []
        
        if is_shopify:
            warnings.append('Shopify CSVフォーマットを検出しました')
        
        for row_num, row in enumerate(reader, start=2):
            # Map columns (support both standard and Shopify format)
            mapped = _map_csv_row(row, is_shopify)
            title = mapped.get('title', '')
            price_str = mapped.get('price', '0')
            url = mapped.get('url', '')

            
            status = 'ok'
            warning = ''
            
            if not title:
                status = 'error'
                warning = 'タイトルなし'
            elif url:
                existing = session_db.query(Product).filter(
                    Product.user_id == current_user.id,
                    Product.source_url == url
                ).first()
                if existing:
                    status = 'skip'
                    warning = f'重複 (ID: {existing.id})'
            
            preview_rows.append({
                'row_num': row_num,
                'title': title[:50] + '...' if len(title) > 50 else title,
                'price': price_str,
                'url': url[:40] + '...' if url and len(url) > 40 else url,
                'status': status,
                'warning': warning
            })
            
            if len(preview_rows) >= 50:
                warnings.append('プレビューは最初の50行のみ表示')
                break
        
        # Store CSV content in session for actual import
        flask_session['import_csv_content'] = content
        flask_session['import_shop_id'] = shop_id_str
        flask_session['import_site'] = site
        
        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        
        ok_count = sum(1 for r in preview_rows if r['status'] == 'ok')
        skip_count = sum(1 for r in preview_rows if r['status'] == 'skip')
        error_count = sum(1 for r in preview_rows if r['status'] == 'error')
        
        return render_template('import.html', 
            all_shops=all_shops,
            preview_data={
                'rows': preview_rows,
                'ok_count': ok_count,
                'skip_count': skip_count,
                'error_count': error_count,
                'warnings': warnings
            }
        )
    finally:
        session_db.close()


@import_bp.route('/import/execute', methods=['POST'])
@login_required
def import_execute():
    """Execute the actual import from previewed data."""
    content = flask_session.get('import_csv_content')
    shop_id_str = flask_session.get('import_shop_id')
    site = flask_session.get('import_site', 'import')
    
    if not content:
        flash('プレビューデータがありません。再度アップロードしてください。', 'error')
        return redirect(url_for('import.import_form'))
    
    # Clear session data
    flask_session.pop('import_csv_content', None)
    flask_session.pop('import_shop_id', None)
    flask_session.pop('import_site', None)
    
    # Process import
    return _process_import(content, shop_id_str, site)


@import_bp.route('/import/csv', methods=['POST'])
@login_required
def import_csv():
    """Handle CSV file upload and import (direct without preview)."""
    file = request.files.get('file')
    shop_id_str = request.form.get('shop_id')
    site = request.form.get('site', 'import')
    
    if not file or file.filename == '':
        flash('ファイルを選択してください', 'error')
        return redirect(url_for('import.import_form'))
    
    content = file.stream.read().decode('utf-8-sig')
    return _process_import(content, shop_id_str, site)


def _process_import(content: str, shop_id_str: str, site: str):
    """Shared import logic used by both direct and preview import."""
    session_db = SessionLocal()
    try:
        reader = csv.DictReader(io.StringIO(content))
        shop_id = int(shop_id_str) if shop_id_str else None
        imported = 0
        errors = []
        
        # Detect Shopify format
        fieldnames = reader.fieldnames or []
        is_shopify = 'Handle' in fieldnames or ('Title' in fieldnames and 'Variant SKU' in fieldnames)
        
        for row_num, row in enumerate(reader, start=2):
            try:
                # Map columns (support both standard and Shopify format)
                mapped = _map_csv_row(row, is_shopify)
                title = mapped.get('title', '')
                price_str = mapped.get('price', '0')
                url = mapped.get('url', '')
                description = mapped.get('description', '')
                image_urls = mapped.get('image_urls', '')
                sku = mapped.get('sku', '')
                inventory = mapped.get('inventory', '1')
                
                if not title:
                    errors.append(f"Row {row_num}: タイトルなし")
                    continue

                
                # Check for duplicate URL
                if url:
                    existing = session_db.query(Product).filter(
                        Product.user_id == current_user.id,
                        Product.source_url == url
                    ).first()
                    if existing:
                        errors.append(f"Row {row_num}: URL重複")
                        continue
                
                # Create product
                product = Product(
                    user_id=current_user.id,
                    shop_id=shop_id,
                    site=site,
                    source_url=url,
                    last_title=title,
                    custom_title=title,
                    custom_description=description,
                    last_price=int(float(price_str)) if price_str else 0,
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow()
                )
                session_db.add(product)
                session_db.flush()
                
                # Create snapshot if images/description provided
                if image_urls or description:
                    from models import ProductSnapshot
                    snapshot = ProductSnapshot(
                        product_id=product.id,
                        title=title,
                        price=product.last_price,
                        description=description,
                        image_urls=image_urls,
                        scraped_at=datetime.utcnow()
                    )
                    session_db.add(snapshot)
                
                # Create default variant
                variant = Variant(
                    product_id=product.id,
                    option1_value="Default Title",
                    sku=sku or f"IMP-{product.id}",
                    price=product.last_price,
                    inventory_qty=int(inventory) if inventory else 1,
                    position=1
                )
                session_db.add(variant)
                imported += 1
                
            except Exception as e:
                errors.append(f"Row {row_num}: {str(e)}")
        
        session_db.commit()
        
        msg = f'{imported}件のインポートが完了しました。'
        if errors:
            msg += f' {len(errors)}件スキップ。'
        flash(msg, 'success' if imported > 0 else 'warning')
        
        return redirect(url_for('import.import_form'))
        
    except Exception as e:
        session_db.rollback()
        flash(f'インポートエラー: {str(e)}', 'error')
        return redirect(url_for('import.import_form'))
    finally:
        session_db.close()

