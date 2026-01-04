"""
Pricing Routes

Handles CRUD operations for pricing rules.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from database import SessionLocal
from models import PricingRule
from services.pricing_service import update_all_products_with_rule

pricing_bp = Blueprint('pricing', __name__)


@pricing_bp.route('/pricing')
@login_required
def pricing_list():
    """List all pricing rules for the current user."""
    session_db = SessionLocal()
    try:
        rules = session_db.query(PricingRule).filter_by(user_id=current_user.id).all()
        return render_template('pricing.html', rules=rules)
    finally:
        session_db.close()


@pricing_bp.route('/pricing/create', methods=['POST'])
@login_required
def pricing_create():
    """Create a new pricing rule."""
    session_db = SessionLocal()
    try:
        rule = PricingRule(
            user_id=current_user.id,
            name=request.form.get('name', 'New Rule'),
            margin_rate=int(request.form.get('margin_rate', 30)),
            shipping_cost=int(request.form.get('shipping_cost', 0)),
            fixed_fee=int(request.form.get('fixed_fee', 0))
        )
        session_db.add(rule)
        session_db.commit()
        flash('価格ルールを作成しました', 'success')
        return redirect(url_for('pricing.pricing_list'))
    except Exception as e:
        session_db.rollback()
        flash(f'エラー: {e}', 'error')
        return redirect(url_for('pricing.pricing_list'))
    finally:
        session_db.close()


@pricing_bp.route('/pricing/<int:rule_id>/edit', methods=['POST'])
@login_required
def pricing_edit(rule_id):
    """Edit an existing pricing rule."""
    session_db = SessionLocal()
    try:
        rule = session_db.query(PricingRule).filter_by(id=rule_id, user_id=current_user.id).first()
        if not rule:
            flash('ルールが見つかりません', 'error')
            return redirect(url_for('pricing.pricing_list'))
        
        rule.name = request.form.get('name', rule.name)
        rule.margin_rate = int(request.form.get('margin_rate', rule.margin_rate))
        rule.shipping_cost = int(request.form.get('shipping_cost', rule.shipping_cost))
        rule.fixed_fee = int(request.form.get('fixed_fee', rule.fixed_fee))
        
        session_db.commit()
        
        # Recalculate all products using this rule
        updated = update_all_products_with_rule(rule_id)
        flash(f'価格ルールを更新しました ({updated}件の商品価格を再計算)', 'success')
        return redirect(url_for('pricing.pricing_list'))
    except Exception as e:
        session_db.rollback()
        flash(f'エラー: {e}', 'error')
        return redirect(url_for('pricing.pricing_list'))
    finally:
        session_db.close()


@pricing_bp.route('/pricing/<int:rule_id>/delete', methods=['POST'])
@login_required
def pricing_delete(rule_id):
    """Delete a pricing rule."""
    session_db = SessionLocal()
    try:
        rule = session_db.query(PricingRule).filter_by(id=rule_id, user_id=current_user.id).first()
        if rule:
            session_db.delete(rule)
            session_db.commit()
            flash('価格ルールを削除しました', 'success')
        return redirect(url_for('pricing.pricing_list'))
    except Exception as e:
        session_db.rollback()
        flash(f'エラー: {e}', 'error')
        return redirect(url_for('pricing.pricing_list'))
    finally:
        session_db.close()
