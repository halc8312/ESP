"""
Mercari lightweight patrol scraper.

Shares the same page parser as the full Mercari scrape so price/status logic
does not drift.
"""
import logging

from services.mercari_browser_fetch import (
    fetch_mercari_page_via_browser_pool_sync,
    should_use_mercari_browser_pool_patrol,
)
from services.mercari_item_parser import parse_mercari_item_page
from services.patrol.base_patrol import BasePatrol, PatrolResult
from services.scraping_client import fetch_dynamic

logger = logging.getLogger("patrol.mercari")


class MercariPatrol(BasePatrol):
    """Lightweight Mercari price/stock scraper using the shared parser."""

    def fetch(self, url: str, driver=None) -> PatrolResult:
        try:
            if should_use_mercari_browser_pool_patrol():
                page = fetch_mercari_page_via_browser_pool_sync(url, network_idle=False)
            else:
                page = fetch_dynamic(
                    url,
                    headless=True,
                    network_idle=False,
                )
            item, meta = parse_mercari_item_page(page, url)
            status = self._normalize_status(item.get("status"))
            reason = "; ".join(str(value) for value in meta.get("reasons", []) if value)
            confidence = meta.get("confidence", "low")
            price_source = meta.get("price_source")

            if item.get("status") in {"unknown", "error"}:
                return self._finalize_result("mercari", url, PatrolResult(
                    error=reason or "Mercari page could not be classified",
                    status=status,
                    confidence=confidence,
                    reason=reason,
                    price_source=price_source,
                ))

            if item.get("status") == "on_sale" and item.get("price") is None:
                return self._finalize_result("mercari", url, PatrolResult(
                    error=reason or "Active Mercari item missing price",
                    status=status,
                    confidence=confidence,
                    reason=reason,
                    price_source=price_source,
                ))

            return self._finalize_result("mercari", url, PatrolResult(
                price=item.get("price"),
                status=status,
                variants=item.get("variants") or [],
                confidence=confidence,
                reason=reason,
                price_source=price_source,
            ))

        except Exception as exc:
            logger.error("Patrol error for %s: %s", url, exc)
            return self._finalize_result("mercari", url, PatrolResult(error=str(exc), confidence="low", reason=str(exc)))

    @staticmethod
    def _normalize_status(status: str | None) -> str:
        if status == "on_sale":
            return "active"
        if status in {"sold", "deleted"}:
            return status
        return "unknown"


def fetch_mercari(url: str, driver=None) -> PatrolResult:
    """Quick access to Mercari patrol."""
    return MercariPatrol().fetch(url, driver)
