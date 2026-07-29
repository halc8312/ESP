"""add per-variant customer-facing selling price

Revision ID: 20260729_0013
Revises: 20260729_0012
Create Date: 2026-07-29 00:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260729_0013"
down_revision = "20260729_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "variants" not in set(inspector.get_table_names()):
        return

    existing_columns = {
        column["name"] for column in inspector.get_columns("variants")
    }
    if "selling_price" not in existing_columns:
        with op.batch_alter_table("variants") as batch_op:
            batch_op.add_column(
                sa.Column("selling_price", sa.Integer(), nullable=True)
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "variants" not in set(inspector.get_table_names()):
        return

    existing_columns = {
        column["name"] for column in inspector.get_columns("variants")
    }
    if "selling_price" in existing_columns:
        with op.batch_alter_table("variants") as batch_op:
            batch_op.drop_column("selling_price")
