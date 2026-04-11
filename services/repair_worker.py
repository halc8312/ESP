"""
Selector repair validation worker.

Phase 3 validates DB-backed repair candidates across multiple canary pages
before promoting them into versioned active rule sets.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from services.mercari_browser_fetch import (
    fetch_mercari_page_via_browser_pool_sync,
    should_use_mercari_browser_pool_detail,
)
from services.page_state_classifier import classify_page_state
from services.repair_store import (
    get_pending_repair_candidate,
    list_pending_repair_candidates,
    load_active_selectors,
    mark_repair_candidate_rejected,
    promote_repair_candidate,
)
from services.scraping_client import fetch_dynamic
from services.selector_healer import evaluate_selector_candidate
from services.snkrdunk_browser_fetch import (
    fetch_snkrdunk_page_via_browser_pool_sync,
    should_use_snkrdunk_browser_pool_dynamic,
)


logger = logging.getLogger("repair_worker")

_CANARY_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "repair_canaries.json"


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _dedupe_urls(urls: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_url in urls:
        url = str(raw_url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        normalized.append(url)
    return normalized


def _load_canary_config() -> dict[str, Any]:
    try:
        return json.loads(_CANARY_CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse repair canary config: %s", exc)
        return {}


def load_repair_canary_urls(site: str, page_type: str) -> list[str]:
    normalized_site = str(site or "").strip().lower()
    normalized_page_type = str(page_type or "").strip().lower()

    env_key = f"SELECTOR_REPAIR_CANARY_URLS_{normalized_site}_{normalized_page_type}".upper()
    env_value = str(os.environ.get(env_key, "") or "").strip()
    if env_value:
        return _dedupe_urls([value.strip() for value in env_value.split(",")])

    config = _load_canary_config()
    configured = config.get(normalized_site, {}).get(normalized_page_type, [])
    if isinstance(configured, list):
        return _dedupe_urls([str(value or "").strip() for value in configured])
    return []


def fetch_canary_page(site: str, url: str):
    normalized_site = str(site or "").strip().lower()
    if normalized_site == "mercari":
        if should_use_mercari_browser_pool_detail():
            return fetch_mercari_page_via_browser_pool_sync(url, network_idle=True)
        return fetch_dynamic(url, headless=True, network_idle=True)

    if normalized_site == "snkrdunk":
        if should_use_snkrdunk_browser_pool_dynamic():
            return fetch_snkrdunk_page_via_browser_pool_sync(url, network_idle=True)
        return fetch_dynamic(url, headless=True, network_idle=True)

    raise ValueError(f"Unsupported repair validation site: {site}")


def _minimum_candidate_score() -> int:
    return max(0, _env_int("SELECTOR_REPAIR_MIN_SCORE", 90))


def _minimum_canary_count() -> int:
    return max(2, _env_int("SELECTOR_REPAIR_MIN_CANARIES", 2))


def _build_promoted_selectors(site: str, page_type: str, field: str, proposed_selector: str) -> list[str]:
    from selector_config import get_json_selectors

    base_selectors = load_active_selectors(site, page_type, field) or get_json_selectors(site, page_type, field)
    selectors = [str(proposed_selector or "").strip()] + [selector for selector in base_selectors if selector != proposed_selector]
    return _dedupe_urls(selectors)


def _load_candidate_batch(*, limit: int, candidate_id: int | None = None) -> list[dict[str, Any]]:
    if candidate_id is not None:
        candidate = get_pending_repair_candidate(candidate_id)
        return [candidate] if candidate else []
    return list_pending_repair_candidates(limit=limit)


def validate_repair_candidate(
    candidate: dict[str, Any],
    *,
    page_fetcher: Callable[[str, str], Any] | None = None,
    canary_urls: list[str] | None = None,
) -> dict[str, Any]:
    score = candidate.get("score")
    try:
        numeric_score = int(score) if score is not None else 0
    except (TypeError, ValueError):
        numeric_score = 0

    if numeric_score < _minimum_candidate_score():
        return {
            "ok": False,
            "reason": "low_score",
            "score": numeric_score,
            "min_score": _minimum_candidate_score(),
            "results": [],
        }

    normalized_canaries = _dedupe_urls(canary_urls or load_repair_canary_urls(candidate["site"], candidate["page_type"]))
    if len(normalized_canaries) < _minimum_canary_count():
        return {
            "ok": False,
            "reason": "insufficient_canaries",
            "required_canaries": _minimum_canary_count(),
            "canary_count": len(normalized_canaries),
            "results": [],
        }

    fetcher = page_fetcher or fetch_canary_page
    results: list[dict[str, Any]] = []
    success_count = 0

    for url in normalized_canaries:
        candidate_result = {
            "url": url,
            "ok": False,
        }
        try:
            page = fetcher(candidate["site"], url)
            assessment = classify_page_state(candidate["site"], page, page_type=candidate["page_type"])
            candidate_result["page_state"] = assessment.state
            candidate_result["page_state_reasons"] = list(assessment.reasons)
        except Exception as exc:
            candidate_result["reason"] = "fetch_or_classification_failed"
            candidate_result["error"] = str(exc)
            results.append(candidate_result)
            continue

        if not assessment.allow_healing:
            candidate_result["reason"] = "page_state_disallowed"
            results.append(candidate_result)
            continue

        try:
            evaluation = evaluate_selector_candidate(
                page,
                candidate["site"],
                candidate["page_type"],
                candidate["field"],
                candidate["proposed_selector"],
                parser=candidate.get("parser") or "scrapling",
            )
            candidate_result["match_count"] = int(evaluation.get("match_count") or 0)
            candidate_result["ok"] = bool(evaluation.get("ok"))
            if candidate_result["ok"]:
                success_count += 1
        except Exception as exc:
            candidate_result["reason"] = "candidate_evaluation_failed"
            candidate_result["error"] = str(exc)
        results.append(candidate_result)

    ok = success_count >= _minimum_canary_count() and success_count == len(normalized_canaries)
    return {
        "ok": ok,
        "reason": "validated" if ok else "canary_validation_failed",
        "score": numeric_score,
        "success_count": success_count,
        "canary_count": len(normalized_canaries),
        "results": results,
    }


def _plan_candidate_action(
    candidate: dict[str, Any],
    *,
    page_fetcher: Callable[[str, str], Any] | None = None,
) -> dict[str, Any]:
    try:
        validation = validate_repair_candidate(candidate, page_fetcher=page_fetcher)
    except Exception as exc:
        logger.warning("Repair candidate validation crashed for candidate_id=%s: %s", candidate.get("id"), exc)
        return {
            "candidate_id": candidate["id"],
            "action": "reject",
            "reason": "validation_worker_exception",
            "details": {"error": str(exc)},
        }

    if validation["reason"] == "insufficient_canaries":
        return {
            "candidate_id": candidate["id"],
            "action": "skip",
            "reason": "insufficient_canaries",
            "details": validation,
        }

    if not validation["ok"]:
        return {
            "candidate_id": candidate["id"],
            "action": "reject",
            "reason": str(validation["reason"]),
            "details": {"validation": validation},
        }

    promoted_selectors = _build_promoted_selectors(
        candidate["site"],
        candidate["page_type"],
        candidate["field"],
        candidate["proposed_selector"],
    )
    if not promoted_selectors:
        return {
            "candidate_id": candidate["id"],
            "action": "reject",
            "reason": "empty_promoted_selector_payload",
            "details": {"validation": validation},
        }

    active_selectors = load_active_selectors(candidate["site"], candidate["page_type"], candidate["field"]) or []
    if active_selectors and promoted_selectors[0] == active_selectors[0]:
        return {
            "candidate_id": candidate["id"],
            "action": "reject",
            "reason": "already_active",
            "details": {"validation": validation},
            "promoted_selectors": promoted_selectors,
            "active_selectors": active_selectors,
        }

    return {
        "candidate_id": candidate["id"],
        "action": "promote",
        "reason": "validated",
        "details": {"validation": validation},
        "promoted_selectors": promoted_selectors,
        "active_selectors": active_selectors,
    }


def preview_pending_repair_candidates(
    *,
    limit: int | None = None,
    candidate_id: int | None = None,
    page_fetcher: Callable[[str, str], Any] | None = None,
) -> dict[str, Any]:
    safe_limit = max(1, int(limit or _env_int("WORKER_SELECTOR_REPAIR_LIMIT", 1)))
    candidates = _load_candidate_batch(limit=safe_limit, candidate_id=candidate_id)
    summary = {
        "inspected": len(candidates),
        "would_promote": 0,
        "would_reject": 0,
        "would_skip": 0,
        "results": [],
    }

    for candidate in candidates:
        plan = _plan_candidate_action(candidate, page_fetcher=page_fetcher)
        if plan["action"] == "promote":
            summary["would_promote"] += 1
            status = "would_promote"
        elif plan["action"] == "skip":
            summary["would_skip"] += 1
            status = "would_skip"
        else:
            summary["would_reject"] += 1
            status = "would_reject"

        summary["results"].append(
            {
                "candidate_id": candidate["id"],
                "status": status,
                "reason": plan["reason"],
                "details": plan.get("details") or {},
                "promoted_selectors": list(plan.get("promoted_selectors") or []),
                "active_selectors": list(plan.get("active_selectors") or []),
            }
        )

    return summary


def process_pending_repair_candidates(
    *,
    limit: int | None = None,
    candidate_id: int | None = None,
    page_fetcher: Callable[[str, str], Any] | None = None,
) -> dict[str, Any]:
    safe_limit = max(1, int(limit or _env_int("WORKER_SELECTOR_REPAIR_LIMIT", 1)))
    candidates = _load_candidate_batch(limit=safe_limit, candidate_id=candidate_id)
    summary = {
        "inspected": len(candidates),
        "promoted": 0,
        "rejected": 0,
        "skipped": 0,
        "results": [],
    }

    for candidate in candidates:
        plan = _plan_candidate_action(candidate, page_fetcher=page_fetcher)

        if plan["action"] == "skip":
            summary["skipped"] += 1
            summary["results"].append(
                {
                    "candidate_id": candidate["id"],
                    "status": "skipped",
                    "reason": plan["reason"],
                    "details": plan.get("details") or {},
                }
            )
            continue

        if plan["action"] == "reject":
            mark_repair_candidate_rejected(
                candidate["id"],
                reason=plan["reason"],
                details=plan.get("details") or {},
            )
            summary["rejected"] += 1
            summary["results"].append(
                {
                    "candidate_id": candidate["id"],
                    "status": "rejected",
                    "reason": plan["reason"],
                    "details": plan.get("details") or {},
                }
            )
            continue

        promotion = promote_repair_candidate(
            candidate["id"],
            selectors_payload=list(plan.get("promoted_selectors") or []),
            validation_summary=plan.get("details") or {},
        )
        if promotion.get("ok"):
            summary["promoted"] += 1
            summary["results"].append(
                {
                    "candidate_id": candidate["id"],
                    "status": "promoted",
                    "reason": plan["reason"],
                    "details": promotion,
                }
            )
            continue

        if promotion.get("reason") == "concurrent_promotion":
            summary["skipped"] += 1
            summary["results"].append({"candidate_id": candidate["id"], "status": "skipped", "details": promotion})
            continue

        mark_repair_candidate_rejected(
            candidate["id"],
            reason=str(promotion.get("reason") or "promotion_failed"),
            details={**(plan.get("details") or {}), "promotion": promotion},
        )
        summary["rejected"] += 1
        summary["results"].append(
            {
                "candidate_id": candidate["id"],
                "status": "rejected",
                "reason": str(promotion.get("reason") or "promotion_failed"),
                "details": promotion,
            }
        )

    return summary


def should_process_repairs_on_startup() -> bool:
    return _env_flag("WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP", default=False)
