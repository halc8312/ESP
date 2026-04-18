"""
Mercari lightweight patrol scraper.

Shares the same page parser as the full Mercari scrape so price/status logic
does not drift.
"""
import logging

from services.mercari_browser_fetch import (
    fetch_mercari_page_and_payloads_via_browser_pool_sync,
    should_use_mercari_browser_pool_patrol,
)
from mercari_db import scrape_shops_product
from services.mercari_item_parser import parse_mercari_item_page, parse_mercari_network_payload
from services.patrol.base_patrol import BasePatrol, PatrolResult
from services.scraping_client import fetch_dynamic

logger = logging.getLogger("patrol.mercari")


class MercariPatrol(BasePatrol):
    """Lightweight Mercari price/stock scraper using the shared parser."""

    # Mercari's SPA hydrates product detail after initial DOM render.
    # ``network_idle=True`` + a selector wait ensures the checkout button
    # and JSON-LD have been injected before we parse, reducing false-sold
    # misclassifications caused by reading half-hydrated pages.
    _WAIT_SELECTOR = "h1, [data-testid='price'], [data-testid='checkout-button']"

    @staticmethod
    def _select_best_payload(captured_payloads: list[dict], url: str) -> tuple[dict, dict]:
        best_item = {}
        best_meta = {"reasons": []}
        best_score = -1

        for captured in captured_payloads or []:
            item, meta = parse_mercari_network_payload(captured.get("payload") or {}, url)
            score = 0
            if item.get("title"):
                score += 4
            if isinstance(item.get("price"), int) and item["price"] > 0:
                score += 3
            if item.get("status") in {"on_sale", "sold", "deleted"}:
                score += 3
            if item.get("image_urls"):
                score += 2
            if item.get("description"):
                score += 1
            if score > best_score:
                best_score = score
                best_item = item
                best_meta = meta

        return best_item, best_meta

    @staticmethod
    def _prefer_payload_fields(dom_item: dict, dom_meta: dict, payload_item: dict, payload_meta: dict) -> tuple[dict, dict]:
        merged_item = dict(dom_item or {})
        merged_meta = dict(dom_meta or {})
        merged_reasons = list(merged_meta.get("reasons") or [])
        merged_sources = dict(merged_meta.get("field_sources") or {})
        payload_reasons = list((payload_meta or {}).get("reasons") or [])

        payload_status = payload_item.get("status")
        if payload_status in {"on_sale", "sold", "deleted"}:
            if merged_item.get("status") != payload_status:
                merged_reasons.append(f"payload-status-preferred:{payload_status}")
            merged_item["status"] = payload_status
            merged_sources["status"] = "payload"
            merged_meta["evidence_strength"] = "hard"
            merged_meta["strategy"] = "payload"

        payload_price = payload_item.get("price")
        if isinstance(payload_price, int) and payload_price > 0:
            if merged_item.get("price") != payload_price:
                merged_reasons.append("payload-price-preferred")
            merged_item["price"] = payload_price
            merged_meta["price_source"] = "payload"
            merged_sources["price"] = "payload"
            merged_meta["confidence"] = "high"
            merged_meta["strategy"] = "payload"

        for reason in payload_reasons:
            if reason not in merged_reasons:
                merged_reasons.append(reason)

        merged_meta["field_sources"] = merged_sources
        merged_meta["reasons"] = merged_reasons
        return merged_item, merged_meta

    def fetch(self, url: str, driver=None) -> PatrolResult:
        try:
            if "/shops/product/" in url:
                item = scrape_shops_product(url)
                meta = dict(item.get("_scrape_meta") or {})
            else:
                captured_payloads = []
                if should_use_mercari_browser_pool_patrol():
                    page, captured_payloads = fetch_mercari_page_and_payloads_via_browser_pool_sync(
                        url,
                        network_idle=True,
                        wait_selector=self._WAIT_SELECTOR,
                    )
                else:
                    page = fetch_dynamic(
                        url,
                        headless=True,
                        # Changed from False → True so the page is more likely
                        # to have finished hydration before we scrape.
                        network_idle=True,
                    )
                item, meta = parse_mercari_item_page(page, url)
                if captured_payloads:
                    payload_item, payload_meta = self._select_best_payload(captured_payloads, url)
                    item, meta = self._prefer_payload_fields(item, meta, payload_item, payload_meta)
            status = self._normalize_status(item.get("status"))
            reason = "; ".join(str(value) for value in meta.get("reasons", []) if value)
            confidence = meta.get("confidence", "low")
            price_source = meta.get("price_source")
            evidence_strength = meta.get("evidence_strength", "none")

            if item.get("status") in {"unknown", "error"}:
                return self._finalize_result("mercari", url, PatrolResult(
                    error=reason or "Mercari page could not be classified",
                    status=status,
                    confidence=confidence,
                    reason=reason,
                    price_source=price_source,
                    evidence_strength=evidence_strength,
                ))

            if item.get("status") == "on_sale" and item.get("price") is None:
                return self._finalize_result("mercari", url, PatrolResult(
                    error=reason or "Active Mercari item missing price",
                    status=status,
                    confidence=confidence,
                    reason=reason,
                    price_source=price_source,
                    evidence_strength=evidence_strength,
                ))

            return self._finalize_result("mercari", url, PatrolResult(
                price=item.get("price"),
                status=status,
                variants=item.get("variants") or [],
                confidence=confidence,
                reason=reason,
                price_source=price_source,
                evidence_strength=evidence_strength,
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
