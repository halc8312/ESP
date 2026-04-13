"""
Monitor Service - Lightweight Patrol for price/stock updates.
Uses efficient patrol scrapers that only fetch price and stock data.
Non-Mercari sites use HTTP-only fetching (Scrapling) to avoid launching Chrome.
"""
import logging
from datetime import timedelta
from sqlalchemy import asc
from database import SessionLocal
from models import Product, Variant
from services.pricing_service import update_product_selling_price
from services.scrape_result_policy import normalize_status_for_persistence
from time_utils import utc_now
from utils import is_valid_detail_url

# Import lightweight patrol scrapers
from services.patrol.mercari_patrol import MercariPatrol
from services.patrol.yahoo_patrol import YahooPatrol
from services.patrol.rakuma_patrol import RakumaPatrol
from services.patrol.surugaya_patrol import SurugayaPatrol
from services.patrol.offmall_patrol import OffmallPatrol
from services.patrol.yahuoku_patrol import YahuokuPatrol
from services.patrol.snkrdunk_patrol import SnkrdunkPatrol

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("patrol")

# Sites that require a browser (Selenium/Chrome)
_BROWSER_SITES = frozenset()

# Maximum backoff in minutes for consecutive patrol failures
_MAX_BACKOFF_MINUTES = 180


class MonitorService:
    """
    Lightweight patrol service for efficient price/stock monitoring.
    Uses HTTP-only fetching for non-Mercari sites (no Chrome required).
    Shares a single WebDriver across Mercari items for speed.
    """
    
    # Site-specific patrol instances
    _patrols = {
        'mercari': MercariPatrol(),
        'yahoo': YahooPatrol(),
        'rakuma': RakumaPatrol(),
        'surugaya': SurugayaPatrol(),
        'offmall': OffmallPatrol(),
        'yahuoku': YahuokuPatrol(),
        'snkrdunk': SnkrdunkPatrol(),
    }
    
    @staticmethod
    def _apply_backoff(product, session_db):
        """Increment fail count and push updated_at into the future."""
        product.patrol_fail_count = (product.patrol_fail_count or 0) + 1
        backoff_minutes = min(product.patrol_fail_count * 15, _MAX_BACKOFF_MINUTES)
        product.updated_at = utc_now() + timedelta(minutes=backoff_minutes)
        session_db.commit()

    @staticmethod
    def check_stale_products(limit=15):
        """
        Check items that haven't been updated for the longest time.
        Uses lightweight patrol for speed (price/stock only).
        Non-Mercari sites use HTTP-only Scrapling fetch (no Chrome needed).
        
        Args:
            limit: Number of products to check (increased from 5 to 15 due to speed)
        """
        logger.info(f"--- Starting Lightweight Patrol (Limit: {limit}) ---")
        session_db = SessionLocal()
        driver = None
        
        try:
            # Find products sorted by updated_at ascending (oldest first)
            # Exclude archived products - include all supported sites
            products = session_db.query(Product).filter(
                Product.site.in_(list(MonitorService._patrols.keys())),
                Product.archived != True,
                Product.deleted_at == None,
            ).order_by(asc(Product.updated_at)).limit(limit).all()

            if not products:
                logger.info("No products to monitor.")
                return

            updated_count = 0
            error_count = 0

            for product in products:
                try:
                    logger.info(f"Patrol: {product.source_url[:50]}...")
                    
                    # ---- URL validation gate ----
                    if not is_valid_detail_url(product.source_url, product.site):
                        logger.warning(
                            f"Invalid detail URL (site={product.site}), "
                            f"skipping: {product.source_url[:80]}"
                        )
                        MonitorService._apply_backoff(product, session_db)
                        error_count += 1
                        continue
                    
                    # Choose patrol scraper
                    patrol = MonitorService._patrols.get(product.site)
                    if not patrol:
                        logger.warning(f"No patrol for site: {product.site}")
                        continue
                    
                    # All sites now use driver-less scraping or manage drivers internally
                    result = patrol.fetch(product.source_url)
                    
                    normalized_status = normalize_status_for_persistence(result.status)
                    active_missing_price = (
                        normalized_status == "on_sale"
                        and result.price is None
                        and str(result.confidence or "").lower() == "low"
                    )
                    unknown_status = normalized_status == "unknown"

                    if not result.success or active_missing_price or unknown_status:
                        failure_reason = (
                            result.error
                            or result.reason
                            or ("unknown patrol status" if unknown_status else "low-confidence active result")
                        )
                        logger.warning(f"Patrol failed: {failure_reason}")
                        MonitorService._apply_backoff(product, session_db)
                        error_count += 1
                        continue
                    
                    # ---- Success: reset fail counter ----
                    product.patrol_fail_count = 0

                    # Update product with new data
                    changes = MonitorService._apply_patrol_result(
                        session_db, product, result
                    )
                    
                    if changes > 0:
                        updated_count += 1
                        logger.info(f"Updated {product.id}: {changes} changes")
                        
                        # Recalculate selling price if needed
                        if product.pricing_rule_id:
                            update_product_selling_price(product.id, session=session_db)
                    
                    # Always update timestamp even if no changes
                    product.updated_at = utc_now()
                    session_db.commit()
                    
                except Exception as e:
                    logger.error(f"Error checking product {product.id}: {e}")
                    try:
                        MonitorService._apply_backoff(product, session_db)
                    except Exception:
                        pass
                    error_count += 1
                    continue
            
            logger.info(f"Patrol complete: {updated_count} updated, {error_count} errors")
                    
        except Exception as e:
            logger.error(f"Patrol fatal error: {e}")
            session_db.rollback()
        finally:
            session_db.close()
            logger.info("--- Patrol Finished ---")
    
    @staticmethod
    def _apply_patrol_result(session_db, product, result) -> int:
        """
        Apply patrol result to product and variants.
        Returns number of changes made.
        """
        changes = 0
        normalized_status = normalize_status_for_persistence(result.status)
        
        # Update price if changed
        if (
            result.price is not None
            and str(result.confidence or "").lower() == "high"
            and result.price != product.last_price
        ):
            product.last_price = result.price
            changes += 1
        
        # Update status if changed
        if normalized_status and normalized_status != "unknown" and normalized_status != product.last_status:
            product.last_status = normalized_status
            changes += 1
        
        existing_variants = session_db.query(Variant).filter_by(
            product_id=product.id
        ).all()

        if normalized_status in {"sold", "deleted"}:
            for existing in existing_variants:
                if existing.inventory_qty != 0:
                    existing.inventory_qty = 0
                    changes += 1
        elif normalized_status == "on_sale" and not result.variants:
            for existing in existing_variants:
                if existing.option1_value == "Default Title" and (existing.inventory_qty or 0) == 0:
                    existing.inventory_qty = 1
                    changes += 1

        # Update variant stock if available
        if result.variants:
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
                        if (
                            var_price
                            and str(result.confidence or "").lower() == "high"
                            and existing.price != var_price
                        ):
                            existing.price = var_price
                            changes += 1
                        break
        
        return changes

