"""Sanitized, passive quality observations; never fetch or modify scraper output."""
from __future__ import annotations

import logging

from services.scrape_result_policy import (
    normalize_price_for_persistence,
    normalize_status_for_persistence,
)

logger = logging.getLogger(__name__)


def classify_scrape_failure(error, *, default="fetch_error") -> str:
    """Persist a small reason vocabulary, never exception text or source URLs."""
    text = str(error or "").lower()
    if "captcha" in text:
        return "captcha"
    if "429" in text or "rate limit" in text or "rate_limit" in text:
        return "rate_limited"
    if any(term in text for term in ("waf", "blocked", "challenge", "アクセスが制限")):
        return "access_blocked"
    return default


def inspect_scraped_items(items: list[dict]) -> dict:
    """Check pre-filter results, so intentional filtering is not a scrape failure.

    Missing/unknown stock and missing active prices are not verified success.
    Sold/deleted products may legitimately have no current selling price.
    """
    if not items:
        return dict(outcome="no_observations", reason="empty_result", success_count=0, error_count=0)
    successful = 0
    reasons = []
    for item in items:
        status = normalize_status_for_persistence(item.get("status"))
        if not str(item.get("title") or "").strip():
            reasons.append("invalid_result")
        elif status in {"blocked", "error"}:
            reasons.append("access_blocked" if status == "blocked" else "invalid_result")
        elif status == "unknown":
            reasons.append("unknown_status")
        elif status == "on_sale":
            try:
                price = normalize_price_for_persistence(item.get("price"))
            except (TypeError, ValueError, OverflowError):
                price = None
            if price is None or price <= 0:
                reasons.append("missing_price")
            else:
                successful += 1
        else:
            successful += 1
    return dict(
        outcome="failure" if reasons else "success",
        reason=reasons[0] if reasons else None,
        success_count=successful,
        error_count=len(reasons),
    )


def record_observation_safely(**observation) -> bool:
    """Monitoring storage must not turn a completed business job into a failure."""
    try:
        from services.scrape_health import record_scrape_observation

        return record_scrape_observation(**observation)
    except Exception as exc:
        logger.warning("Scrape health write unavailable: error_type=%s", type(exc).__name__)
        return False
