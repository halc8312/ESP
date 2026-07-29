"""add price_lists.unpublish_at

Revision ID: 20260729_0011
Revises: 20260610_0010
Create Date: 2026-07-29 00:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260729_0011"
down_revision = "20260610_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "price_lists" not in set(inspector.get_table_names()):
        return

    existing_columns = {
        column["name"] for column in inspector.get_columns("price_lists")
    }
    if "unpublish_at" not in existing_columns:
        with op.batch_alter_table("price_lists") as batch_op:
            batch_op.add_column(sa.Column("unpublish_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "price_lists" not in set(inspector.get_table_names()):
        return

    existing_columns = {
        column["name"] for column in inspector.get_columns("price_lists")
    }
    if "unpublish_at" in existing_columns:
        with op.batch_alter_table("price_lists") as batch_op:
            batch_op.drop_column("unpublish_at")
