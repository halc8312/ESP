"""
SNKRDUNK lightweight patrol scraper.
Uses Scrapling HTTP fetch (no browser) for fast price/status extraction.
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
        Fetch current price and status from SNKRDUNK.
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

            price = None
            status = "active"

            # Try __NEXT_DATA__ first (Next.js)
            script_el = page.find("#__NEXT_DATA__")
            if script_el:
                json_text = str(script_el.text or "").strip()
                if json_text:
                    try:
                        data = json.loads(json_text)
                        props = data.get("props", {})
                        page_props = props.get("pageProps", {})

                        item = (
                            page_props.get("item")
                            or page_props.get("product")
                            or page_props.get("initialState", {}).get("item", {})
                            or page_props.get("initialState", {}).get("product", {})
                            or {}
                        )

                        if item:
                            price_raw = item.get("price") or item.get("lowestPrice") or item.get("minPrice")
                            if price_raw is not None:
                                try:
                                    price = int(price_raw)
                                except (ValueError, TypeError):
                                    pass

                            status_flag = item.get("status") or item.get("soldOut") or item.get("isSoldOut")
                            if status_flag in (True, "sold_out", "soldout", "SOLD_OUT"):
                                status = "sold"
                    except (json.JSONDecodeError, Exception) as e:
                        logger.debug(f"SNKRDUNK __NEXT_DATA__ parse error: {e}")
            else:
                logger.debug(f"SNKRDUNK: No __NEXT_DATA__ found for {url}")

            # Try application/ld+json as fallback
            if price is None:
                try:
                    ld_els = page.css("script[type='application/ld+json']")
                    for ld_el in ld_els:
                        ld_text = str(ld_el.text or "").strip()
                        if not ld_text:
                            continue
                        ld_data = json.loads(ld_text)
                        offers = ld_data.get("offers") or ld_data.get("Offers")
                        if offers:
                            if isinstance(offers, list):
                                offers = offers[0]
                            raw = offers.get("price") or offers.get("lowPrice")
                            if raw is not None:
                                try:
                                    price = int(float(str(raw)))
                                    break
                                except (ValueError, TypeError):
                                    pass
                except Exception as e:
                    logger.debug(f"SNKRDUNK ld+json parse error: {e}")

            # CSS selector fallback for price
            if price is None:
                try:
                    css_price_selectors = [
                        ".new-buy-button",
                        "[class*='buy-button']",
                        "[class*='price']",
                        "[class*='Price']",
                    ]
                    for selector in css_price_selectors:
                        el = page.css_first(selector)
                        if el:
                            text = el.text or ""
                            m = re.search(r"[¥￥]\s*([\d,]+)", text) or re.search(r"([\d,]+)", text)
                            if m:
                                price = int(m.group(1).replace(",", ""))
                                break
                except Exception as e:
                    logger.debug(f"SNKRDUNK CSS price fallback error: {e}")

            # Body text fallback for sold-out status
            try:
                page_text = str(page.get_all_text())
                if "SOLD OUT" in page_text or "売り切れ" in page_text or "在庫なし" in page_text:
                    status = "sold"
                if price is None:
                    logger.debug(f"SNKRDUNK page text preview (200 chars): {page_text[:200]}")
            except Exception as e:
                logger.debug(f"SNKRDUNK body text error: {e}")

            if price is None and status == "active":
                return PatrolResult(error="No __NEXT_DATA__ found and no price extracted")

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
                WebDriverWait(driver, 8).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                time.sleep(1.5)
            except Exception:
                pass

            price = None
            status = "active"

            # Try __NEXT_DATA__ first
            try:
                scripts = driver.find_elements(By.CSS_SELECTOR, "script#__NEXT_DATA__")
                if scripts:
                    data = json.loads(scripts[0].get_attribute("textContent") or "{}")
                    props = data.get("props", {})
                    page_props = props.get("pageProps", {})
                    item = (
                        page_props.get("item")
                        or page_props.get("product")
                        or page_props.get("initialState", {}).get("item", {})
                        or page_props.get("initialState", {}).get("product", {})
                        or {}
                    )
                    if item:
                        price_raw = item.get("price") or item.get("lowestPrice") or item.get("minPrice")
                        if price_raw is not None:
                            try:
                                price = int(price_raw)
                            except (ValueError, TypeError):
                                pass
                        status_flag = item.get("status") or item.get("soldOut") or item.get("isSoldOut")
                        if status_flag in (True, "sold_out", "soldout", "SOLD_OUT"):
                            status = "sold"
            except Exception:
                pass

            # CSS fallback for price
            if price is None:
                try:
                    price_selectors = [".new-buy-button", "[class*='buy-button']"]
                    for selector in price_selectors:
                        els = driver.find_elements(By.CSS_SELECTOR, selector)
                        if els:
                            text = els[0].text
                            m = re.search(r"[¥￥]\s*([\d,]+)", text) or re.search(r"([\d,]+)", text)
                            if m:
                                price = int(m.group(1).replace(",", ""))
                                break
                except Exception:
                    pass

            # CSS fallback for status
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
            logger.error(f"SNKRDUNK Selenium patrol error: {e}")
            return PatrolResult(error=str(e))
