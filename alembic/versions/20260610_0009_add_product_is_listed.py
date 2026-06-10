"""add product is_listed column

Revision ID: 20260610_0009
Revises: 20260424_0008
Create Date: 2026-06-10 04:30:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260610_0009"
down_revision = "20260424_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "products" not in set(inspector.get_table_names()):
        return

    existing_columns = {column["name"] for column in inspector.get_columns("products")}
    if "is_listed" not in existing_columns:
        with op.batch_alter_table("products") as batch_op:
            batch_op.add_column(
                sa.Column("is_listed", sa.Boolean(), nullable=True, server_default=sa.true())
            )

    bind.execute(sa.text("UPDATE products SET is_listed = TRUE WHERE is_listed IS NULL"))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "products" not in set(inspector.get_table_names()):
        return

    existing_columns = {column["name"] for column in inspector.get_columns("products")}
    if "is_listed" in existing_columns:
        with op.batch_alter_table("products") as batch_op:
            batch_op.drop_column("is_listed")
