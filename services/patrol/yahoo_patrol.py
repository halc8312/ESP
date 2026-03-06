"""
Yahoo Shopping lightweight patrol scraper.
Uses Scrapling HTTP fetch (no browser) for fast price/variant extraction.
Falls back to Selenium if a shared driver is provided.
"""
import json
import re
import logging
from typing import Optional

from services.patrol.base_patrol import BasePatrol, PatrolResult

logger = logging.getLogger("patrol.yahoo")


class YahooPatrol(BasePatrol):
    """Lightweight Yahoo Shopping price/stock scraper."""
    
    def fetch(self, url: str, driver=None) -> PatrolResult:
        """
        Fetch price and variant stock from Yahoo Shopping.
        Uses Scrapling HTTP-only fetch (no browser) when driver is None.
        Falls back to Selenium extraction when a shared driver is provided.
        """
        if driver is None:
            return self._fetch_with_scrapling(url)
        return self._fetch_with_selenium(url, driver)

    def _fetch_with_scrapling(self, url: str) -> PatrolResult:
        """HTTP-only fetch using Scrapling Fetcher - no browser needed."""
        try:
            from services.scraping_client import fetch_static
            page = fetch_static(url)

            script_el = page.find("#__NEXT_DATA__")
            if not script_el:
                return PatrolResult(error="No __NEXT_DATA__ found")

            json_text = str(script_el.text or "").strip()
            if not json_text:
                return PatrolResult(error="Empty __NEXT_DATA__")

            data = json.loads(json_text)
            page_props = data.get("props", {}).get("pageProps", {})
            sp = page_props.get("sp", {}) or page_props.get("initialState", {})
            item = sp.get("item", {}) or sp.get("product", {})

            if not item:
                return PatrolResult(error="No item data in JSON")

            price = None
            if item.get("applicablePrice"):
                price = int(item["applicablePrice"])
            elif item.get("price"):
                price = int(item["price"])

            status = "active"
            stock = item.get("stock", {})
            if isinstance(stock, dict):
                if stock.get("isSoldOut") or stock.get("quantity", 1) <= 0:
                    status = "sold"

            variants = self._extract_variants_from_json(item, price)
            return PatrolResult(price=price, status=status, variants=variants)

        except Exception as e:
            logger.debug(f"Yahoo Scrapling patrol error: {e}")
            return PatrolResult(error=str(e))

    def _fetch_with_selenium(self, url: str, driver) -> PatrolResult:
        """Selenium-based fetch using a shared driver."""
        import time
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        try:
            driver.get(url)
            try:
                WebDriverWait(driver, 8).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                time.sleep(0.5)
            except Exception:
                pass

            result = self._extract_from_next_data(driver)
            if result.success:
                return result
            return self._extract_from_css(driver)

        except Exception as e:
            logger.error(f"Yahoo Selenium patrol error for {url}: {e}")
            return PatrolResult(error=str(e))

    def _extract_from_next_data(self, driver) -> PatrolResult:
        """Extract data from __NEXT_DATA__ JSON script tag (Selenium)."""
        try:
            from selenium.webdriver.common.by import By
            scripts = driver.find_elements(By.CSS_SELECTOR, "script[id='__NEXT_DATA__']")
            if not scripts:
                return PatrolResult(error="No __NEXT_DATA__ found")

            json_text = scripts[0].get_attribute("textContent")
            if not json_text:
                return PatrolResult(error="Empty __NEXT_DATA__")

            data = json.loads(json_text)
            page_props = data.get("props", {}).get("pageProps", {})
            sp = page_props.get("sp", {}) or page_props.get("initialState", {})
            item = sp.get("item", {}) or sp.get("product", {})

            if not item:
                return PatrolResult(error="No item data in JSON")

            price = None
            if item.get("applicablePrice"):
                price = int(item.get("applicablePrice"))
            elif item.get("price"):
                price = int(item.get("price"))

            status = "active"
            stock = item.get("stock", {})
            if isinstance(stock, dict):
                if stock.get("isSoldOut") or stock.get("quantity", 1) <= 0:
                    status = "sold"

            variants = self._extract_variants_from_json(item, price)
            return PatrolResult(price=price, status=status, variants=variants)

        except Exception as e:
            logger.debug(f"__NEXT_DATA__ parse error: {e}")
            return PatrolResult(error=str(e))
    
    def _extract_variants_from_json(self, item: dict, base_price: Optional[int]) -> list:
        """Extract variant stock from JSON item data."""
        variants = []
        
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
        """Fallback CSS selector extraction (Selenium)."""
        try:
            from selenium.webdriver.common.by import By
            body_text = driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            body_text = ""

        price = None
        match = re.search(r"([\d,]+)\s*円", body_text)
        if match:
            try:
                price = int(match.group(1).replace(",", ""))
            except ValueError:
                pass

        status = "active"
        if "売り切れ" in body_text or "在庫切れ" in body_text:
            status = "sold"

        return PatrolResult(price=price, status=status, variants=[])


def fetch_yahoo(url: str, driver=None) -> PatrolResult:
    """Quick access to Yahoo patrol."""
    return YahooPatrol().fetch(url, driver)
