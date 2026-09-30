"""Search-only identity and completion evidence; contains no network operations."""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from services.scrape_observation import inspect_scraped_items
from services.scrape_safety import (
    UnsafeScrapeUrlError, identify_marketplace_site, validate_marketplace_url,
)


END_REASONS = frozenset({
    "requested_reached", "listing_exhausted", "explicit_empty", "page_limit",
    "candidate_limit", "pagination_loop", "unknown",
})


def search_item_identity(item: dict, *, site: str | None = None) -> str | None:
    """Use validated product paths, never titles or untrusted standalone SKUs.

    Query/fragment removal matches product persistence. Known apex/www aliases
    share an identity; RecordCity also has language aliases for a catalog ID.
    Missing/invalid URLs remain separate from unrelated results.
    """
    try:
        if site is None:
            site = identify_marketplace_site(urlsplit(str(item.get("url") or "")).hostname or "")
        url = validate_marketplace_url(item.get("url"), site, kind="detail")
    except (UnsafeScrapeUrlError, TypeError, ValueError):
        return None
    parsed = urlsplit(url)
    if site == "recordcity":
        match = re.fullmatch(r"/(?:[a-z]{2}/)?catalog/(\d+)/?", parsed.path)
        if match:
            return f"recordcity:{match.group(1)}"
    host = parsed.hostname
    if site == "snkrdunk" and host in {"snkrdunk.com", "www.snkrdunk.com"}:
        host = "snkrdunk.com"
    elif site == "surugaya" and host in {"suruga-ya.jp", "www.suruga-ya.jp"}:
        host = "suruga-ya.jp"
    return urlunsplit(("https", host, parsed.path.rstrip("/"), "", ""))


def deduplicate_search_items(items: list[dict], *, site: str) -> tuple[list[dict], int]:
    unique, seen = [], set()
    duplicates = 0
    for item in items:
        identity = search_item_identity(item, site=site)
        if identity is not None and identity in seen:
            duplicates += 1
            continue
        if identity is not None:
            seen.add(identity)
        unique.append(item)
    return unique, duplicates


def _count(value) -> int:
    try:
        return min(1_000_000, max(0, int(value))) if not isinstance(value, bool) else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def build_search_quality(
    items: list[dict], *, requested_count: int, duplicate_count: int = 0,
    progress: dict | None = None, excluded_count: int = 0,
    displayed_count: int | None = None, site: str | None = None,
    acquisition_mode: str = "detail",
) -> dict:
    """A sanitized payload for the authenticated job, before user filtering.

    ``listing_exhausted`` is reserved for adapters with explicit end evidence.
    An absent next link alone must be reported as ``unknown``. Old adapters
    returning only a list can establish target attainment, but not exhaustion.
    """
    progress = progress or {}
    requested = max(1, _count(requested_count))
    identifiable_items = [item for item in items if search_item_identity(item, site=site) is not None]
    unique_count = len(identifiable_items)
    invalid_identity_count = len(items) - unique_count
    if acquisition_mode == "listing":
        # Listing validity establishes display fields, not stock. In particular,
        # an explicitly unknown stock state must remain unknown after success.
        from services.listing_cards import validate_listing_card
        valid_count = sum(validate_listing_card(item, site=site) for item in identifiable_items)
    else:
        valid_count = inspect_scraped_items(identifiable_items)["success_count"]
    reason = progress.get("end_reason")
    if not isinstance(reason, str) or reason not in END_REASONS or reason == "requested_reached":
        reason = "unknown"
    if valid_count >= requested:
        reason = "requested_reached"
    elif reason == "explicit_empty" and items:
        reason = "unknown"
    detail_errors = _count(progress.get("detail_error_count"))
    invalid_cards = len(items) - valid_count + _count(progress.get("invalid_card_count")) if acquisition_mode == "listing" else 0
    return {
        "requested_count": requested,
        "unique_count": unique_count,
        "valid_count": valid_count,
        "invalid_identity_count": invalid_identity_count,
        "duplicate_count": _count(duplicate_count) + (_count(progress.get("duplicate_count")) if acquisition_mode == "listing" else 0),
        "acquisition_rate": min(1.0, valid_count / requested),
        "excluded_count": _count(excluded_count),
        "displayed_count": len(items) if displayed_count is None else _count(displayed_count),
        "candidates_count": _count(progress["candidates_count"]) if "candidates_count" in progress else None,
        "pages_fetched": _count(progress["pages_fetched"]) if "pages_fetched" in progress else None,
        "detail_error_count": detail_errors,
        "end_reason": reason,
        "completion_verified": valid_count == len(items) and not invalid_cards and (
            reason in {"requested_reached", "explicit_empty"}
            or (reason == "listing_exhausted" and detail_errors == 0)
        ),
        **({"acquisition_mode": "listing", "invalid_card_count": invalid_cards} if acquisition_mode == "listing" else {}),
    }


def inspect_search_quality(items: list[dict], quality: dict) -> dict:
    """Do not let an unexplained short search close an operational incident."""
    if quality.get("acquisition_mode") == "listing":
        errors = quality.get("invalid_card_count", len(items) - quality["valid_count"])
        observation = dict(
            outcome="failure" if errors else "success" if items else "no_observations",
            reason="invalid_result" if errors else None if items else "empty_result",
            success_count=quality["valid_count"], error_count=errors,
        )
    else:
        observation = inspect_scraped_items(items)
    if quality["invalid_identity_count"]:
        observation = {
            **observation,
            "outcome": "failure",
            "reason": observation["reason"] or "invalid_result",
            "success_count": quality["valid_count"],
            "error_count": max(observation["error_count"], len(items) - quality["valid_count"]),
        }
    if observation["outcome"] == "failure":
        return observation
    reason = quality["end_reason"]
    if reason == "requested_reached":
        return observation
    if reason == "explicit_empty" and not items:
        return observation  # No actual successful product, so never recovery.
    if reason == "listing_exhausted" and not quality["detail_error_count"]:
        return observation
    return {
        **observation,
        "outcome": "failure",
        "reason": "incomplete_results",
        "error_count": max(1, quality["detail_error_count"]),
    }
