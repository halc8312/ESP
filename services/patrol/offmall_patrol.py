"""
Offmall patrol scraper.
Uses Scrapling HTTP fetches only.
"""
import logging

from offmall_db import _extract_json_ld_product, _extract_visible_price, _get_page_text, _infer_offmall_status
from services.patrol.base_patrol import BasePatrol, PatrolResult

logger = logging.getLogger("patrol.offmall")


class OffmallPatrol(BasePatrol):
    """Lightweight patrol for netmall.hardoff.co.jp."""

    def fetch(self, url: str, driver=None) -> PatrolResult:
        """Fetch price and stock status. The driver argument is ignored."""
        return self._finalize_result("offmall", url, self._fetch_with_scrapling(url))

    def _fetch_with_scrapling(self, url: str) -> PatrolResult:
        try:
            from services.scraping_client import fetch_static

            page = fetch_static(url)
            page_text = _get_page_text(page)
            json_ld = _extract_json_ld_product(page)
            offers = json_ld.get("offers", {}) if isinstance(json_ld, dict) else {}

            price = _extract_visible_price(page, page_text)
            if price is None and isinstance(offers, dict):
                raw_price = offers.get("price")
                if raw_price is not None:
                    try:
                        price = int(float(str(raw_price)))
                    except ValueError:
                        price = None

            status = _infer_offmall_status(page_text, offers)

            variants = []
            if price is not None:
                variants.append({"name": "Default Title", "stock": 1 if status == "active" else 0, "price": price})

            return PatrolResult(price=price, status=status, variants=variants)
        except Exception as exc:
            logger.debug("Offmall patrol error: %s", exc)
            return PatrolResult(error=str(exc))
