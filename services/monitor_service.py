"""
Monitor Service - Lightweight Patrol for price/stock updates.
Uses efficient patrol scrapers that only fetch price and stock data.
Non-Mercari sites use HTTP-only fetching (Scrapling) to avoid launching Chrome.
"""
import logging
from datetime import timedelta
from sqlalchemy import asc, func, or_
from database import create_isolated_session
from models import Product, Variant
from services.pricing_service import product_has_pricing_config, update_product_selling_price
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

    # Mercari-specific: track consecutive soft-sold counts per product to
    # implement hysteresis.  Only stored in memory — resets on restart,
    # which is the safe default (never persisting a stale count).
    # NOTE: Not thread-safe; relies on the single-threaded RQ worker model
    # used in production.  If concurrency is introduced, wrap with a Lock.
    _mercari_soft_sold_counts: dict[int, int] = {}

    # Number of consecutive soft-sold patrol results required before
    # the status is actually persisted as ``sold``.
    _MERCARI_SOFT_SOLD_THRESHOLD = 2
    
    @staticmethod
    def _apply_backoff(product, session_db, now=None):
        """Increment fail count and schedule the next patrol attempt."""
        now = now or utc_now()
        product.patrol_fail_count = (product.patrol_fail_count or 0) + 1
        backoff_minutes = min(product.patrol_fail_count * 15, _MAX_BACKOFF_MINUTES)
        product.last_patrolled_at = now
        product.next_patrol_at = now + timedelta(minutes=backoff_minutes)
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
        # Use an isolated session: helpers invoked during a patrol cycle
        # (selector repair store, pricing, alerts) close the thread-scoped
        # SessionLocal, which would detach every product mid-loop and make
        # all patrol commits silent no-ops.
        session_db = create_isolated_session()
        driver = None
        summary = {
            "status": "started",
            "limit": limit,
            "selected_count": 0,
            "updated_count": 0,
            "error_count": 0,
            "site_counts": {},
        }
        
        try:
            # Find due products by patrol cursor, independent from the
            # product-list updated_at sort.
            now = utc_now()
            patrol_cursor = func.coalesce(Product.last_patrolled_at, Product.updated_at, Product.created_at)
            products = session_db.query(Product).filter(
                Product.site.in_(list(MonitorService._patrols.keys())),
                Product.archived != True,
                Product.deleted_at == None,
                or_(Product.next_patrol_at == None, Product.next_patrol_at <= now),
            ).order_by(asc(patrol_cursor), asc(Product.id)).limit(limit).all()

            summary["selected_count"] = len(products)
            summary["site_counts"] = {
                site: sum(1 for product in products if product.site == site)
                for site in sorted({product.site for product in products})
            }

            if not products:
                summary["status"] = "no_products"
                logger.info("No products to monitor.")
                return summary

            updated_count = 0
            error_count = 0

            for product in products:
                product_id = product.id
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
                    product.last_patrolled_at = utc_now()
                    product.next_patrol_at = None

                    # ── Mercari sold-hysteresis ──────────────────────────
                    # Soft-evidence "sold" from Mercari is not persisted
                    # until seen N consecutive times, preventing transient
                    # hydration glitches from flipping active items to sold.
                    if (
                        product.site == "mercari"
                        and normalized_status == "sold"
                        and getattr(result, "evidence_strength", "hard") == "soft"
                    ):
                        prev_count = MonitorService._mercari_soft_sold_counts.get(product.id, 0)
                        new_count = prev_count + 1
                        MonitorService._mercari_soft_sold_counts[product.id] = new_count
                        if new_count < MonitorService._MERCARI_SOFT_SOLD_THRESHOLD:
                            logger.info(
                                "Mercari soft-sold deferred for product %s "
                                "(%d/%d): %s",
                                product.id,
                                new_count,
                                MonitorService._MERCARI_SOFT_SOLD_THRESHOLD,
                                result.reason or "",
                            )
                            # Treat as unknown for this cycle — do not persist
                            product.updated_at = product.last_patrolled_at
                            session_db.commit()
                            continue
                        else:
                            logger.info(
                                "Mercari soft-sold confirmed for product %s "
                                "after %d consecutive observations",
                                product.id,
                                new_count,
                            )
                    else:
                        # Any non-soft-sold result resets the counter
                        MonitorService._mercari_soft_sold_counts.pop(product.id, None)

                    # Update product with new data
                    changes = MonitorService._apply_patrol_result(
                        session_db, product, result
                    )
                    
                    if changes > 0:
                        updated_count += 1
                        logger.info(f"Updated {product.id}: {changes} changes")
                        
                        # Recalculate selling price if needed
                        if product_has_pricing_config(product):
                            update_product_selling_price(product.id, session=session_db)
                    
                    # Always update timestamp even if no changes
                    product.updated_at = product.last_patrolled_at or utc_now()
                    session_db.commit()
                    
                except Exception as e:
                    logger.error(f"Error checking product {product_id}: {e}")
                    try:
                        session_db.rollback()
                        MonitorService._apply_backoff(product, session_db)
                    except Exception:
                        logger.exception(f"Backoff failed for product {product_id}")
                    error_count += 1
                    continue
            
            summary["status"] = "completed"
            summary["updated_count"] = updated_count
            summary["error_count"] = error_count
            logger.info(f"Patrol complete: {updated_count} updated, {error_count} errors")
            return summary
                    
        except Exception as e:
            summary["status"] = "fatal_error"
            summary["fatal_error"] = type(e).__name__
            logger.error(f"Patrol fatal error: {e}")
            session_db.rollback()
            return summary
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

