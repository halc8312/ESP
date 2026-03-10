"""
Rakuma (fril.jp) lightweight patrol scraper.
Only fetches price and stock status.

Stage 1: Selenium → Playwright (Scrapling StealthyFetcher) migration.
"""
import re
import logging
from typing import Optional

from services.patrol.base_patrol import BasePatrol, PatrolResult

logger = logging.getLogger("patrol.rakuma")


class RakumaPatrol(BasePatrol):
    """Lightweight Rakuma price/stock scraper using Scrapling StealthyFetcher."""
    
    def fetch(self, url: str, driver=None) -> PatrolResult:
        """
        Fetch price and status from a Rakuma product page.

        Playwright（Scrapling StealthyFetcher）を使用してラクマの価格・在庫を取得。
        driver 引数は後方互換のために保持するが、使用しない。
        """
        try:
            from scrapling import StealthyFetcher
            page = StealthyFetcher.fetch(url, headless=True, network_idle=True)
            body_els = page.css("body")
            body_text = body_els[0].text if body_els else ""
            if not body_text:
                for attr_name in ("get_text", "get_all_text"):
                    extractor = getattr(page, attr_name, None)
                    if not callable(extractor):
                        continue
                    try:
                        body_text = str(extractor() or "")
                    except Exception:
                        continue
                    if body_text:
                        break

            # --- Price extraction ---
            price = self._extract_price_from_page(page, body_text)

            # --- Status extraction ---
            status = self._extract_status(body_text)

            return PatrolResult(price=price, status=status, variants=[])

        except Exception as e:
            logger.error(f"Rakuma patrol error for {url}: {e}")
            return PatrolResult(error=str(e))
    
    def _extract_price_from_page(self, page, body_text: str) -> Optional[int]:
        """Extract price from Scrapling page object."""
        # Try common Rakuma selectors
        price_selectors = [
            ".item-price", 
            "[class*='price']",
            ".price"
        ]
        
        for selector in price_selectors:
            try:
                els = page.css(selector)
                for el in els:
                    text = el.text or ""
                    match = re.search(r"([\d,]+)", text)
                    if match:
                        return int(match.group(1).replace(",", ""))
            except Exception:
                continue
        
        # Fallback: regex on body text
        if body_text:
            match = re.search(r"[¥￥]\s*([\d,]+)", body_text)
            if match:
                try:
                    return int(match.group(1).replace(",", ""))
                except ValueError:
                    pass
        
        return None
    
    def _extract_status(self, body_text: str) -> str:
        """Extract sale status."""
        if "売り切れ" in body_text or "SOLD" in body_text.upper():
            return "sold"
        return "active"


def fetch_rakuma(url: str, driver=None) -> PatrolResult:
    """Quick access to Rakuma patrol."""
    return RakumaPatrol().fetch(url, driver)
