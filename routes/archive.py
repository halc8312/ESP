"""
Archive routes - SOLD Stacking / Product archiving.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import login_required, current_user
from database import SessionLocal
from models import Product, Shop

archive_bp = Blueprint('archive', __name__)


@archive_bp.route('/archive')
@login_required
def archive_list():
    """Display archived products."""
    session_db = SessionLocal()
    try:
        products = session_db.query(Product).filter(
            Product.user_id == current_user.id,
            Product.archived == True
        ).order_by(Product.updated_at.desc()).all()
        
        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        current_shop_id = session.get('current_shop_id')
        
        return render_template(
            'archive.html',
            products=products,
            all_shops=all_shops,
            current_shop_id=current_shop_id
        )
    finally:
        session_db.close()


@archive_bp.route('/archive/add', methods=['POST'])
@login_required
def archive_products():
    """Archive selected products."""
    session_db = SessionLocal()
    try:
        product_ids = request.form.getlist('ids')
        if not product_ids:
            return redirect(url_for('main.index'))
        
        products = session_db.query(Product).filter(
            Product.id.in_([int(pid) for pid in product_ids]),
            Product.user_id == current_user.id
        ).all()
        
        for product in products:
            product.archived = True
        
        session_db.commit()
        flash(f'{len(products)}件をアーカイブしました', 'success')
        return redirect(url_for('main.index'))
    except Exception as e:
        session_db.rollback()
        flash(f'エラー: {e}', 'error')
        return redirect(url_for('main.index'))
    finally:
        session_db.close()


@archive_bp.route('/archive/restore', methods=['POST'])
@login_required
def restore_products():
    """Restore archived products."""
    session_db = SessionLocal()
    try:
        product_ids = request.form.getlist('ids')
        if not product_ids:
            return redirect(url_for('archive.archive_list'))
        
        products = session_db.query(Product).filter(
            Product.id.in_([int(pid) for pid in product_ids]),
            Product.user_id == current_user.id
        ).all()
        
        for product in products:
            product.archived = False
        
        session_db.commit()
        flash(f'{len(products)}件を復元しました', 'success')
        return redirect(url_for('archive.archive_list'))
    except Exception as e:
        session_db.rollback()
        flash(f'エラー: {e}', 'error')
        return redirect(url_for('archive.archive_list'))
    finally:
        session_db.close()
