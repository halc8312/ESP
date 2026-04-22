from __future__ import annotations

from typing import Callable

from database import SessionLocal
from models import DescriptionTemplate, PriceList, Product, ProductSnapshot
from services.rich_text import normalize_rich_text


Normalizer = Callable[[str | None], str | None]


def _normalize_optional_rich_text(value: str | None) -> str | None:
    normalized = normalize_rich_text(value)
    return normalized or None


def _process_records(query, field_names: tuple[str, ...], normalizer: Normalizer) -> dict[str, int]:
    scanned = 0
    changed = 0

    for record in query:
        scanned += 1
        dirty = False
        for field_name in field_names:
            current_value = getattr(record, field_name)
            normalized_value = normalizer(current_value)
            if current_value != normalized_value:
                setattr(record, field_name, normalized_value)
                dirty = True
        if dirty:
            changed += 1

    return {
        "scanned": scanned,
        "changed": changed,
    }


def run_rich_text_maintenance(*, apply: bool = False, user_id: int | None = None, include_snapshots: bool = True) -> dict[str, object]:
    session_db = SessionLocal()
    try:
        warnings: list[str] = []
        product_query = session_db.query(Product)
        pricelist_query = session_db.query(PriceList)
        snapshot_query = session_db.query(ProductSnapshot)

        if user_id is not None:
            product_query = product_query.filter(Product.user_id == user_id)
            pricelist_query = pricelist_query.filter(PriceList.user_id == user_id)
            snapshot_query = snapshot_query.join(Product, Product.id == ProductSnapshot.product_id).filter(Product.user_id == user_id)
            description_template_query = (
                session_db.query(DescriptionTemplate)
                .filter(DescriptionTemplate.user_id == user_id)
            )
        else:
            description_template_query = session_db.query(DescriptionTemplate)

        sections: dict[str, dict[str, int]] = {}
        sections["products"] = _process_records(
            product_query.order_by(Product.id),
            ("custom_description", "custom_description_en"),
            _normalize_optional_rich_text,
        )
        sections["price_lists"] = _process_records(
            pricelist_query.order_by(PriceList.id),
            ("notes",),
            _normalize_optional_rich_text,
        )
        sections["description_templates"] = _process_records(
            description_template_query.order_by(DescriptionTemplate.id),
            ("content",),
            _normalize_optional_rich_text,
        )

        if include_snapshots:
            sections["product_snapshots"] = _process_records(
                snapshot_query.order_by(ProductSnapshot.id),
                ("description",),
                _normalize_optional_rich_text,
            )

        changed_rows = sum(section["changed"] for section in sections.values())
        scanned_rows = sum(section["scanned"] for section in sections.values())

        if apply:
            session_db.commit()
        else:
            session_db.rollback()

        return {
            "ready": True,
            "mode": "apply" if apply else "dry-run",
            "user_id": user_id,
            "include_snapshots": include_snapshots,
            "scanned_rows": scanned_rows,
            "changed_rows": changed_rows,
            "sections": sections,
            "blockers": [],
            "warnings": warnings,
        }
    except Exception as exc:
        session_db.rollback()
        return {
            "ready": False,
            "mode": "apply" if apply else "dry-run",
            "user_id": user_id,
            "include_snapshots": include_snapshots,
            "scanned_rows": 0,
            "changed_rows": 0,
            "sections": {},
            "blockers": ["rich_text_maintenance_failed"],
            "warnings": [],
            "error": str(exc),
        }
    finally:
        session_db.close()
