import logging
from typing import Any

from services.alerts import get_alert_dispatcher


logger = logging.getLogger("scrape_alerts")


def notify_scrape_issue(
    *,
    event_type: str,
    site: str,
    page_type: str,
    field: str = "",
    severity: str = "warning",
    message: str = "",
    details: dict[str, Any] | None = None,
    dedupe_key: str = "",
) -> bool:
    try:
        return get_alert_dispatcher().notify_scrape_issue(
            event_type=event_type,
            site=site,
            page_type=page_type,
            field=field,
            severity=severity,
            message=message,
            details=details or {},
            dedupe_key=dedupe_key,
        )
    except Exception as exc:
        logger.debug("Scrape alert dispatch hook failed: %s", exc)
        return False


def report_patrol_result(site: str, url: str, result, *, page_type: str = "patrol_detail") -> bool:
    status = str(getattr(result, "status", "") or "unknown").strip().lower()
    price = getattr(result, "price", None)
    error = str(getattr(result, "error", "") or "").strip()
    reason = str(getattr(result, "reason", "") or "").strip()
    confidence = str(getattr(result, "confidence", "") or "").strip().lower()
    price_source = str(getattr(result, "price_source", "") or "").strip()

    details = {
        "url": url,
        "status": status or "unknown",
        "price": price,
        "reason": reason,
        "error": error,
        "confidence": confidence,
        "price_source": price_source,
    }

    if error:
        return notify_scrape_issue(
            event_type="patrol_error",
            site=site,
            page_type=page_type,
            field="status",
            severity="warning",
            message="Patrol scrape returned an error.",
            details=details,
            dedupe_key=f"scrape:patrol_error:{site}:{page_type}",
        )

    if status == "unknown":
        return notify_scrape_issue(
            event_type="patrol_unknown_status",
            site=site,
            page_type=page_type,
            field="status",
            severity="warning",
            message="Patrol scrape could not classify the product state.",
            details=details,
            dedupe_key=f"scrape:patrol_unknown_status:{site}:{page_type}",
        )

    if status == "active" and price is None:
        return notify_scrape_issue(
            event_type="patrol_active_without_price",
            site=site,
            page_type=page_type,
            field="price",
            severity="warning",
            message="Patrol scrape classified the item as active but did not extract a price.",
            details=details,
            dedupe_key=f"scrape:patrol_active_without_price:{site}:{page_type}",
        )

    return False


def report_detail_result(site: str, url: str, item: dict | None, meta: dict | None, *, page_type: str = "detail") -> bool:
    normalized_item = dict(item or {})
    normalized_meta = dict(meta or {})
    status = str(normalized_item.get("status") or "unknown").strip().lower()
    price = normalized_item.get("price")
    title = str(normalized_item.get("title") or "").strip()
    confidence = str(normalized_meta.get("confidence") or "").strip().lower()
    reasons = [str(value) for value in (normalized_meta.get("reasons") or []) if value]

    details = {
        "url": url,
        "status": status or "unknown",
        "price": price,
        "title_present": bool(title),
        "confidence": confidence,
        "reasons": reasons,
        "strategy": str(normalized_meta.get("strategy") or ""),
        "page_type": str(normalized_meta.get("page_type") or ""),
    }

    if status in {"blocked", "error"}:
        return notify_scrape_issue(
            event_type=f"{status}_detail_result",
            site=site,
            page_type=page_type,
            field="status",
            severity="error",
            message="Detail scrape ended in a blocked or error state.",
            details=details,
            dedupe_key=f"scrape:{status}_detail_result:{site}:{page_type}",
        )

    if status == "unknown":
        return notify_scrape_issue(
            event_type="unknown_detail_result",
            site=site,
            page_type=page_type,
            field="status",
            severity="warning",
            message="Detail scrape could not confidently classify the page.",
            details=details,
            dedupe_key=f"scrape:unknown_detail_result:{site}:{page_type}",
        )

    if status in {"active", "on_sale"} and price is None:
        return notify_scrape_issue(
            event_type="active_without_price",
            site=site,
            page_type=page_type,
            field="price",
            severity="warning",
            message="Detail scrape classified the item as active but did not extract a price.",
            details=details,
            dedupe_key=f"scrape:active_without_price:{site}:{page_type}",
        )

    return False
