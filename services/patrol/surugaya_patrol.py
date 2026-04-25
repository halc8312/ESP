"""
Surugaya patrol scraper.
Uses Scrapling HTTP fetches only.
"""
import json
import logging
import re

from services.patrol.base_patrol import BasePatrol, PatrolResult

logger = logging.getLogger("patrol.surugaya")


class SurugayaPatrol(BasePatrol):
    """Lightweight patrol for suruga-ya.jp."""

    SELECTORS = {
        "price": ".price_group .text-price-detail, .price_group label",
        "stock_available": ".btn_buy, .cart1, #cart-add",
        "stock_sold": ".waitbtn, .soldout, .outofstock",
    }
    ACTIVE_KEYWORDS = ("カートに入れる", "購入手続き", "注文する")
    SOLD_KEYWORDS = ("売り切れ", "在庫なし", "品切れ", "販売終了")

    def fetch(self, url: str, driver=None) -> PatrolResult:
        """Fetch price and stock status. The driver argument is ignored."""
        return self._finalize_result("surugaya", url, self._fetch_with_scrapling(url))

    def _fetch_with_scrapling(self, url: str) -> PatrolResult:
        try:
            from bs4 import BeautifulSoup
            from services.scraping_client import fetch_static

            page = fetch_static(url)
            soup = BeautifulSoup(page.body, "html.parser")

            price = None
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

            body_text = soup.get_text(" ", strip=True)
            if price is None:
                match = re.search(r"([\d,]+)\s*円\s*\(税込\)", body_text)
                if match:
                    try:
                        price = int(match.group(1).replace(",", ""))
                    except ValueError:
                        pass

            status = "unknown"
            for selector in self.SELECTORS["stock_sold"].split(", "):
                if soup.select(selector.strip()):
                    status = "sold"
                    break
            if status == "unknown":
                for selector in self.SELECTORS["stock_available"].split(", "):
                    if soup.select(selector.strip()):
                        status = "active"
                        break
            if status == "unknown":
                if any(keyword in body_text for keyword in self.SOLD_KEYWORDS):
                    status = "sold"
                elif any(keyword in body_text for keyword in self.ACTIVE_KEYWORDS):
                    status = "active"
            if status == "unknown":
                for script in soup.select("script[type='application/ld+json']"):
                    raw = script.string or script.get_text()
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw.strip())
                    except Exception:
                        continue

                    nodes = payload if isinstance(payload, list) else payload.get("@graph", [payload]) if isinstance(payload, dict) else []
                    for node in nodes:
                        if not isinstance(node, dict):
                            continue
                        offers = node.get("offers")
                        if isinstance(offers, list) and offers:
                            offers = offers[0]
                        if not isinstance(offers, dict):
                            continue
                        availability = str(offers.get("availability") or "").lower()
                        if "outofstock" in availability or "soldout" in availability:
                            status = "sold"
                            break
                        if "instock" in availability:
                            status = "active"
                            break
                    if status != "unknown":
                        break

            variants = []
            if price is not None:
                variants.append({"name": "Default Title", "stock": 1 if status == "active" else 0, "price": price})

            return PatrolResult(price=price, status=status, variants=variants)
        except Exception as exc:
            logger.debug("Surugaya patrol error: %s", exc)
            return PatrolResult(error=str(exc))
