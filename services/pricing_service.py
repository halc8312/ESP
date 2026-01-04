"""
Pricing Service

Handles the calculation of selling prices based on pricing rules.
Formula: selling_price = (cost_price + shipping_cost) * (1 + margin_rate/100) + fixed_fee
"""
import logging
from database import SessionLocal
from models import Product, PricingRule

logger = logging.getLogger("pricing")


def calculate_selling_price(cost_price: int, rule: PricingRule) -> int:
    """
    Calculate selling price based on cost price and pricing rule.
    
    Args:
        cost_price: The scraped cost price (JPY)
        rule: The PricingRule to apply
        
    Returns:
        Calculated selling price (JPY, rounded to integer)
    """
    if cost_price is None or cost_price <= 0:
        return 0
    
    if rule is None:
        # No rule, return cost as-is (or apply default margin)
        return cost_price
    
    # Formula: (cost + shipping) * (1 + margin%) + fixed_fee
    base = cost_price + (rule.shipping_cost or 0)
    margin_multiplier = 1 + (rule.margin_rate or 0) / 100
    result = base * margin_multiplier + (rule.fixed_fee or 0)
    
    return int(round(result))


def update_product_selling_price(product_id: int) -> bool:
    """
    Recalculate and update the selling price for a product.
    
    Args:
        product_id: The ID of the product to update
        
    Returns:
        True if updated, False if no rule assigned or error
    """
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
        new_price = calculate_selling_price(product.last_price, rule)
        
        if old_price != new_price:
            product.selling_price = new_price
            session.commit()
            logger.info(f"Product {product_id}: selling_price updated {old_price} -> {new_price}")
            return True
        
        return False
        
    except Exception as e:
        logger.error(f"Error updating selling price for product {product_id}: {e}")
        session.rollback()
        return False
    finally:
        session.close()


def update_all_products_with_rule(rule_id: int) -> int:
    """
    Update all products that use a specific pricing rule.
    
    Args:
        rule_id: The ID of the pricing rule
        
    Returns:
        Number of products updated
    """
    session = SessionLocal()
    updated_count = 0
    try:
        rule = session.query(PricingRule).filter_by(id=rule_id).first()
        if not rule:
            return 0
        
        products = session.query(Product).filter_by(pricing_rule_id=rule_id).all()
        for product in products:
            new_price = calculate_selling_price(product.last_price, rule)
            if product.selling_price != new_price:
                product.selling_price = new_price
                updated_count += 1
        
        session.commit()
        logger.info(f"Updated {updated_count} products with rule {rule_id}")
        return updated_count
        
    except Exception as e:
        logger.error(f"Error updating products with rule {rule_id}: {e}")
        session.rollback()
        return 0
    finally:
        session.close()
