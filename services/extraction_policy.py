"""
Helpers for consistent extractor priority and internal provenance tracking.
"""


def _has_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def pick_first(*candidates, default=None):
    """
    Return the first non-empty `(value, source)` pair.
    """
    for source, value in candidates:
        if _has_value(value):
            return value, source
    return default, ""


def pick_first_valid(*candidates, validator=None, default=None):
    """
    Return the first candidate accepted by the supplied validator.
    """
    effective_validator = validator or _has_value
    for source, value in candidates:
        if effective_validator(value):
            return value, source
    return default, ""


def attach_extraction_trace(result: dict, strategy: str = "", field_sources: dict | None = None) -> dict:
    meta = dict(result.get("_scrape_meta") or {})

    if strategy:
        meta["strategy"] = strategy

    if field_sources:
        merged_sources = dict(meta.get("field_sources") or {})
        for field, source in field_sources.items():
            if source:
                merged_sources[field] = source
        if merged_sources:
            meta["field_sources"] = merged_sources

    if meta:
        result["_scrape_meta"] = meta
    return result
