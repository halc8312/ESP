import logging
from datetime import datetime
from sqlalchemy import asc
from database import SessionLocal
from models import Product, ProductSnapshot, Variant
from mercari_db import scrape_single_item as scrape_mercari
from yahoo_db import scrape_single_item as scrape_yahoo
from rakuma_db import scrape_single_item as scrape_rakuma
from services.product_service import save_scraped_items_to_db

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("patrol")

class MonitorService:
    @staticmethod
    def check_stale_products(limit=5):
        """
        Check items that haven't been updated for the longest time.
        Target only 'active' items (on sale in scraping list).
        """
        logger.info(f"--- Starting Patrol (Limit: {limit}) ---")
        session_db = SessionLocal()
        try:
            # Find active products sorted by updated_at ascending (oldest first)
            # We filter by site to ensure we have a scraper for it
            products = session_db.query(Product).filter(
                Product.site.in_(['mercari', 'yahoo', 'rakuma']),
                # We might want to filter by status='active' if we had such a field for "monitoring enabled"
                # For now, let's monitor everything that is tracked.
            ).order_by(asc(Product.updated_at)).limit(limit).all()

            if not products:
                logger.info("No products to monitor.")
                return

            for product in products:
                try:
                    logger.info(f"Checking: {product.source_url} (Last update: {product.updated_at})")
                    
                    items = []
                    # Choose scraper
                    if product.site == 'yahoo':
                        items = scrape_yahoo(product.source_url, headless=True)
                    elif product.site == 'rakuma':
                        items = scrape_rakuma(product.source_url, headless=True)
                    else:
                        items = scrape_mercari(product.source_url, headless=True)
                    
                    if items:
                        # Save result (this updates last_price, last_status, updated_at)
                        # save_scraped_items_to_db handles logic for logic changes
                        new_c, updated_c = save_scraped_items_to_db(items, user_id=product.user_id, site=product.site)
                        logger.info(f"Updated {product.id}: {updated_c} changes.")
                    else:
                        logger.warning(f"Failed to scrape {product.source_url} or item deleted.")
                        # Optionally mark as error or increment error count
                        
                except Exception as e:
                    logger.error(f"Error checking product {product.id}: {e}")
                    
        except Exception as e:
            logger.error(f"Patrol fatal error: {e}")
        finally:
            session_db.close()
            logger.info("--- Patrol Finished ---")
