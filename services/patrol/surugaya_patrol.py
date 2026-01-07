"""
Surugaya Patrol - Lightweight price and stock monitoring
"""
import re
import logging
from typing import Optional
from services.patrol.base_patrol import BasePatrol, PatrolResult

logger = logging.getLogger("patrol")


class SurugayaPatrol(BasePatrol):
    """Lightweight patrol for suruga-ya.jp"""
    
    # CSS Selectors for patrol (minimal set) - Updated 2026-01-07
    SELECTORS = {
        "price": ".price_group .text-price-detail, .price_group label",
        "stock_available": ".btn_buy, .cart1",  # Both selectors work
        "stock_sold": ".waitbtn",
    }
    
    def fetch(self, url: str, driver=None) -> PatrolResult:
        """
        Fetch price and stock status from Surugaya product page.
        Optimized for speed - only extracts essential data.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        import time
        
        own_driver = False
        
        try:
            if driver is None:
                from mercari_db import create_driver
                driver = create_driver(headless=True)
                own_driver = True
            
            driver.get(url)
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            time.sleep(1)  # Short wait for dynamic content
            
            # ---- Price ----
            price = None
            try:
                price_els = driver.find_elements(By.CSS_SELECTOR, self.SELECTORS["price"])
                for el in price_els:
                    text = el.text
                    match = re.search(r"([\d,]+)\s*円", text)
                    if match:
                        price = int(match.group(1).replace(",", ""))
                        break
            except Exception:
                pass
            
            # Fallback
            if price is None:
                try:
                    body_text = driver.find_element(By.TAG_NAME, "body").text
                    match = re.search(r"([\d,]+)\s*円\s*\(税込\)", body_text)
                    if match:
                        price = int(match.group(1).replace(",", ""))
                except Exception:
                    pass
            
            # ---- Stock Status ----
            status = "unknown"
            try:
                buy_btn = driver.find_elements(By.CSS_SELECTOR, self.SELECTORS["stock_available"])
                sold_btn = driver.find_elements(By.CSS_SELECTOR, self.SELECTORS["stock_sold"])
                
                if buy_btn and len(buy_btn) > 0:
                    status = "active"
                elif sold_btn and len(sold_btn) > 0:
                    status = "sold"
                else:
                    body_text = driver.find_element(By.TAG_NAME, "body").text
                    if "品切れ" in body_text:
                        status = "sold"
                    else:
                        status = "active"
            except Exception:
                pass
            
            # Create variant for stock tracking
            variants = []
            if price is not None:
                variants.append({
                    "name": "Default Title",
                    "stock": 1 if status == "active" else 0,
                    "price": price
                })
            
            return PatrolResult(
                price=price,
                status=status,
                variants=variants
            )
            
        except Exception as e:
            logger.error(f"Surugaya patrol error: {e}")
            return PatrolResult(error=str(e))
            
        finally:
            if own_driver and driver:
                try:
                    driver.quit()
                except Exception:
                    pass
