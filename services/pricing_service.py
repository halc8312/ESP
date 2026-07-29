"""
Pricing Service

Handles the calculation of selling prices based on pricing rules.
Formula: selling_price = (cost_price + shipping_cost) * (1 + margin_rate/100) + fixed_fee
"""
import logging
from collections.abc import Iterable

from database import create_isolated_session
from models import Product, PricingRule, Variant

logger = logging.getLogger("pricing")


def resolve_product_selling_price(
    product: Product,
    *fallback_prices: int | None,
    fallback_multiplier: float = 1.0,
    reference_price: int | None = None,
) -> int | None:
    """Resolve the final product price used by customer/export surfaces.

    ``Product.selling_price`` is the application-level sale price and is
    therefore authoritative whenever it has been set (including an explicit
    zero). For multi-variant exports, ``reference_price`` preserves the
    variants' relative price differences while anchoring the first variant to
    that authoritative product price. Legacy products without one retain the
    old export behaviour by applying the caller's multiplier.
    """
    selling_price = getattr(product, "selling_price", None)
    if selling_price is not None:
        if reference_price is not None and reference_price > 0:
            for fallback_price in fallback_prices:
                if fallback_price is not None:
                    return int(round(float(fallback_price) * selling_price / reference_price))
        return int(selling_price)

    for fallback_price in fallback_prices:
        if fallback_price is not None:
            return int(float(fallback_price) * fallback_multiplier)
    return None


def resolve_variant_selling_price(
    product: Product,
    variant: Variant,
    *,
    fallback_multiplier: float = 1.0,
    reference_price: int | None = None,
) -> int | None:
    """Return the exact customer/export price for one variant.

    Resolution order is deliberately shared by the edit UI and every export:

    1. the variant's explicit sale-price override (including ``0``);
    2. the product's default sale price (including ``0``), proportionally
       adjusted for legacy multi-variant source prices;
    3. the legacy ``Variant.price`` source-price fallback.

    ``Variant.price`` remains source data so a patrol refresh cannot silently
    overwrite a price that an operator entered for customers.
    """
    variant_selling_price = getattr(variant, "selling_price", None)
    if variant_selling_price is not None:
        return int(variant_selling_price)

    return resolve_product_selling_price(
        product,
        getattr(variant, "price", None),
        fallback_multiplier=fallback_multiplier,
        reference_price=reference_price,
    )


def resolve_variant_selling_prices(
    product: Product,
    variants: Iterable[Variant],
    *,
    fallback_multiplier: float = 1.0,
) -> list[int | None]:
    """Resolve ordered variant prices under one common reference contract."""
    ordered_variants = list(variants)
    reference_price = None
    if len(ordered_variants) > 1:
        reference_variant = min(
            ordered_variants,
            key=lambda variant: (
                getattr(variant, "position", None) is None,
                getattr(variant, "position", None) or 0,
                getattr(variant, "id", None) is None,
                getattr(variant, "id", None) or 0,
            ),
        )
        first_source_price = getattr(reference_variant, "price", None)
        if first_source_price is not None:
            reference_price = first_source_price

    return [
        resolve_variant_selling_price(
            product,
            variant,
            fallback_multiplier=fallback_multiplier,
            reference_price=reference_price,
        )
        for variant in ordered_variants
    ]


def resolve_product_display_price(
    product: Product,
    variants: Iterable[Variant] | None = None,
) -> int | None:
    """Return the product-level price shown on catalog/price-list surfaces.

    A product card can show only one amount, so for a multi-variant product it
    uses the lowest price that can actually be exported.  With no variants,
    the product default and source price retain the legacy fallback behaviour.
    """
    ordered_variants = list(variants or ())
    resolved_prices = [
        price
        for price in resolve_variant_selling_prices(product, ordered_variants)
        if price is not None
    ]
    if resolved_prices:
        return min(resolved_prices)
    return resolve_product_selling_price(
        product,
        getattr(product, "last_price", None),
    )


def product_has_pricing_config(product: Product) -> bool:
    """
    Return True if the product is eligible for selling-price recalculation.

    A product is eligible when it either has a pricing rule assigned OR has
    at least one per-product manual override (margin% / shipping¥) set.
    Callers (patrol, scrape ingest, CLI) use this to gate the trigger of
    `update_product_selling_price`, so manual-override-only products are not
    silently skipped when their cost price changes.
    """
    if product is None:
        return False
    if getattr(product, "pricing_rule_id", None):
        return True
    if getattr(product, "manual_margin_rate", None) is not None:
        return True
    if getattr(product, "manual_shipping_cost", None) is not None:
        return True
    return False


def calculate_selling_price(
    cost_price: int | None,
    rule: PricingRule | None,
    *,
    manual_margin_rate: int | None = None,
    manual_shipping_cost: int | None = None,
) -> int | None:
    """
    Calculate selling price based on cost price and pricing rule.

    Args:
        cost_price: The scraped cost price (JPY)
        rule: The PricingRule to apply
        manual_margin_rate: Optional per-product override for margin% (replaces rule.margin_rate)
        manual_shipping_cost: Optional per-product override for shipping JPY (replaces rule.shipping_cost)

    Returns:
        Calculated selling price (JPY, rounded to integer), or ``None`` when
        the source cost has not been obtained. Missing cost must not become an
        explicit customer-facing zero price.
    """
    if cost_price is None or cost_price <= 0:
        return None

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


def calculate_configured_product_price(
    session,
    product: Product,
    *,
    user_id: int | None = None,
) -> int | None:
    """Calculate a product's automatic price from its persisted configuration.

    ``None`` means no automatic rule/manual override is configured, the
    configured rule is unavailable, or the source cost has not been obtained.
    This distinction lets callers clear a manual price without accidentally
    freezing the source cost as a new customer-facing override.
    """
    has_manual_override = (
        product.manual_margin_rate is not None
        or product.manual_shipping_cost is not None
    )
    if product.pricing_rule_id is None and not has_manual_override:
        return None

    rule = None
    if product.pricing_rule_id is not None:
        rule_query = session.query(PricingRule).filter(
            PricingRule.id == product.pricing_rule_id
        )
        if user_id is not None:
            rule_query = rule_query.filter(PricingRule.user_id == user_id)
        rule = rule_query.one_or_none()
        if rule is None:
            return None

    return calculate_selling_price(
        product.last_price,
        rule,
        manual_margin_rate=product.manual_margin_rate,
        manual_shipping_cost=product.manual_shipping_cost,
    )


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
        session = create_isolated_session()
    try:
        product = session.query(Product).filter_by(id=product_id).first()
        if not product:
            logger.warning("Product %s not found", product_id)
            return False

        has_manual_override = (
            product.manual_margin_rate is not None
            or product.manual_shipping_cost is not None
        )

        if not product.pricing_rule_id and not has_manual_override:
            # No pricing rule assigned and no manual overrides — nothing to recalc
            return False

        old_price = product.selling_price
        new_price = calculate_configured_product_price(
            session,
            product,
            user_id=product.user_id,
        )
        if new_price is None:
            logger.warning(
                "Product %s automatic price unavailable; keeping existing price",
                product_id,
            )
            return False
        
        if old_price != new_price:
            product.selling_price = new_price
            if owns_session:
                session.commit()
            logger.info(
                "Product %s: selling_price updated %s -> %s",
                product_id,
                old_price,
                new_price,
            )
            return True
        
        return False
        
    except Exception as e:
        logger.error("Error updating selling price for product %s: %s", product_id, e)
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
        session = create_isolated_session()
    updated_count = 0
    try:
        rule = session.query(PricingRule).filter_by(id=rule_id).first()
        if not rule:
            return 0
        
        products = session.query(Product).filter_by(pricing_rule_id=rule_id).all()
        for product in products:
            new_price = calculate_configured_product_price(
                session,
                product,
                user_id=product.user_id,
            )
            if new_price is None:
                continue
            if product.selling_price != new_price:
                product.selling_price = new_price
                updated_count += 1
        
        if owns_session:
            session.commit()
        logger.info("Updated %s products with rule %s", updated_count, rule_id)
        return updated_count
        
    except Exception as e:
        logger.error("Error updating products with rule %s: %s", rule_id, e)
        if owns_session:
            session.rollback()
        return 0
    finally:
        if owns_session:
            session.close()
