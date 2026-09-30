"""RecordCity patrol using one guarded, identity-checked detail-page fetch."""
from __future__ import annotations

import recordcity_db
from services.patrol.base_patrol import BasePatrol, PatrolResult, deleted_http_error_result
from services.scrape_safety import ScrapeBlockedError, validate_marketplace_url


class RecordCityPatrol(BasePatrol):
    """Observe price and stock without storing full details or downloading images."""

    def fetch(self, url: str, driver=None) -> PatrolResult:
        return self._finalize_result("recordcity", url, self._fetch(url))

    @staticmethod
    def _uncertain(reason: str) -> PatrolResult:
        return PatrolResult(error=f"RecordCity {reason}", confidence="low", reason=reason)

    def _fetch(self, url: str) -> PatrolResult:
        try:
            url = validate_marketplace_url(url, "recordcity", kind="detail")
            page = recordcity_db._fetch_page(url, kind="detail")
            product = recordcity_db._extract_json_ld_product(
                page, expected_sku=recordcity_db._catalog_id(url),
            )
            if not product or not isinstance(product.get("name"), str) or not product["name"].strip():
                return self._uncertain("unverified_product_identity")
            offers = product.get("offers")
            if isinstance(offers, list) and len(offers) == 1:
                offers = offers[0]
            if not isinstance(offers, dict):
                return self._uncertain("unverified_offer")
            if offers.get("@type") and not recordcity_db._schema_type(offers, "Offer"):
                return self._uncertain("unverified_offer")
            raw_status = recordcity_db._infer_status(offers)
            status = {"on_sale": "active", "sold_out": "sold"}.get(raw_status)
            if status is None:
                return self._uncertain("unknown_status")

            raw_price = offers.get("price")
            price = None
            if raw_price is not None:
                if offers.get("priceCurrency") != "JPY":
                    return self._uncertain("unverified_currency")
                price = recordcity_db._listing_price(raw_price)
                if price is None or price > 2_147_483_647:
                    return self._uncertain("invalid_price")
            elif status == "active":
                return self._uncertain("missing_price")

            variants = []
            if price is not None:
                variants.append({
                    "name": "Default Title", "stock": 1 if status == "active" else 0,
                    "price": price,
                })
            return PatrolResult(
                price=price, status=status, variants=variants,
                confidence="high", evidence_strength="hard",
                reason="target_product_offer", price_source="json_ld",
            )
        except Exception as exc:
            missing = deleted_http_error_result(exc)
            if missing is not None:
                return missing
            status_code = getattr(exc, "status_code", None)
            if isinstance(exc, ScrapeBlockedError) or status_code in {401, 403, 429, 503}:
                return PatrolResult(
                    status="blocked", confidence="low",
                    error=f"RecordCity access blocked (HTTP {status_code})" if status_code else "RecordCity access blocked",
                    reason="blocked_http_status" if status_code else "blocked_challenge_page",
                )
            return self._uncertain("detail_fetch_failed")
