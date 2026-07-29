"""
Pricing Routes

Handles CRUD operations for pricing rules.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from database import SessionLocal
from models import PricingRule, Product, User
from services.pricing_service import update_all_products_with_rule

pricing_bp = Blueprint('pricing', __name__)


def _parse_rule_form(form, *, defaults=None):
    """Validate one pricing rule submission without partially mutating a row."""
    defaults = defaults or {}
    name = (form.get("name", defaults.get("name", "")) or "").strip()
    if not name:
        return None, "ルール名を入力してください"

    parsed = {"name": name}
    field_specs = (
        ("margin_rate", "利益上乗せ率", defaults.get("margin_rate", 30), 500),
        ("shipping_cost", "送料上乗せ", defaults.get("shipping_cost", 0), 100_000_000),
        ("fixed_fee", "固定手数料", defaults.get("fixed_fee", 0), 100_000_000),
    )
    for field_name, label, default_value, maximum in field_specs:
        raw_value = form.get(field_name, default_value)
        raw_text = str(default_value if raw_value in (None, "") else raw_value).strip()
        try:
            value = int(raw_text)
        except (TypeError, ValueError):
            return None, f"{label}は0以上の整数で入力してください"
        if value < 0 or value > maximum:
            if field_name == "margin_rate":
                return None, "利益上乗せ率は0〜500の整数で入力してください"
            return None, f"{label}は0以上の整数で入力してください"
        parsed[field_name] = value
    return parsed, None


@pricing_bp.route('/pricing')
@login_required
def pricing_list():
    """List all pricing rules for the current user."""
    session_db = SessionLocal()
    try:
        rules = session_db.query(PricingRule).filter_by(user_id=current_user.id).all()
        return render_template('pricing.html', rules=rules)
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()


@pricing_bp.route('/pricing/create', methods=['POST'])
@login_required
def pricing_create():
    """Create a new pricing rule."""
    session_db = SessionLocal()
    try:
        values, validation_error = _parse_rule_form(request.form)
        if validation_error:
            flash(validation_error, 'error')
            return redirect(url_for('pricing.pricing_list'))

        rule = PricingRule(user_id=current_user.id, **values)
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

        values, validation_error = _parse_rule_form(
            request.form,
            defaults={
                "name": rule.name,
                "margin_rate": rule.margin_rate,
                "shipping_cost": rule.shipping_cost,
                "fixed_fee": rule.fixed_fee,
            },
        )
        if validation_error:
            flash(validation_error, 'error')
            return redirect(url_for('pricing.pricing_list'))

        rule.name = values["name"]
        rule.margin_rate = values["margin_rate"]
        rule.shipping_cost = values["shipping_cost"]
        rule.fixed_fee = values["fixed_fee"]
        
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
            # Clear both sides that can reference the rule before deletion.
            # Keep each product's already-calculated selling_price as its
            # manual sale price rather than making a rule deletion alter what
            # customers see.
            session_db.query(User).filter(
                User.id == current_user.id,
                User.default_pricing_rule_id == rule.id,
            ).update(
                {User.default_pricing_rule_id: None},
                synchronize_session=False,
            )
            session_db.query(Product).filter(
                Product.user_id == current_user.id,
                Product.pricing_rule_id == rule.id,
            ).update(
                {Product.pricing_rule_id: None},
                synchronize_session=False,
            )
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
