"""
Rakuma (fril.jp) lightweight patrol scraper.
Only fetches price and stock status.

Uses Rakuma's SSR item pages over HTTP to avoid browser startup on Render.
"""
import logging

from services.patrol.base_patrol import BasePatrol, PatrolResult
from services.rakuma_item_parser import (
    extract_rakuma_page_text,
    is_rakuma_missing_item_page,
    parse_rakuma_item_page,
)
from services.scraping_client import fetch_static

logger = logging.getLogger("patrol.rakuma")


class RakumaPatrol(BasePatrol):
    """Lightweight Rakuma price/stock scraper using HTTP fetches."""
    
    def fetch(self, url: str, driver=None) -> PatrolResult:
        """
        Fetch price and status from a Rakuma product page.

        item.fril.jp は SSR のため、HTTP fetch で価格・在庫を取得する。
        driver 引数は後方互換のために保持するが、使用しない。
        """
        try:
            page = fetch_static(url, follow_redirects=True)
            http_status = self._extract_http_status(page)
            body_text = extract_rakuma_page_text(page)

            if http_status == 404:
                return self._finalize_result("rakuma", url, PatrolResult(error="Rakuma item unavailable (404)"))

            if is_rakuma_missing_item_page(body_text):
                return self._finalize_result("rakuma", url, PatrolResult(error="Rakuma item unavailable"))

            parsed = parse_rakuma_item_page(page, url, body_text=body_text)
            status = self._normalize_status(parsed.get("status"))

            if not parsed.get("title") and parsed.get("price") is None and status != "sold":
                return self._finalize_result("rakuma", url, PatrolResult(error="Rakuma page missing expected item data"))

            return self._finalize_result("rakuma", url, PatrolResult(price=parsed.get("price"), status=status, variants=[]))

        except Exception as e:
            logger.error(f"Rakuma patrol error for {url}: {e}")
            return self._finalize_result("rakuma", url, PatrolResult(error=str(e)))

    @staticmethod
    def _extract_http_status(page) -> int:
        raw_status = getattr(page, "status", None)
        if isinstance(raw_status, int):
            return raw_status
        if isinstance(raw_status, str):
            try:
                return int(raw_status)
            except ValueError:
                return 200
        return 200

    @staticmethod
    def _normalize_status(status: str | None) -> str:
        if status == "sold":
            return "sold"
        if status in {"on_sale", "active"}:
            return "active"
        return "unknown"


def fetch_rakuma(url: str, driver=None) -> PatrolResult:
    """Quick access to Rakuma patrol."""
    return RakumaPatrol().fetch(url, driver)
