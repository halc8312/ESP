"""
PostgreSQL-backed selector repair storage with fail-open JSON fallback support.

The store remains optional at runtime so split web/worker deploys do not crash
during migration windows, but active-rule promotion is versioned and
transactional once the tables are available.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from sqlalchemy import func, inspect as sa_inspect
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

from database import SessionLocal, get_database_url, normalize_database_url
from models import SelectorActiveRuleSet, SelectorRepairCandidate
from time_utils import utc_now


logger = logging.getLogger("repair_store")

_STORE_AVAILABILITY_CACHE: dict[str, bool] = {}
_MISSING_TABLE_MARKERS = (
    "selector_repair_candidates",
    "selector_active_rule_sets",
    "no such table",
    "undefined table",
    "does not exist",
)
_REPAIR_STORE_TABLES = (
    "selector_repair_candidates",
    "selector_active_rule_sets",
)


def _store_mode() -> str:
    return str(os.environ.get("SELECTOR_REPAIR_STORE_MODE", "auto") or "auto").strip().lower()


def _db_store_enabled() -> bool:
    return _store_mode() not in {"disabled", "json_only", "off"}


def _store_cache_key() -> str:
    return normalize_database_url(get_database_url())


def _looks_like_missing_store_table(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _MISSING_TABLE_MARKERS)


def _normalize_selectors(selectors: Any) -> list[str]:
    if isinstance(selectors, str):
        try:
            parsed = json.loads(selectors)
        except json.JSONDecodeError:
            parsed = [selectors]
    elif isinstance(selectors, (list, tuple)):
        parsed = list(selectors)
    else:
        parsed = []

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_selector in parsed:
        selector = str(raw_selector or "").strip()
        if not selector or selector in seen:
            continue
        seen.add(selector)
        normalized.append(selector)
    return normalized


def _mark_store_available() -> None:
    _STORE_AVAILABILITY_CACHE[_store_cache_key()] = True


def _mark_store_unavailable() -> None:
    _STORE_AVAILABILITY_CACHE[_store_cache_key()] = False


def _store_known_unavailable() -> bool:
    return _STORE_AVAILABILITY_CACHE.get(_store_cache_key()) is False


def reset_repair_store_cache() -> None:
    _STORE_AVAILABILITY_CACHE.clear()


def inspect_repair_store_state() -> dict[str, Any]:
    if not _db_store_enabled():
        return {
            "ready": False,
            "enabled": False,
            "known_unavailable": _store_known_unavailable(),
            "missing_tables": list(_REPAIR_STORE_TABLES),
            "blockers": ["repair_store_disabled"],
        }

    session = SessionLocal()
    try:
        inspector = sa_inspect(session.get_bind())
        existing_tables = set(inspector.get_table_names())
        missing_tables = [table_name for table_name in _REPAIR_STORE_TABLES if table_name not in existing_tables]
        if missing_tables:
            _mark_store_unavailable()
        else:
            _mark_store_available()
        return {
            "ready": not missing_tables,
            "enabled": True,
            "known_unavailable": _store_known_unavailable(),
            "missing_tables": missing_tables,
            "blockers": ["repair_store_tables_missing"] if missing_tables else [],
        }
    except (OperationalError, ProgrammingError) as exc:
        session.rollback()
        if _looks_like_missing_store_table(exc):
            _mark_store_unavailable()
            return {
                "ready": False,
                "enabled": True,
                "known_unavailable": True,
                "missing_tables": list(_REPAIR_STORE_TABLES),
                "blockers": ["repair_store_tables_missing"],
                "error": str(exc),
            }
        return {
            "ready": False,
            "enabled": True,
            "known_unavailable": _store_known_unavailable(),
            "missing_tables": [],
            "blockers": ["repair_store_inspection_failed"],
            "error": str(exc),
        }
    except Exception as exc:
        session.rollback()
        return {
            "ready": False,
            "enabled": True,
            "known_unavailable": _store_known_unavailable(),
            "missing_tables": [],
            "blockers": ["repair_store_inspection_failed"],
            "error": str(exc),
        }
    finally:
        session.close()


def _serialize_repair_candidate(record: SelectorRepairCandidate) -> dict[str, Any]:
    return {
        "id": int(record.id),
        "site": record.site,
        "page_type": record.page_type,
        "field": record.field,
        "parser": record.parser,
        "proposed_selector": record.proposed_selector,
        "source_selector": record.source_selector,
        "score": record.score,
        "page_state": record.page_state,
        "status": record.status,
        "details": json.loads(record.details_payload) if record.details_payload else {},
    }


def load_active_selectors(site: str, page_type: str, field: str) -> list[str] | None:
    if not _db_store_enabled() or _store_known_unavailable():
        return None

    session = SessionLocal()
    try:
        record = (
            session.query(SelectorActiveRuleSet)
            .filter_by(
                site=str(site or "").strip().lower(),
                page_type=str(page_type or "").strip().lower(),
                field=str(field or "").strip().lower(),
                is_active=True,
            )
            .order_by(SelectorActiveRuleSet.version.desc(), SelectorActiveRuleSet.id.desc())
            .first()
        )
        _mark_store_available()
        if record is None:
            return None
        selectors = _normalize_selectors(record.selectors_payload)
        return selectors or None
    except (OperationalError, ProgrammingError) as exc:
        session.rollback()
        if _looks_like_missing_store_table(exc):
            _mark_store_unavailable()
            logger.info("Repair store tables unavailable; falling back to JSON selectors")
            return None
        logger.warning("Repair store lookup failed; falling back to JSON selectors: %s", exc)
        return None
    except Exception as exc:
        session.rollback()
        logger.warning("Repair store lookup failed; falling back to JSON selectors: %s", exc)
        return None
    finally:
        session.close()


def record_repair_candidate(
    *,
    site: str,
    page_type: str,
    field: str,
    parser: str,
    proposed_selector: str,
    source_selector: str | None = None,
    score: float | int | None = None,
    page_state: str = "healthy",
    details: dict[str, Any] | None = None,
) -> int | None:
    if not _db_store_enabled() or _store_known_unavailable():
        return None

    normalized_selector = str(proposed_selector or "").strip()
    if not normalized_selector:
        return None

    payload = json.dumps(details or {}, ensure_ascii=False, sort_keys=True) if details else None
    numeric_score = None
    if score is not None:
        try:
            numeric_score = int(round(float(score)))
        except (TypeError, ValueError):
            numeric_score = None

    session = SessionLocal()
    try:
        candidate = SelectorRepairCandidate(
            site=str(site or "").strip().lower(),
            page_type=str(page_type or "").strip().lower(),
            field=str(field or "").strip().lower(),
            parser=str(parser or "scrapling").strip().lower(),
            proposed_selector=normalized_selector,
            source_selector=str(source_selector or "").strip() or None,
            score=numeric_score,
            page_state=str(page_state or "healthy").strip().lower(),
            status="pending",
            details_payload=payload,
        )
        session.add(candidate)
        session.commit()
        session.refresh(candidate)
        _mark_store_available()
        return int(candidate.id)
    except (OperationalError, ProgrammingError) as exc:
        session.rollback()
        if _looks_like_missing_store_table(exc):
            _mark_store_unavailable()
            logger.info("Repair store tables unavailable; skipping candidate persistence")
            return None
        logger.warning("Repair candidate persistence failed: %s", exc)
        return None
    except Exception as exc:
        session.rollback()
        logger.warning("Repair candidate persistence failed: %s", exc)
        return None
    finally:
        session.close()


def list_pending_repair_candidates(limit: int = 10) -> list[dict[str, Any]]:
    if not _db_store_enabled() or _store_known_unavailable():
        return []

    safe_limit = max(1, int(limit or 10))
    session = SessionLocal()
    try:
        records = (
            session.query(SelectorRepairCandidate)
            .filter(
                SelectorRepairCandidate.status == "pending",
                SelectorRepairCandidate.page_state == "healthy",
            )
            .order_by(SelectorRepairCandidate.created_at.asc(), SelectorRepairCandidate.id.asc())
            .limit(safe_limit)
            .all()
        )
        _mark_store_available()
        return [_serialize_repair_candidate(record) for record in records]
    except (OperationalError, ProgrammingError) as exc:
        session.rollback()
        if _looks_like_missing_store_table(exc):
            _mark_store_unavailable()
            return []
        logger.warning("Repair candidate listing failed: %s", exc)
        return []
    except Exception as exc:
        session.rollback()
        logger.warning("Repair candidate listing failed: %s", exc)
        return []
    finally:
        session.close()


def get_pending_repair_candidate(candidate_id: int) -> dict[str, Any] | None:
    if not _db_store_enabled() or _store_known_unavailable():
        return None

    session = SessionLocal()
    try:
        record = (
            session.query(SelectorRepairCandidate)
            .filter(
                SelectorRepairCandidate.id == int(candidate_id),
                SelectorRepairCandidate.status == "pending",
                SelectorRepairCandidate.page_state == "healthy",
            )
            .one_or_none()
        )
        _mark_store_available()
        if record is None:
            return None
        return _serialize_repair_candidate(record)
    except (OperationalError, ProgrammingError) as exc:
        session.rollback()
        if _looks_like_missing_store_table(exc):
            _mark_store_unavailable()
            return None
        logger.warning("Repair candidate fetch failed: %s", exc)
        return None
    except Exception as exc:
        session.rollback()
        logger.warning("Repair candidate fetch failed: %s", exc)
        return None
    finally:
        session.close()


def _merge_details_payload(existing_payload: str | None, patch: dict[str, Any] | None) -> str | None:
    existing: dict[str, Any] = {}
    if existing_payload:
        try:
            existing = json.loads(existing_payload)
        except json.JSONDecodeError:
            existing = {}
    if patch:
        existing.update(patch)
    return json.dumps(existing, ensure_ascii=False, sort_keys=True) if existing else None


def mark_repair_candidate_rejected(candidate_id: int, *, reason: str, details: dict[str, Any] | None = None) -> bool:
    if not _db_store_enabled() or _store_known_unavailable():
        return False

    session = SessionLocal()
    try:
        candidate = session.get(SelectorRepairCandidate, int(candidate_id))
        if candidate is None:
            return False
        if candidate.status in {"promoted", "rejected"}:
            return False
        candidate.status = "rejected"
        candidate.details_payload = _merge_details_payload(
            candidate.details_payload,
            {"rejection_reason": str(reason or "").strip() or "rejected", **(details or {})},
        )
        session.commit()
        _mark_store_available()
        return True
    except (OperationalError, ProgrammingError) as exc:
        session.rollback()
        if _looks_like_missing_store_table(exc):
            _mark_store_unavailable()
            return False
        logger.warning("Repair candidate rejection failed: %s", exc)
        return False
    except Exception as exc:
        session.rollback()
        logger.warning("Repair candidate rejection failed: %s", exc)
        return False
    finally:
        session.close()


def promote_repair_candidate(
    candidate_id: int,
    *,
    selectors_payload: list[str],
    validation_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not _db_store_enabled() or _store_known_unavailable():
        return {"ok": False, "reason": "store_disabled"}

    normalized_selectors = _normalize_selectors(selectors_payload)
    if not normalized_selectors:
        return {"ok": False, "reason": "empty_selector_payload"}

    session = SessionLocal()
    try:
        candidate = (
            session.query(SelectorRepairCandidate)
            .filter(SelectorRepairCandidate.id == int(candidate_id))
            .with_for_update()
            .one_or_none()
        )
        if candidate is None:
            return {"ok": False, "reason": "candidate_missing"}
        if candidate.status == "promoted":
            return {"ok": True, "reason": "already_promoted"}
        if candidate.status == "rejected":
            return {"ok": False, "reason": "candidate_rejected"}

        active_rules = (
            session.query(SelectorActiveRuleSet)
            .filter(
                SelectorActiveRuleSet.site == candidate.site,
                SelectorActiveRuleSet.page_type == candidate.page_type,
                SelectorActiveRuleSet.field == candidate.field,
                SelectorActiveRuleSet.is_active.is_(True),
            )
            .with_for_update()
            .all()
        )

        current_max_version = (
            session.query(func.max(SelectorActiveRuleSet.version))
            .filter(
                SelectorActiveRuleSet.site == candidate.site,
                SelectorActiveRuleSet.page_type == candidate.page_type,
                SelectorActiveRuleSet.field == candidate.field,
            )
            .scalar()
        ) or 0

        for record in active_rules:
            record.is_active = False

        promoted_rule = SelectorActiveRuleSet(
            site=candidate.site,
            page_type=candidate.page_type,
            field=candidate.field,
            version=int(current_max_version) + 1,
            selectors_payload=json.dumps(normalized_selectors, ensure_ascii=False),
            is_active=True,
            source_candidate_id=candidate.id,
            activated_at=utc_now(),
        )
        session.add(promoted_rule)
        candidate.status = "promoted"
        candidate.details_payload = _merge_details_payload(candidate.details_payload, validation_summary)
        session.commit()
        session.refresh(promoted_rule)
        _mark_store_available()
        return {
            "ok": True,
            "rule_id": int(promoted_rule.id),
            "version": int(promoted_rule.version),
            "selectors": normalized_selectors,
        }
    except IntegrityError as exc:
        session.rollback()
        logger.warning("Repair candidate promotion lost a concurrency race: %s", exc)
        return {"ok": False, "reason": "concurrent_promotion", "error": str(exc)}
    except (OperationalError, ProgrammingError) as exc:
        session.rollback()
        if _looks_like_missing_store_table(exc):
            _mark_store_unavailable()
            return {"ok": False, "reason": "store_unavailable"}
        logger.warning("Repair candidate promotion failed: %s", exc)
        return {"ok": False, "reason": "promotion_failed", "error": str(exc)}
    except Exception as exc:
        session.rollback()
        logger.warning("Repair candidate promotion failed: %s", exc)
        return {"ok": False, "reason": "promotion_failed", "error": str(exc)}
    finally:
        session.close()
