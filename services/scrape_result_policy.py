"""
Persistence policy for scraper outputs.

Fail closed on uncertain active prices so noisy pages cannot overwrite good
product state.
"""
import re
from typing import Optional


def normalize_price_for_persistence(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)

    text = str(value).strip()
    if not text:
        return None

    match = re.search(r"[-+]?\d[\d,]*", text)
    if not match:
        return None

    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return None


def normalize_status_for_persistence(status: Optional[str]) -> str:
    normalized = str(status or "").strip().lower()
    if normalized == "active":
        return "on_sale"
    if normalized in {"on_sale", "sold", "deleted", "blocked", "error", "unknown"}:
        return normalized
    return "unknown"


def _can_promote_unknown_status_for_manual_selection(item: Optional[dict]) -> bool:
    candidate = dict(item or {})
    title = str(candidate.get("title") or "").strip()
    if not title:
        return False

    numeric_price = normalize_price_for_persistence(candidate.get("price"))
    return numeric_price is not None and numeric_price > 0


def normalize_item_for_persistence(item: Optional[dict], *, manual_selection: bool = False) -> dict:
    normalized = dict(item or {})
    normalized["price"] = normalize_price_for_persistence(normalized.get("price"))
    normalized["status"] = normalize_status_for_persistence(normalized.get("status"))
    if manual_selection and normalized["status"] == "unknown" and _can_promote_unknown_status_for_manual_selection(normalized):
        normalized["status"] = "on_sale"
        normalized["_manual_status_override"] = True
    return normalized


def evaluate_persistence(
    site: str,
    item: dict,
    meta: Optional[dict],
    existing_product,
    *,
    manual_selection: bool = False,
) -> str:
    normalized = normalize_item_for_persistence(item, manual_selection=manual_selection)
    status = normalized.get("status") or "unknown"
    title = str(normalized.get("title") or "").strip()
    confidence = str((meta or {}).get("confidence") or "high").lower()

    numeric_price = normalize_price_for_persistence(normalized.get("price"))

    if status in {"blocked", "error"}:
        return "reject"
    if status == "unknown":
        return "allow_full" if manual_selection and title else "reject"

    if status == "deleted":
        return "allow_status_only" if existing_product is not None else "reject"

    if status == "sold":
        if existing_product is not None and (numeric_price is None or numeric_price <= 0):
            return "allow_status_only"
        if numeric_price is None or numeric_price <= 0:
            return "allow_full" if manual_selection and title else "reject"
        if confidence == "low":
            return "allow_status_only" if existing_product is not None else "reject"
        if not title:
            return "reject"
        return "allow_full"

    if status == "on_sale":
        if numeric_price is None or numeric_price <= 0:
            return "allow_full" if manual_selection and title else "reject"
        if confidence == "low":
            return "reject"
        if not title:
            return "reject"
        return "allow_full"

    return "reject"


def build_policy_reason(item: Optional[dict], meta: Optional[dict]) -> str:
    normalized = normalize_item_for_persistence(item)
    status = normalized.get("status") or "unknown"
    price = normalized.get("price")
    reasons = list((meta or {}).get("reasons") or [])
    if reasons:
        return "; ".join(str(reason) for reason in reasons)
    if status == "on_sale" and price is None:
        return "active-without-price"
    return status
