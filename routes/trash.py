"""
Trash routes - Soft delete with recovery and purge.
"""
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from database import SessionLocal
from models import Product, Variant, ProductSnapshot

trash_bp = Blueprint('trash', __name__)


@trash_bp.route('/trash')
@login_required
def trash_list():
    """Show items in trash."""
    session_db = SessionLocal()
    try:
        items = session_db.query(Product).filter(
            Product.user_id == current_user.id,
            Product.deleted_at != None
        ).order_by(Product.deleted_at.desc()).all()
        
        # Calculate days remaining for each item
        now = datetime.utcnow()
        for item in items:
            days_passed = (now - item.deleted_at).days
            item.days_remaining = max(0, 30 - days_passed)
        
        return render_template('trash.html', items=items)
    finally:
        session_db.close()


@trash_bp.route('/trash/delete', methods=['POST'])
@login_required
def trash_delete():
    """Move products to trash (soft delete)."""
    session_db = SessionLocal()
    try:
        ids = request.form.getlist('id')
        if not ids:
            flash('商品を選択してください', 'warning')
            return redirect(request.referrer or url_for('main.index'))
        
        count = 0
        for product_id in ids:
            product = session_db.query(Product).filter(
                Product.id == int(product_id),
                Product.user_id == current_user.id
            ).first()
            if product:
                product.deleted_at = datetime.utcnow()
                count += 1
        
        session_db.commit()
        flash(f'{count}件をゴミ箱に移動しました', 'success')
        return redirect(request.referrer or url_for('main.index'))
    finally:
        session_db.close()


@trash_bp.route('/trash/restore', methods=['POST'])
@login_required
def trash_restore():
    """Restore products from trash."""
    session_db = SessionLocal()
    try:
        ids = request.form.getlist('id')
        if not ids:
            flash('商品を選択してください', 'warning')
            return redirect(url_for('trash.trash_list'))
        
        count = 0
        for product_id in ids:
            product = session_db.query(Product).filter(
                Product.id == int(product_id),
                Product.user_id == current_user.id,
                Product.deleted_at != None
            ).first()
            if product:
                product.deleted_at = None
                count += 1
        
        session_db.commit()
        flash(f'{count}件を復元しました', 'success')
        return redirect(url_for('trash.trash_list'))
    finally:
        session_db.close()


@trash_bp.route('/trash/purge', methods=['POST'])
@login_required
def trash_purge():
    """Permanently delete products."""
    session_db = SessionLocal()
    try:
        ids = request.form.getlist('id')
        if not ids:
            flash('商品を選択してください', 'warning')
            return redirect(url_for('trash.trash_list'))
        
        count = 0
        for product_id in ids:
            product = session_db.query(Product).filter(
                Product.id == int(product_id),
                Product.user_id == current_user.id,
                Product.deleted_at != None
            ).first()
            if product:
                # Delete related data
                session_db.query(Variant).filter_by(product_id=product.id).delete()
                session_db.query(ProductSnapshot).filter_by(product_id=product.id).delete()
                session_db.delete(product)
                count += 1
        
        session_db.commit()
        flash(f'{count}件を完全に削除しました', 'success')
        return redirect(url_for('trash.trash_list'))
    finally:
        session_db.close()


def purge_old_trash():
    """Auto-purge items deleted more than 30 days ago."""
    session_db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=30)
        old_items = session_db.query(Product).filter(
            Product.deleted_at != None,
            Product.deleted_at < cutoff
        ).all()
        
        count = 0
        for product in old_items:
            session_db.query(Variant).filter_by(product_id=product.id).delete()
            session_db.query(ProductSnapshot).filter_by(product_id=product.id).delete()
            session_db.delete(product)
            count += 1
        
        session_db.commit()
        return count
    finally:
        session_db.close()
