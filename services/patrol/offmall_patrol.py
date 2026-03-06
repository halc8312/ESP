"""
Offmall Patrol - Lightweight price and stock monitoring for netmall.hardoff.co.jp.
Uses Scrapling HTTP fetch (no browser) with JSON-LD for fast, reliable extraction.
Falls back to Selenium when a shared driver is provided.
"""
import re
import json
import logging
from services.patrol.base_patrol import BasePatrol, PatrolResult

logger = logging.getLogger("patrol")


class OffmallPatrol(BasePatrol):
    """Lightweight patrol for netmall.hardoff.co.jp"""
    
    def fetch(self, url: str, driver=None) -> PatrolResult:
        """
        Fetch price and stock status from Offmall product page.
        Uses Scrapling HTTP-only fetch when driver is None (no Chrome needed).
        """
        if driver is None:
            return self._fetch_with_scrapling(url)
        return self._fetch_with_selenium(url, driver)

    def _fetch_with_scrapling(self, url: str) -> PatrolResult:
        """HTTP-only fetch using Scrapling Fetcher with JSON-LD extraction."""
        try:
            from services.scraping_client import fetch_static
            page = fetch_static(url)

            price = None
            status = "unknown"

            # Extract JSON-LD (fastest and most reliable)
            scripts = page.css("script[type='application/ld+json']")
            for script_el in scripts:
                try:
                    raw = str(script_el.text or "").strip()
                    if not raw:
                        continue
                    data = json.loads(raw)
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
                except (json.JSONDecodeError, Exception):
                    continue

            # CSS fallback if JSON-LD failed
            if price is None:
                page_text = str(page.get_all_text())
                match = re.search(r"([\d,]+)\s*円", page_text)
                if match:
                    try:
                        price = int(match.group(1).replace(",", ""))
                    except ValueError:
                        pass

            if status == "unknown":
                page_text = str(page.get_all_text())
                if "カートに入れる" in page_text or "購入手続き" in page_text:
                    status = "active"
                elif "対象の商品はございません" in page_text or "ページが見つかりません" in page_text:
                    status = "sold"

            variants = []
            if price is not None:
                variants.append({
                    "name": "Default Title",
                    "stock": 1 if status == "active" else 0,
                    "price": price
                })

            return PatrolResult(price=price, status=status, variants=variants)

        except Exception as e:
            logger.debug(f"Offmall Scrapling patrol error: {e}")
            return PatrolResult(error=str(e))

    def _fetch_with_selenium(self, url: str, driver) -> PatrolResult:
        """Selenium-based fetch using a shared driver."""
        import time
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        try:
            driver.get(url)
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            time.sleep(1)

            price = None
            status = "unknown"

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
                    if cart_btn:
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

            return PatrolResult(price=price, status=status, variants=variants)

        except Exception as e:
            logger.error(f"Offmall Selenium patrol error: {e}")
            return PatrolResult(error=str(e))
