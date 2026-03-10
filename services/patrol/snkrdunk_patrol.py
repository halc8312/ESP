"""
SNKRDUNK lightweight patrol scraper.
Uses Scrapling HTTP fetches only.
"""
import json
import logging
import re

from services.patrol.base_patrol import BasePatrol, PatrolResult

logger = logging.getLogger("patrol.snkrdunk")


class SnkrdunkPatrol(BasePatrol):
    """Lightweight patrol for snkrdunk.com."""

    def fetch(self, url: str, driver=None) -> PatrolResult:
        """Fetch current price and status. The driver argument is ignored."""
        return self._fetch_with_scrapling(url)

    def _fetch_with_scrapling(self, url: str) -> PatrolResult:
        try:
            from services.scraping_client import fetch_static

            page = fetch_static(url)
            price = None
            status = "active"

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
                    except (json.JSONDecodeError, Exception) as exc:
                        logger.debug("SNKRDUNK __NEXT_DATA__ parse error: %s", exc)

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
                except Exception as exc:
                    logger.debug("SNKRDUNK ld+json parse error: %s", exc)

            if price is None:
                css_price_selectors = [
                    ".new-buy-button",
                    "[class*='buy-button']",
                    "[class*='price']",
                    "[class*='Price']",
                ]
                for selector in css_price_selectors:
                    el = page.css_first(selector)
                    if not el:
                        continue
                    text = el.text or ""
                    match = re.search(r"[¥￥]\s*([\d,]+)", text) or re.search(r"([\d,]+)", text)
                    if match:
                        price = int(match.group(1).replace(",", ""))
                        break

            page_text = str(page.get_all_text())
            if "SOLD OUT" in page_text or "売り切れ" in page_text or "在庫なし" in page_text:
                status = "sold"

            if price is None and status == "active":
                return PatrolResult(error="No price extracted")

            variants = []
            if price is not None:
                variants.append({"name": "Default Title", "stock": 1 if status == "active" else 0, "price": price})

            return PatrolResult(price=price, status=status, variants=variants)
        except Exception as exc:
            logger.debug("SNKRDUNK patrol error: %s", exc)
            return PatrolResult(error=str(exc))
