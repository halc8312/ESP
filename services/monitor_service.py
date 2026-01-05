"""
Monitor Service - Lightweight Patrol for price/stock updates.
Uses efficient patrol scrapers that only fetch price and stock data.
"""
import logging
from datetime import datetime
from sqlalchemy import asc
from database import SessionLocal
from models import Product, Variant
from services.pricing_service import update_product_selling_price

# Import lightweight patrol scrapers
from services.patrol.mercari_patrol import MercariPatrol
from services.patrol.yahoo_patrol import YahooPatrol
from services.patrol.rakuma_patrol import RakumaPatrol

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("patrol")


class MonitorService:
    """
    Lightweight patrol service for efficient price/stock monitoring.
    Uses shared WebDriver across items for speed.
    """
    
    # Site-specific patrol instances
    _patrols = {
        'mercari': MercariPatrol(),
        'yahoo': YahooPatrol(),
        'rakuma': RakumaPatrol()
    }
    
    @staticmethod
    def check_stale_products(limit=15):
        """
        Check items that haven't been updated for the longest time.
        Uses lightweight patrol for speed (price/stock only).
        
        Args:
            limit: Number of products to check (increased from 5 to 15 due to speed)
        """
        logger.info(f"--- Starting Lightweight Patrol (Limit: {limit}) ---")
        session_db = SessionLocal()
        driver = None
        
        try:
            # Find products sorted by updated_at ascending (oldest first)
            # Exclude archived products
            products = session_db.query(Product).filter(
                Product.site.in_(['mercari', 'yahoo', 'rakuma']),
                Product.archived != True
            ).order_by(asc(Product.updated_at)).limit(limit).all()

            if not products:
                logger.info("No products to monitor.")
                return

            # Create shared driver for efficiency
            from mercari_db import create_driver
            driver = create_driver(headless=True)
            
            updated_count = 0
            error_count = 0

            for product in products:
                try:
                    logger.info(f"Patrol: {product.source_url[:50]}...")
                    
                    # Choose patrol scraper
                    patrol = MonitorService._patrols.get(product.site)
                    if not patrol:
                        logger.warning(f"No patrol for site: {product.site}")
                        continue
                    
                    # Fetch price/stock with shared driver
                    result = patrol.fetch(product.source_url, driver=driver)
                    
                    if not result.success:
                        logger.warning(f"Patrol failed: {result.error}")
                        error_count += 1
                        continue
                    
                    # Update product with new data
                    changes = MonitorService._apply_patrol_result(
                        session_db, product, result
                    )
                    
                    if changes > 0:
                        updated_count += 1
                        logger.info(f"Updated {product.id}: {changes} changes")
                        
                        # Recalculate selling price if needed
                        if product.pricing_rule_id:
                            update_product_selling_price(product.id)
                    
                    # Always update timestamp even if no changes
                    product.updated_at = datetime.utcnow()
                    session_db.commit()
                    
                except Exception as e:
                    logger.error(f"Error checking product {product.id}: {e}")
                    error_count += 1
                    continue
            
            logger.info(f"Patrol complete: {updated_count} updated, {error_count} errors")
                    
        except Exception as e:
            logger.error(f"Patrol fatal error: {e}")
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            session_db.close()
            logger.info("--- Patrol Finished ---")
    
    @staticmethod
    def _apply_patrol_result(session_db, product, result) -> int:
        """
        Apply patrol result to product and variants.
        Returns number of changes made.
        """
        changes = 0
        
        # Update price if changed
        if result.price is not None and result.price != product.last_price:
            product.last_price = result.price
            changes += 1
        
        # Update status if changed
        if result.status and result.status != product.last_status:
            product.last_status = result.status
            changes += 1
        
        # Update variant stock if available
        if result.variants:
            existing_variants = session_db.query(Variant).filter_by(
                product_id=product.id
            ).all()
            
            # Match variants by name/option and update stock
            for patrol_var in result.variants:
                var_name = patrol_var.get("name", "")
                var_stock = patrol_var.get("stock", 0)
                var_price = patrol_var.get("price")
                
                for existing in existing_variants:
                    # Match by option1_value or combined name
                    existing_name = existing.option1_value or ""
                    if existing.option2_value:
                        existing_name += f" / {existing.option2_value}"
                    
                    if var_name in existing_name or existing_name in var_name:
                        if existing.inventory_qty != var_stock:
                            existing.inventory_qty = var_stock
                            changes += 1
                        if var_price and existing.price != var_price:
                            existing.price = var_price
                            changes += 1
                        break
        
        return changes
