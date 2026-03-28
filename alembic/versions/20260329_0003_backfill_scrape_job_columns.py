"""backfill additive scrape job columns on existing databases

Revision ID: 20260329_0003
Revises: 20260323_0002
Create Date: 2026-03-29 00:03:00
"""
from __future__ import annotations

from alembic import op

from database import apply_additive_startup_migrations


revision = "20260329_0003"
down_revision = "20260323_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    apply_additive_startup_migrations(bind=bind)


def downgrade() -> None:
    # Additive compatibility columns are intentionally left in place.
    pass
