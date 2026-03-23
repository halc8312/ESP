"""baseline current application schema

Revision ID: 20260323_0001
Revises:
Create Date: 2026-03-23 00:01:00
"""
from __future__ import annotations

from alembic import op

from database import Base
from models import (
    CatalogPageView,
    DescriptionTemplate,
    ExclusionKeyword,
    PriceList,
    PriceListItem,
    PricingRule,
    Product,
    ProductSnapshot,
    Shop,
    User,
    Variant,
)


revision = "20260323_0001"
down_revision = None
branch_labels = None
depends_on = None


BASELINE_TABLES = [
    User.__table__,
    Shop.__table__,
    PricingRule.__table__,
    Product.__table__,
    Variant.__table__,
    ProductSnapshot.__table__,
    DescriptionTemplate.__table__,
    ExclusionKeyword.__table__,
    PriceList.__table__,
    PriceListItem.__table__,
    CatalogPageView.__table__,
]


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, tables=BASELINE_TABLES)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind, tables=list(reversed(BASELINE_TABLES)))
