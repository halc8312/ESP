"""
Offmall Patrol - Lightweight price and stock monitoring for netmall.hardoff.co.jp
Uses JSON-LD for fast, reliable extraction.
"""
import re
import json
import logging
from typing import Optional
from services.patrol.base_patrol import BasePatrol, PatrolResult

logger = logging.getLogger("patrol")


class OffmallPatrol(BasePatrol):
    """Lightweight patrol for netmall.hardoff.co.jp"""
    
    def fetch(self, url: str, driver=None) -> PatrolResult:
        """
        Fetch price and stock status from Offmall product page.
        Uses JSON-LD for speed and reliability.
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
            time.sleep(1)
            
            price = None
            status = "unknown"
            
            # Try JSON-LD first (fastest and most reliable)
            try:
                scripts = driver.find_elements(By.CSS_SELECTOR, "script[type='application/ld+json']")
                for script in scripts:
                    try:
                        data = json.loads(script.get_attribute("innerHTML"))
                        if isinstance(data, dict) and data.get("@type") == "Product":
                            offers = data.get("offers", {})
                            if isinstance(offers, dict):
                                price_str = str(offers.get("price", ""))
                                if price_str:
                                    price = int(float(price_str))
                                
                                availability = offers.get("availability", "")
                                if "InStock" in availability:
                                    status = "active"
                                elif "OutOfStock" in availability:
                                    status = "sold"
                            break
                    except json.JSONDecodeError:
                        continue
            except Exception:
                pass
            
            # CSS fallback if JSON-LD failed
            if price is None:
                try:
                    price_el = driver.find_element(By.CSS_SELECTOR, ".product-detail-price__main")
                    price_text = price_el.text.strip()
                    match = re.search(r"([\d,]+)", price_text)
                    if match:
                        price = int(match.group(1).replace(",", ""))
                except Exception:
                    pass
            
            if status == "unknown":
                try:
                    cart_btn = driver.find_elements(By.CSS_SELECTOR, ".cart-add-button")
                    if cart_btn and len(cart_btn) > 0:
                        is_disabled = cart_btn[0].get_attribute("disabled")
                        status = "sold" if is_disabled else "active"
                    else:
                        status = "sold"
                except Exception:
                    pass
            
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
            logger.error(f"Offmall patrol error: {e}")
            return PatrolResult(error=str(e))
            
        finally:
            if own_driver and driver:
                try:
                    driver.quit()
                except Exception:
                    pass
