"""
Yahoo Auctions Patrol - Lightweight price and status monitoring
Uses __NEXT_DATA__ JSON for fast, reliable extraction.
"""
import re
import json
import logging
from typing import Optional
from services.patrol.base_patrol import BasePatrol, PatrolResult

logger = logging.getLogger("patrol")


class YahuokuPatrol(BasePatrol):
    """Lightweight patrol for auctions.yahoo.co.jp"""
    
    def fetch(self, url: str, driver=None) -> PatrolResult:
        """
        Fetch current bid price and auction status from Yahoo Auctions.
        Uses __NEXT_DATA__ for speed and reliability.
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
            time.sleep(1.5)
            
            price = None
            status = "active"
            
            # Try __NEXT_DATA__ first
            try:
                script = driver.find_element(By.CSS_SELECTOR, "#__NEXT_DATA__")
                data = json.loads(script.get_attribute("innerHTML"))
                
                props = data.get("props", {})
                page_props = props.get("pageProps", {})
                initial_state = page_props.get("initialState", {})
                item_detail = initial_state.get("item", {}).get("detail", {}).get("item", {})
                
                if item_detail:
                    price_data = item_detail.get("price", {})
                    if isinstance(price_data, dict):
                        price = price_data.get("current") or price_data.get("bid")
                    elif isinstance(price_data, (int, float)):
                        price = int(price_data)
                else:
                    # Alternative path
                    initial_props = page_props.get("initialProps", {})
                    auction_item = initial_props.get("auctionItem", {})
                    if auction_item:
                        price = auction_item.get("currentPrice") or auction_item.get("price")
            except Exception:
                pass
            
            # CSS fallback
            if price is None:
                try:
                    price_els = driver.find_elements(By.CSS_SELECTOR, ".Price__value")
                    for el in price_els:
                        text = el.text.strip()
                        match = re.search(r"([\d,]+)", text)
                        if match:
                            price = int(match.group(1).replace(",", ""))
                            break
                except Exception:
                    pass
            
            # Check if ended
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
                if "終了" in body_text or "落札" in body_text:
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
            logger.error(f"Yahoo Auctions patrol error: {e}")
            return PatrolResult(error=str(e))
            
        finally:
            if own_driver and driver:
                try:
                    driver.quit()
                except Exception:
                    pass
