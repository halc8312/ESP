"""
Rakuma (fril.jp) lightweight patrol scraper.
Only fetches price and stock status.
"""
import re
import time
import logging
from typing import Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from services.patrol.base_patrol import BasePatrol, PatrolResult

logger = logging.getLogger("patrol.rakuma")


class RakumaPatrol(BasePatrol):
    """Lightweight Rakuma price/stock scraper."""
    
    def fetch(self, url: str, driver=None) -> PatrolResult:
        """
        Fetch price and status from a Rakuma product page.
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
            
            # Get body text
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                body_text = ""
            
            # --- Price extraction ---
            price = self._extract_price(driver, body_text)
            
            # --- Status extraction ---
            status = self._extract_status(body_text)
            
            return PatrolResult(price=price, status=status, variants=[])
            
        except Exception as e:
            logger.error(f"Rakuma patrol error for {url}: {e}")
            return PatrolResult(error=str(e))
            
        finally:
            if own_driver and driver:
                try:
                    driver.quit()
                except Exception:
                    pass
    
    def _extract_price(self, driver, body_text: str) -> Optional[int]:
        """Extract price from page."""
        # Try common Rakuma selectors
        price_selectors = [
            ".item-price", 
            "[class*='price']",
            ".price"
        ]
        
        for selector in price_selectors:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, selector)
                for el in els:
                    text = el.text
                    match = re.search(r"([\d,]+)", text)
                    if match:
                        return int(match.group(1).replace(",", ""))
            except Exception:
                continue
        
        # Fallback: regex on body text
        if body_text:
            match = re.search(r"[¥￥]\s*([\d,]+)", body_text)
            if match:
                try:
                    return int(match.group(1).replace(",", ""))
                except ValueError:
                    pass
        
        return None
    
    def _extract_status(self, body_text: str) -> str:
        """Extract sale status."""
        if "売り切れ" in body_text or "SOLD" in body_text.upper():
            return "sold"
        return "active"


def fetch_rakuma(url: str, driver=None) -> PatrolResult:
    """Quick access to Rakuma patrol."""
    return RakumaPatrol().fetch(url, driver)
