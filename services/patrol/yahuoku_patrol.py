"""
Yahoo Auctions patrol scraper.
Uses Scrapling HTTP fetches only.
"""
import logging

from services.patrol.base_patrol import BasePatrol, PatrolResult
from yahuoku_db import (
    _extract_auction_item,
    _extract_tax_inclusive_price,
    _get_page_text,
    _infer_auction_status,
)

logger = logging.getLogger("patrol.yahuoku")


class YahuokuPatrol(BasePatrol):
    """Lightweight patrol for auctions.yahoo.co.jp."""

    def fetch(self, url: str, driver=None) -> PatrolResult:
        """Fetch current bid price and auction status. The driver argument is ignored."""
        return self._finalize_result("yahuoku", url, self._fetch_with_scrapling(url))

    def _fetch_with_scrapling(self, url: str) -> PatrolResult:
        try:
            from services.scraping_client import fetch_static

            page = fetch_static(url)
            item_detail = _extract_auction_item(page)
            if not item_detail:
                return PatrolResult(error="No auction item data found")

            page_text = _get_page_text(page)
            price = _extract_tax_inclusive_price(item_detail, page_text)
            status = _infer_auction_status(item_detail, page_text)

            variants = []
            if price is not None:
                variants.append({"name": "Default Title", "stock": 1 if status == "active" else 0, "price": price})

            return PatrolResult(price=price, status=status, variants=variants)
        except Exception as exc:
            logger.debug("Yahuoku patrol error: %s", exc)
            return PatrolResult(error=str(exc))
