"""
SNKRDUNK lightweight patrol scraper.
Uses Scrapling HTTP fetches only.
"""
import logging

from services.patrol.base_patrol import (
    DELETED_HTTP_STATUSES,
    BasePatrol,
    PatrolResult,
    deleted_http_result,
)
from snkrdunk_db import _parse_detail_page

logger = logging.getLogger("patrol.snkrdunk")


class SnkrdunkPatrol(BasePatrol):
    """Lightweight patrol for snkrdunk.com."""

    def fetch(self, url: str, driver=None) -> PatrolResult:
        """Fetch current price and status. The driver argument is ignored."""
        return self._finalize_result("snkrdunk", url, self._fetch_with_scrapling(url))

    def _fetch_with_scrapling(self, url: str) -> PatrolResult:
        try:
            from services.scraping_client import fetch_marketplace_static

            page = fetch_marketplace_static(
                url,
                site="snkrdunk",
                kind="detail",
                allowed_statuses=DELETED_HTTP_STATUSES,
            )
            missing_result = deleted_http_result(page)
            if missing_result is not None:
                return missing_result
            parsed = _parse_detail_page(page, url)
            price = parsed.get("price")
            detail_status = parsed.get("status", "unknown")
            status = "active" if detail_status == "on_sale" else detail_status

            if price is None and status == "active":
                return PatrolResult(error="No price extracted")

            variants = []
            if price is not None:
                variants.append({"name": "Default Title", "stock": 1 if status == "active" else 0, "price": price})

            return PatrolResult(price=price, status=status, variants=variants)
        except Exception as exc:
            logger.debug("SNKRDUNK patrol error: %s", exc)
            return PatrolResult(error=str(exc))
