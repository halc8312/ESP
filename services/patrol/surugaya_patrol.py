"""
Surugaya Patrol - Lightweight price and stock monitoring.
Uses Scrapling HTTP fetch (no browser) when no driver is provided.
"""
import re
import logging
from services.patrol.base_patrol import BasePatrol, PatrolResult

logger = logging.getLogger("patrol")


class SurugayaPatrol(BasePatrol):
    """Lightweight patrol for suruga-ya.jp"""

    # CSS Selectors for patrol (minimal set) - Updated 2026-01-07
    SELECTORS = {
        "price": ".price_group .text-price-detail, .price_group label",
        "stock_available": ".btn_buy, .cart1, #cart-add",
        "stock_sold": ".waitbtn, .soldout, .outofstock",
    }

    def fetch(self, url: str, driver=None) -> PatrolResult:
        """
        Fetch price and stock status from Surugaya product page.
        Uses Scrapling HTTP-only fetch (no browser required).
        driver 引数は後方互換のために保持するが、使用しない。
        """
        return self._fetch_with_scrapling(url)

    def _fetch_with_scrapling(self, url: str) -> PatrolResult:
        """HTTP-only fetch using Scrapling Fetcher - no browser needed."""
        try:
            from services.scraping_client import fetch_static
            from bs4 import BeautifulSoup
            page = fetch_static(url)

            # Use raw HTML for BeautifulSoup parsing (same logic as surugaya_db.py)
            soup = BeautifulSoup(page.body, "html.parser")

            price = None
            # Try CSS selectors via BeautifulSoup
            for selector in self.SELECTORS["price"].split(", "):
                for el in soup.select(selector.strip()):
                    text = el.get_text(strip=True)
                    match = re.search(r"([\d,]+)\s*円", text) or re.search(r"[¥￥]\s*([\d,]+)", text)
                    if match:
                        try:
                            price = int(match.group(1).replace(",", ""))
                            break
                        except ValueError:
                            pass
                if price is not None:
                    break

            # Fallback: search full page text
            if price is None:
                body_text = soup.get_text(" ", strip=True)
                match = re.search(r"([\d,]+)\s*円\s*\(税込\)", body_text)
                if match:
                    try:
                        price = int(match.group(1).replace(",", ""))
                    except ValueError:
                        pass

            # Stock status
            status = "unknown"
            for selector in self.SELECTORS["stock_available"].split(", "):
                if soup.select(selector.strip()):
                    status = "active"
                    break
            if status == "unknown":
                for selector in self.SELECTORS["stock_sold"].split(", "):
                    if soup.select(selector.strip()):
                        status = "sold"
                        break
            if status == "unknown":
                body_text = soup.get_text(" ", strip=True)
                sold_keywords = ("売り切れ", "在庫なし", "品切れ", "販売終了")
                active_keywords = ("カートに入れる", "購入手続き", "注文する")
                if any(k in body_text for k in sold_keywords):
                    status = "sold"
                elif any(k in body_text for k in active_keywords):
                    status = "active"

            variants = []
            if price is not None:
                variants.append({
                    "name": "Default Title",
                    "stock": 1 if status == "active" else 0,
                    "price": price
                })

            return PatrolResult(price=price, status=status, variants=variants)

        except Exception as e:
            logger.debug(f"Surugaya Scrapling patrol error: {e}")
            return PatrolResult(error=str(e))
