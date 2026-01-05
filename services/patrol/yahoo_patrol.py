"""
Yahoo Shopping lightweight patrol scraper.
Uses __NEXT_DATA__ JSON parsing for fast price/variant extraction.
"""
import json
import re
import time
import logging
from typing import Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from services.patrol.base_patrol import BasePatrol, PatrolResult

logger = logging.getLogger("patrol.yahoo")


class YahooPatrol(BasePatrol):
    """Lightweight Yahoo Shopping price/stock scraper using __NEXT_DATA__."""
    
    def fetch(self, url: str, driver=None) -> PatrolResult:
        """
        Fetch price and variant stock from Yahoo Shopping.
        Uses __NEXT_DATA__ JSON for fast extraction.
        """
        own_driver = False
        
        try:
            if driver is None:
                from mercari_db import create_driver
                driver = create_driver(headless=True)
                own_driver = True
            
            driver.get(url)
            
            # Wait for page load
            try:
                WebDriverWait(driver, 8).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                time.sleep(0.5)
            except Exception:
                pass
            
            # Try __NEXT_DATA__ JSON extraction (fastest)
            result = self._extract_from_next_data(driver)
            if result.success:
                return result
            
            # Fallback to CSS selectors
            return self._extract_from_css(driver)
            
        except Exception as e:
            logger.error(f"Yahoo patrol error for {url}: {e}")
            return PatrolResult(error=str(e))
            
        finally:
            if own_driver and driver:
                try:
                    driver.quit()
                except Exception:
                    pass
    
    def _extract_from_next_data(self, driver) -> PatrolResult:
        """Extract data from __NEXT_DATA__ JSON script tag."""
        try:
            scripts = driver.find_elements(By.CSS_SELECTOR, "script[id='__NEXT_DATA__']")
            if not scripts:
                return PatrolResult(error="No __NEXT_DATA__ found")
            
            json_text = scripts[0].get_attribute("textContent")
            if not json_text:
                return PatrolResult(error="Empty __NEXT_DATA__")
            
            data = json.loads(json_text)
            
            # Navigate to item data
            page_props = data.get("props", {}).get("pageProps", {})
            sp = page_props.get("sp", {}) or page_props.get("initialState", {})
            item = sp.get("item", {}) or sp.get("product", {})
            
            if not item:
                return PatrolResult(error="No item data in JSON")
            
            # --- Price ---
            price = None
            if item.get("applicablePrice"):
                price = int(item.get("applicablePrice"))
            elif item.get("price"):
                price = int(item.get("price"))
            
            # --- Status ---
            status = "active"
            stock = item.get("stock", {})
            if isinstance(stock, dict):
                if stock.get("isSoldOut") or stock.get("quantity", 1) <= 0:
                    status = "sold"
            
            # --- Variants ---
            variants = self._extract_variants_from_json(item, price)
            
            return PatrolResult(price=price, status=status, variants=variants)
            
        except Exception as e:
            logger.debug(f"__NEXT_DATA__ parse error: {e}")
            return PatrolResult(error=str(e))
    
    def _extract_variants_from_json(self, item: dict, base_price: Optional[int]) -> list:
        """Extract variant stock from JSON item data."""
        variants = []
        
        # Two-axis variants
        if isinstance(item.get("stockTableTwoAxis"), dict):
            two_axis = item.get("stockTableTwoAxis")
            first_opt = two_axis.get("firstOption", {})
            
            for opt1 in first_opt.get("choiceList", []):
                v_name1 = opt1.get("choiceName")
                sec_opt = opt1.get("secondOption", {})
                
                for opt2 in sec_opt.get("choiceList", []):
                    v_name2 = opt2.get("choiceName")
                    stock_info = opt2.get("stock", {})
                    qty = stock_info.get("quantity", 0)
                    v_price = opt2.get("price") or base_price
                    
                    if v_name1 and v_name2:
                        variants.append({
                            "name": f"{v_name1} / {v_name2}",
                            "stock": qty,
                            "price": v_price
                        })
        
        # One-axis variants
        elif isinstance(item.get("stockTableOneAxis"), dict):
            one_axis = item.get("stockTableOneAxis")
            first_opt = one_axis.get("firstOption", {})
            
            for opt1 in first_opt.get("choiceList", []):
                v_name = opt1.get("choiceName")
                stock_info = opt1.get("stock", {})
                qty = stock_info.get("quantity", 0)
                v_price = opt1.get("price") or base_price
                
                if v_name:
                    variants.append({
                        "name": v_name,
                        "stock": qty,
                        "price": v_price
                    })
        
        return variants
    
    def _extract_from_css(self, driver) -> PatrolResult:
        """Fallback CSS selector extraction."""
        try:
            body_text = driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            body_text = ""
        
        # Price
        price = None
        match = re.search(r"([\d,]+)\s*円", body_text)
        if match:
            try:
                price = int(match.group(1).replace(",", ""))
            except ValueError:
                pass
        
        # Status
        status = "active"
        if "売り切れ" in body_text or "在庫切れ" in body_text:
            status = "sold"
        
        return PatrolResult(price=price, status=status, variants=[])


def fetch_yahoo(url: str, driver=None) -> PatrolResult:
    """Quick access to Yahoo patrol."""
    return YahooPatrol().fetch(url, driver)
