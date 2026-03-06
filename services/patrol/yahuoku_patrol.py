"""
Yahoo Auctions Patrol - Lightweight price and status monitoring.
Uses Scrapling HTTP fetch (no browser) for fast, reliable extraction.
Falls back to Selenium when a shared driver is provided.
"""
import re
import json
import logging
from services.patrol.base_patrol import BasePatrol, PatrolResult

logger = logging.getLogger("patrol")


class YahuokuPatrol(BasePatrol):
    """Lightweight patrol for auctions.yahoo.co.jp"""
    
    def fetch(self, url: str, driver=None) -> PatrolResult:
        """
        Fetch current bid price and auction status from Yahoo Auctions.
        Uses Scrapling HTTP-only fetch when driver is None (no Chrome needed).
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
            props = data.get("props", {})
            page_props = props.get("pageProps", {})

            price = None
            status = "active"

            initial_state = page_props.get("initialState", {})
            item_detail = initial_state.get("item", {}).get("detail", {}).get("item", {})

            if item_detail:
                price_data = item_detail.get("price", {})
                if isinstance(price_data, dict):
                    price = price_data.get("current") or price_data.get("bid")
                elif isinstance(price_data, (int, float)):
                    price = int(price_data)
            else:
                initial_props = page_props.get("initialProps", {})
                auction_item = initial_props.get("auctionItem", {})
                if auction_item:
                    price = auction_item.get("currentPrice") or auction_item.get("price")

            # Check page text for ended status
            page_text = str(page.get_all_text())
            if "終了" in page_text or "落札" in page_text:
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
            logger.debug(f"Yahuoku Scrapling patrol error: {e}")
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
            time.sleep(1.5)

            price = None
            status = "active"

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
                    initial_props = page_props.get("initialProps", {})
                    auction_item = initial_props.get("auctionItem", {})
                    if auction_item:
                        price = auction_item.get("currentPrice") or auction_item.get("price")
            except Exception:
                pass

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

            return PatrolResult(price=price, status=status, variants=variants)

        except Exception as e:
            logger.error(f"Yahoo Auctions Selenium patrol error: {e}")
            return PatrolResult(error=str(e))
