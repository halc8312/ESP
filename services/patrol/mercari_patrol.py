"""
Mercari lightweight patrol scraper.
Only fetches price and stock status - no title, description, or images.
"""
import re
import time
import logging
from typing import Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from services.patrol.base_patrol import BasePatrol, PatrolResult

logger = logging.getLogger("patrol.mercari")


class MercariPatrol(BasePatrol):
    """Lightweight Mercari price/stock scraper."""
    
    def fetch(self, url: str, driver=None) -> PatrolResult:
        """
        Fetch price and status from a Mercari product page.
        Does NOT fetch title, description, or images.
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
                time.sleep(1)  # Brief wait for dynamic content
            except Exception:
                pass
            
            # Get body text for status detection
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                body_text = ""
            
            # --- Price extraction ---
            price = self._extract_price(driver, body_text)
            
            # --- Status extraction ---
            status = self._extract_status(driver, body_text)
            
            # --- Variant extraction (if available) ---
            variants = self._extract_variants(driver)
            
            return PatrolResult(
                price=price,
                status=status,
                variants=variants
            )
            
        except Exception as e:
            logger.error(f"Patrol error for {url}: {e}")
            return PatrolResult(error=str(e))
            
        finally:
            if own_driver and driver:
                try:
                    driver.quit()
                except Exception:
                    pass
    
    def _extract_price(self, driver, body_text: str) -> Optional[int]:
        """Extract price from page."""
        # Try data-testid first
        try:
            price_el = driver.find_elements(By.CSS_SELECTOR, "[data-testid='price']")
            if price_el:
                price_text = price_el[0].text
                return self.parse_price(price_text)
        except Exception:
            pass
        
        # Fallback: regex on body text
        if body_text:
            m = re.search(r"[¥￥]\s*([\d,]+)", body_text)
            if m:
                try:
                    return int(m.group(1).replace(",", ""))
                except ValueError:
                    pass
        
        return None
    
    def _extract_status(self, driver, body_text: str) -> str:
        """Extract sale status."""
        # Check for sold indicators
        if "売り切れ" in body_text or "Sold" in body_text:
            return "sold"
        
        # Check purchase button
        try:
            buy_buttons = driver.find_elements(By.CSS_SELECTOR, "button")
            for btn in buy_buttons:
                btn_text = btn.text.lower()
                if "購入" in btn_text or "buy" in btn_text:
                    if btn.is_enabled():
                        return "active"
                    else:
                        return "sold"
        except Exception:
            pass
        
        # Default
        return "active" if body_text else "unknown"
    
    def _extract_variants(self, driver) -> list:
        """
        Extract variant information (for Mercari Shops).
        Regular Mercari items don't have variants.
        """
        variants = []
        
        try:
            # Mercari Shops uses variation selection
            var_labels = driver.find_elements(By.CSS_SELECTOR, "[data-testid='variation-label']")
            for label in var_labels:
                name = label.text.strip()
                # Check if sold out
                is_sold = "売り切れ" in label.get_attribute("innerHTML") or "disabled" in label.get_attribute("class")
                variants.append({
                    "name": name,
                    "stock": 0 if is_sold else 1,
                    "price": None  # Same as main price
                })
        except Exception:
            pass
        
        return variants


# Convenience function
def fetch_mercari(url: str, driver=None) -> PatrolResult:
    """Quick access to Mercari patrol."""
    return MercariPatrol().fetch(url, driver)
