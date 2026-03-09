"""
Offmall Patrol - Lightweight price and stock monitoring for netmall.hardoff.co.jp.
Uses Scrapling HTTP fetch (no browser) with JSON-LD for fast, reliable extraction.
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
        Uses Scrapling HTTP-only fetch (no browser required).
        driver 引数は後方互換のために保持するが、使用しない。
        """
        return self._fetch_with_scrapling(url)

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
