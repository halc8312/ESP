"""add price_lists.list_type

Distinguishes the permanent main list customers always see from the
time-limited lists that get closed once their window passes.

Revision ID: 20260807_0015
Revises: 20260729_0014
Create Date: 2026-08-07 00:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260807_0015"
down_revision = "20260729_0014"
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
    if "list_type" not in existing_columns:
        with op.batch_alter_table("price_lists") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "list_type",
                    sa.String(),
                    nullable=False,
                    server_default="permanent",
                )
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "price_lists" not in set(inspector.get_table_names()):
        return

    existing_columns = {
        column["name"] for column in inspector.get_columns("price_lists")
    }
    if "list_type" in existing_columns:
        with op.batch_alter_table("price_lists") as batch_op:
            batch_op.drop_column("list_type")
