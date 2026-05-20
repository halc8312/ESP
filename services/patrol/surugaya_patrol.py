"""
Surugaya patrol scraper.
Uses Scrapling HTTP fetches only.
"""
import json
import logging
import re

import surugaya_db
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

    @staticmethod
    def _status_for_patrol(status: str) -> str:
        normalized = str(status or "").strip().lower()
        if normalized == "on_sale":
            return "active"
        if normalized in {"active", "sold", "deleted", "unknown"}:
            return normalized
        return "unknown"

    @staticmethod
    def _result_from_detail_item(item: dict) -> PatrolResult:
        status = SurugayaPatrol._status_for_patrol(item.get("status"))
        price = item.get("price")
        confidence = "high"
        reason = None
        if status == "active" and price is None:
            confidence = "low"
            reason = "active-without-price"

        variants = []
        raw_variants = item.get("variants") or []
        if isinstance(raw_variants, list):
            for raw in raw_variants:
                if not isinstance(raw, dict):
                    continue
                variant_price = raw.get("price", price)
                variants.append(
                    {
                        "name": raw.get("option1_value") or raw.get("name") or item.get("condition") or "Default Title",
                        "stock": int(raw.get("inventory_qty") or 0),
                        "price": variant_price,
                    }
                )

        if not variants and price is not None:
            variants.append(
                {
                    "name": item.get("condition") or "Default Title",
                    "stock": 1 if status == "active" else 0,
                    "price": price,
                }
            )

        return PatrolResult(price=price, status=status, variants=variants, confidence=confidence, reason=reason)

    def _fallback_to_full_detail(self, url: str, reason: str) -> PatrolResult:
        try:
            items = surugaya_db.scrape_single_item(url, headless=True)
        except Exception as exc:
            logger.debug("Surugaya full-detail patrol fallback failed: %s", exc)
            return PatrolResult(error=f"{reason}; fallback failed: {exc}", confidence="low", reason=reason)
        if not items:
            return PatrolResult(error=reason, confidence="low", reason=reason)
        return self._result_from_detail_item(items[0])

    def _fetch_with_scrapling(self, url: str) -> PatrolResult:
        try:
            from bs4 import BeautifulSoup
            from services.scraping_client import fetch_static

            page = fetch_static(url)
            soup = BeautifulSoup(page.body, "html.parser")
            status_code = int(getattr(page, "status", 200) or 200)
            if status_code == 404:
                return PatrolResult(status="deleted", confidence="high", reason="http-404")
            if status_code >= 400:
                return self._fallback_to_full_detail(url, f"http-{status_code}")
            if surugaya_db._looks_like_challenge_soup(soup):
                return self._fallback_to_full_detail(url, "challenge-page")

            ld_product = surugaya_db._extract_json_ld_product(soup)
            degraded_marker = surugaya_db._looks_like_degraded_detail_page(soup, ld_product=ld_product)
            if degraded_marker:
                if surugaya_db._is_maintenance_marker(degraded_marker):
                    reason = f"degraded-marker:{degraded_marker}"
                    return PatrolResult(error=reason, confidence="low", reason=reason)
                return self._fallback_to_full_detail(url, f"degraded-marker:{degraded_marker}")

            price = None
            price_source = None
            ld_price = ld_product.get("price")
            if isinstance(ld_price, int) and ld_price > 0:
                price = ld_price
                price_source = "json_ld"

            for selector in self.SELECTORS["price"].split(", "):
                for el in soup.select(selector.strip()):
                    text = el.get_text(strip=True)
                    match = re.search(r"([\d,]+)\s*円", text) or re.search(r"[¥￥]\s*([\d,]+)", text)
                    if match:
                        try:
                            price = int(match.group(1).replace(",", ""))
                            price_source = "css"
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
                        price_source = "body"
                    except ValueError:
                        pass

            status = self._status_for_patrol(surugaya_db._extract_status(soup, ld_product))
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

            condition = surugaya_db._extract_condition(soup)
            variants = []
            if price is not None:
                variants.append({"name": condition or "Default Title", "stock": 1 if status == "active" else 0, "price": price})

            if status == "active" and price is None:
                return self._fallback_to_full_detail(url, "active-without-price")

            return PatrolResult(price=price, status=status, variants=variants, price_source=price_source)
        except Exception as exc:
            logger.debug("Surugaya patrol error: %s", exc)
            return self._fallback_to_full_detail(url, str(exc))
