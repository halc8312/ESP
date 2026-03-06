"""
SNKRDUNK lightweight patrol scraper.
Uses Scrapling HTTP fetch (no browser) for fast price/status extraction via __NEXT_DATA__.
Falls back to Selenium when a shared driver is provided.
"""
import json
import re
import logging

from services.patrol.base_patrol import BasePatrol, PatrolResult

logger = logging.getLogger("patrol.snkrdunk")


class SnkrdunkPatrol(BasePatrol):
    """Lightweight patrol for snkrdunk.com"""

    def fetch(self, url: str, driver=None) -> PatrolResult:
        """
        Fetch price and status from SNKRDUNK.
        Uses Scrapling HTTP-only fetch when driver is None (no Chrome needed).
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

            price = None
            status = "active"

            # Explore multiple common paths for SNKRDUNK Next.js structure
            item = (
                page_props.get("item")
                or page_props.get("product")
                or page_props.get("initialState", {}).get("item", {})
                or page_props.get("initialState", {}).get("product", {})
                or {}
            )

            if item:
                # Price extraction
                price_raw = (
                    item.get("minPrice")
                    or item.get("price")
                    or item.get("lowestPrice")
                    or item.get("currentPrice")
                )
                if price_raw is not None:
                    try:
                        price = int(price_raw)
                    except (ValueError, TypeError):
                        m = re.search(r"([\d,]+)", str(price_raw))
                        if m:
                            price = int(m.group(1).replace(",", ""))

                # Status extraction
                sold_out = (
                    item.get("isSoldOut")
                    or item.get("soldOut")
                    or item.get("status", "") in ("sold_out", "sold", "inactive", "SOLD_OUT")
                    or item.get("stock", 1) == 0
                )
                if sold_out:
                    status = "sold"

            # Fallback: page text indicators
            if price is None or status == "active":
                page_text = str(page.get_all_text())
                if price is None:
                    m = re.search(r"[¥￥]\s*([\d,]+)", page_text) or re.search(r"([\d,]+)\s*円", page_text)
                    if m:
                        try:
                            price = int(m.group(1).replace(",", ""))
                        except ValueError:
                            pass
                if "SOLD OUT" in page_text or "売り切れ" in page_text or "在庫なし" in page_text:
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
            logger.debug(f"SNKRDUNK Scrapling patrol error: {e}")
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
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                time.sleep(2)
            except Exception:
                pass

            price = None
            status = "active"

            # Try __NEXT_DATA__ first (fastest)
            try:
                script = driver.find_element(By.CSS_SELECTOR, "#__NEXT_DATA__")
                data = json.loads(script.get_attribute("innerHTML"))
                page_props = data.get("props", {}).get("pageProps", {})
                item = (
                    page_props.get("item")
                    or page_props.get("product")
                    or page_props.get("initialState", {}).get("item", {})
                    or {}
                )
                if item:
                    price_raw = item.get("minPrice") or item.get("price") or item.get("lowestPrice")
                    if price_raw is not None:
                        try:
                            price = int(price_raw)
                        except (ValueError, TypeError):
                            pass
                    if item.get("isSoldOut") or item.get("soldOut"):
                        status = "sold"
            except Exception:
                pass

            # CSS selector fallback
            if price is None:
                for selector in [".new-buy-button", "[class*='buy-button']", "[class*='price']"]:
                    try:
                        els = driver.find_elements(By.CSS_SELECTOR, selector)
                        if els:
                            m = re.search(r"[¥￥]\s*([\d,]+)", els[0].text) or re.search(r"([\d,]+)", els[0].text)
                            if m:
                                price = int(m.group(1).replace(",", ""))
                                break
                    except Exception:
                        pass

            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text
                if "SOLD OUT" in body_text or "売り切れ" in body_text or "在庫なし" in body_text:
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
            logger.error(f"SNKRDUNK Selenium patrol error for {url}: {e}")
            return PatrolResult(error=str(e))
