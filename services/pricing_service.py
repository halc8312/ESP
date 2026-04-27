"""
Pricing Service

Handles the calculation of selling prices based on pricing rules.
Formula: selling_price = (cost_price + shipping_cost) * (1 + margin_rate/100) + fixed_fee
"""
import logging
from database import SessionLocal
from models import Product, PricingRule

logger = logging.getLogger("pricing")


def calculate_selling_price(
    cost_price: int,
    rule: PricingRule,
    *,
    manual_margin_rate: int | None = None,
    manual_shipping_cost: int | None = None,
) -> int:
    """
    Calculate selling price based on cost price and pricing rule.

    Args:
        cost_price: The scraped cost price (JPY)
        rule: The PricingRule to apply
        manual_margin_rate: Optional per-product override for margin% (replaces rule.margin_rate)
        manual_shipping_cost: Optional per-product override for shipping JPY (replaces rule.shipping_cost)

    Returns:
        Calculated selling price (JPY, rounded to integer)
    """
    if cost_price is None or cost_price <= 0:
        return 0

    rule_margin = (rule.margin_rate if rule is not None else None) or 0
    rule_shipping = (rule.shipping_cost if rule is not None else None) or 0
    rule_fixed_fee = (rule.fixed_fee if rule is not None else None) or 0

    margin_rate = manual_margin_rate if manual_margin_rate is not None else rule_margin
    shipping_cost = manual_shipping_cost if manual_shipping_cost is not None else rule_shipping

    if rule is None and manual_margin_rate is None and manual_shipping_cost is None:
        # No rule, return cost as-is (or apply default margin)
        return cost_price

    # Formula: (cost + shipping) * (1 + margin%) + fixed_fee
    base = cost_price + shipping_cost
    margin_multiplier = 1 + margin_rate / 100
    result = base * margin_multiplier + rule_fixed_fee

    return int(round(result))


def update_product_selling_price(product_id: int, session=None) -> bool:
    """
    Recalculate and update the selling price for a product.
    
    Args:
        product_id: The ID of the product to update
        session: Optional SQLAlchemy session. If provided, the caller owns the
                 session lifecycle (no commit/rollback/close performed here).
        
    Returns:
        True if updated, False if no rule assigned or error
    """
    owns_session = session is None
    if owns_session:
        session = SessionLocal()
    try:
        product = session.query(Product).filter_by(id=product_id).first()
        if not product:
            logger.warning(f"Product {product_id} not found")
            return False
        
        if not product.pricing_rule_id:
            # No pricing rule assigned
            return False
        
        rule = session.query(PricingRule).filter_by(id=product.pricing_rule_id).first()
        if not rule:
            logger.warning(f"PricingRule {product.pricing_rule_id} not found")
            return False
        
        old_price = product.selling_price
        new_price = calculate_selling_price(
            product.last_price,
            rule,
            manual_margin_rate=product.manual_margin_rate,
            manual_shipping_cost=product.manual_shipping_cost,
        )
        
        if old_price != new_price:
            product.selling_price = new_price
            if owns_session:
                session.commit()
            logger.info(f"Product {product_id}: selling_price updated {old_price} -> {new_price}")
            return True
        
        return False
        
    except Exception as e:
        logger.error(f"Error updating selling price for product {product_id}: {e}")
        if owns_session:
            session.rollback()
        return False
    finally:
        if owns_session:
            session.close()


def update_all_products_with_rule(rule_id: int, session=None) -> int:
    """
    Update all products that use a specific pricing rule.
    
    Args:
        rule_id: The ID of the pricing rule
        session: Optional SQLAlchemy session. If provided, the caller owns the
                 session lifecycle (no commit/rollback/close performed here).
        
    Returns:
        Number of products updated
    """
    owns_session = session is None
    if owns_session:
        session = SessionLocal()
    updated_count = 0
    try:
        rule = session.query(PricingRule).filter_by(id=rule_id).first()
        if not rule:
            return 0
        
        products = session.query(Product).filter_by(pricing_rule_id=rule_id).all()
        for product in products:
            new_price = calculate_selling_price(
                product.last_price,
                rule,
                manual_margin_rate=product.manual_margin_rate,
                manual_shipping_cost=product.manual_shipping_cost,
            )
            if product.selling_price != new_price:
                product.selling_price = new_price
                updated_count += 1
        
        if owns_session:
            session.commit()
        logger.info(f"Updated {updated_count} products with rule {rule_id}")
        return updated_count
        
    except Exception as e:
        logger.error(f"Error updating products with rule {rule_id}: {e}")
        if owns_session:
            session.rollback()
        return 0
    finally:
        if owns_session:
            session.close()
