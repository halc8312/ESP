"""
Settings routes - Exclusion keyword management.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from database import SessionLocal
from models import ExclusionKeyword, Shop

settings_bp = Blueprint('settings', __name__)


@settings_bp.route('/settings')
@login_required
def settings_list():
    """Display settings page with exclusion keywords."""
    session_db = SessionLocal()
    try:
        keywords = session_db.query(ExclusionKeyword).filter_by(user_id=current_user.id).order_by(ExclusionKeyword.created_at.desc()).all()
        all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
        return render_template('settings.html', keywords=keywords, all_shops=all_shops)
    finally:
        session_db.close()


@settings_bp.route('/settings/keyword/add', methods=['POST'])
@login_required
def add_keyword():
    """Add a new exclusion keyword."""
    session_db = SessionLocal()
    try:
        keyword = request.form.get('keyword', '').strip()
        match_type = request.form.get('match_type', 'partial')
        
        if not keyword:
            flash('キーワードを入力してください', 'error')
            return redirect(url_for('settings.settings_list'))
        
        # Check for duplicate
        existing = session_db.query(ExclusionKeyword).filter_by(
            user_id=current_user.id, 
            keyword=keyword
        ).first()
        
        if existing:
            flash('このキーワードは既に登録されています', 'error')
            return redirect(url_for('settings.settings_list'))
        
        new_keyword = ExclusionKeyword(
            user_id=current_user.id,
            keyword=keyword,
            match_type=match_type
        )
        session_db.add(new_keyword)
        session_db.commit()
        flash(f'除外キーワード「{keyword}」を追加しました', 'success')
        return redirect(url_for('settings.settings_list'))
    except Exception as e:
        session_db.rollback()
        flash(f'エラー: {e}', 'error')
        return redirect(url_for('settings.settings_list'))
    finally:
        session_db.close()


@settings_bp.route('/settings/keyword/<int:keyword_id>/delete', methods=['POST'])
@login_required
def delete_keyword(keyword_id):
    """Delete an exclusion keyword."""
    session_db = SessionLocal()
    try:
        keyword = session_db.query(ExclusionKeyword).filter_by(
            id=keyword_id, 
            user_id=current_user.id
        ).first()
        
        if keyword:
            session_db.delete(keyword)
            session_db.commit()
            flash('除外キーワードを削除しました', 'success')
        return redirect(url_for('settings.settings_list'))
    except Exception as e:
        session_db.rollback()
        flash(f'エラー: {e}', 'error')
        return redirect(url_for('settings.settings_list'))
    finally:
        session_db.close()
