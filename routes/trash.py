"""
Trash routes - Soft delete with recovery and purge.
"""
from datetime import timedelta
import logging
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from database import SessionLocal
from models import ImageProcessingJob, Product, ProductThumbnailJob
from services.product_purge import ProductPurgeOwnershipError, purge_locked_trashed_product
from time_utils import utc_now

trash_bp = Blueprint('trash', __name__)
logger = logging.getLogger(__name__)


def _lock_trashed_product(session_db, product_id, *, user_id=None, cutoff=None):
    query = session_db.query(Product).filter(
        Product.id == product_id, Product.deleted_at.is_not(None),
    )
    if user_id is not None:
        query = query.filter(Product.user_id == user_id)
    if cutoff is not None:
        query = query.filter(Product.deleted_at < cutoff)
    product = query.with_for_update().first()
    if product is None:
        return None
    # FOR UPDATE fences restore/source edits on PostgreSQL. The no-op write
    # supplies SQLite's write fence and rechecks ownership/deletion atomically.
    if query.update({Product.deleted_at: Product.deleted_at}, synchronize_session=False) != 1:
        return None
    return product


def _has_thumbnail_reservation(session_db, product_id):
    # Physical work outlives its mutable capture, state and upload lease.
    # Purging snapshots would cascade away the only durable reservation.
    thumbnail_reserved = session_db.query(ProductThumbnailJob.product_id).filter(
        ProductThumbnailJob.product_id == product_id,
        ProductThumbnailJob.job_id.is_not(None),
    ).first() is not None
    if thumbnail_reserved:
        return True
    # A background-removal upload can also outlive its initial DB read.
    # Preserve queued/running rows until that physical work has finished.
    return session_db.query(ImageProcessingJob.id).filter(
        ImageProcessingJob.product_id == product_id,
        ImageProcessingJob.status.in_(("queued", "running")),
    ).first() is not None


def _purge_product(session_db, product):
    purge_locked_trashed_product(session_db, product)


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
        now = utc_now()
        for item in items:
            days_passed = (now - item.deleted_at).days
            item.days_remaining = max(0, 30 - days_passed)
        
        return render_template('trash.html', items=items)
    except Exception:
        session_db.rollback()
        raise
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
                product.deleted_at = utc_now()
                count += 1
        
        session_db.commit()
        flash(f'{count}件をゴミ箱に移動しました', 'success')
        return redirect(request.referrer or url_for('main.index'))
    except Exception:
        session_db.rollback()
        raise
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
    except Exception:
        session_db.rollback()
        raise
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
        blocked = 0
        mismatched = 0
        for product_id in ids:
            product = _lock_trashed_product(session_db, int(product_id), user_id=current_user.id)
            if product:
                if _has_thumbnail_reservation(session_db, product.id):
                    blocked += 1
                    continue
                try:
                    _purge_product(session_db, product)
                except ProductPurgeOwnershipError:
                    mismatched += 1
                    logger.warning("Product purge blocked by dependent owner mismatch product_id=%s", product.id)
                    continue
                count += 1
        
        session_db.commit()
        flash(f'{count}件を完全に削除しました', 'success')
        if blocked:
            flash(f'{blocked}件は画像処理中のため削除できません。画像処理が終了してから再試行してください。', 'warning')
        if mismatched:
            flash(f'{mismatched}件は関連データの所有者が一致しないため削除できません。管理者にご連絡ください。', 'warning')
        return redirect(url_for('trash.trash_list'))
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


def purge_old_trash():
    """Auto-purge items deleted more than 30 days ago."""
    session_db = SessionLocal()
    try:
        cutoff = utc_now() - timedelta(days=30)
        old_ids = session_db.query(Product.id).filter(
            Product.deleted_at != None,
            Product.deleted_at < cutoff
        ).all()
        
        count = 0
        for (product_id,) in old_ids:
            product = _lock_trashed_product(session_db, product_id, cutoff=cutoff)
            if product is None or _has_thumbnail_reservation(session_db, product.id):
                continue
            try:
                _purge_product(session_db, product)
            except ProductPurgeOwnershipError:
                logger.warning("Automatic product purge blocked by dependent owner mismatch product_id=%s", product.id)
                continue
            count += 1
        
        session_db.commit()
        return count
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()
