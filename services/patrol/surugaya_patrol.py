"""
Surugaya patrol scraper.
Uses Scrapling HTTP fetches only.
"""
import json
import logging
import re

from services.patrol.base_patrol import BasePatrol, PatrolResult

logger = logging.getLogger("patrol.surugaya")

_BLOCK_HTTP_STATUSES = {403, 429, 503}
_BLOCK_MARKERS = (
    "just a moment...",
    "challenges.cloudflare.com",
    "cf-chl",
    "attention required! | cloudflare",
)


def _response_status(page) -> int | None:
    try:
        return int(page.status)
    except (AttributeError, TypeError, ValueError):
        return None


def _body_text(page) -> str:
    body = page.body
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="ignore")
    return str(body or "")


def _parse_price(value) -> int | None:
    if value is None:
        return None
    match = re.search(r"([\d,]+)", str(value))
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _json_ld_nodes(payload) -> list[dict]:
    if isinstance(payload, list):
        return [node for node in payload if isinstance(node, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("@graph"), list):
        return [node for node in payload["@graph"] if isinstance(node, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def _extract_json_ld_offer_fields(soup) -> tuple[int | None, str | None]:
    price = None
    status = None
    for script in soup.select("script[type='application/ld+json']"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            payload = json.loads(raw.strip())
        except Exception:
            continue

        for node in _json_ld_nodes(payload):
            offers = node.get("offers")
            if isinstance(offers, dict):
                offer_nodes = [offers]
            elif isinstance(offers, list):
                offer_nodes = [offer for offer in offers if isinstance(offer, dict)]
            else:
                offer_nodes = []

            for offer in offer_nodes:
                if price is None:
                    price = _parse_price(offer.get("price") or offer.get("lowPrice"))
                if status is None:
                    availability = str(offer.get("availability") or "").lower()
                    if "outofstock" in availability or "soldout" in availability:
                        status = "sold"
                    elif "instock" in availability:
                        status = "active"
                if price is not None and status is not None:
                    return price, status
    return price, status


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
            response_status = _response_status(page)
            html = _body_text(page)
            lowered_html = html.lower()
            blocked = response_status in _BLOCK_HTTP_STATUSES or any(marker in lowered_html for marker in _BLOCK_MARKERS)
            if blocked:
                return PatrolResult(
                    status="blocked",
                    error=f"HTTP {response_status}" if response_status in _BLOCK_HTTP_STATUSES else "challenge_page",
                    confidence="low",
                    reason="blocked_http_status" if response_status in _BLOCK_HTTP_STATUSES else "blocked_challenge_page",
                )
            if response_status is not None and response_status >= 400:
                return PatrolResult(
                    status="error",
                    error=f"HTTP {response_status}",
                    confidence="low",
                    reason="http_status",
                )

            soup = BeautifulSoup(html, "html.parser")
            json_ld_price, json_ld_status = _extract_json_ld_offer_fields(soup)

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
            if price is None:
                price = json_ld_price

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
            if status == "unknown" and json_ld_status:
                status = json_ld_status

            variants = []
            if price is not None:
                variants.append({"name": "Default Title", "stock": 1 if status == "active" else 0, "price": price})

            return PatrolResult(price=price, status=status, variants=variants)
        except Exception as exc:
            logger.debug("Surugaya patrol error: %s", exc)
            return PatrolResult(error=str(exc))
