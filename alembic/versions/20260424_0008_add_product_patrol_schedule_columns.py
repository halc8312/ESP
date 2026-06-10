"""add product patrol schedule columns

Revision ID: 20260424_0008
Revises: 20260422_0006
Create Date: 2026-04-24 00:08:00
"""
from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op


revision = "20260424_0008"
down_revision = "20260422_0006"
branch_labels = None
depends_on = None


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    if "products" not in existing_tables:
        return

    existing_columns = {column["name"] for column in inspector.get_columns("products")}
    existing_indexes = {index["name"] for index in inspector.get_indexes("products")}

    with op.batch_alter_table("products") as batch_op:
        if "last_patrolled_at" not in existing_columns:
            batch_op.add_column(sa.Column("last_patrolled_at", sa.DateTime(), nullable=True))
        if "next_patrol_at" not in existing_columns:
            batch_op.add_column(sa.Column("next_patrol_at", sa.DateTime(), nullable=True))
        if "ix_products_last_patrolled_at" not in existing_indexes:
            batch_op.create_index("ix_products_last_patrolled_at", ["last_patrolled_at"], unique=False)
        if "ix_products_next_patrol_at" not in existing_indexes:
            batch_op.create_index("ix_products_next_patrol_at", ["next_patrol_at"], unique=False)

    if {"patrol_fail_count", "updated_at"}.issubset(existing_columns):
        # Earlier patrol backoff used products.updated_at as the retry cursor.
        # Preserve that retry time while removing future timestamps from the
        # product-list sort key.
        now = _utc_now()
        bind.execute(
            sa.text(
                """
                UPDATE products
                SET next_patrol_at = updated_at,
                    last_patrolled_at = :now,
                    updated_at = :now
                WHERE patrol_fail_count > 0
                  AND updated_at > :now
                  AND next_patrol_at IS NULL
                """
            ),
            {"now": now},
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    if "products" not in existing_tables:
        return

    existing_columns = {column["name"] for column in inspector.get_columns("products")}
    existing_indexes = {index["name"] for index in inspector.get_indexes("products")}

    with op.batch_alter_table("products") as batch_op:
        if "ix_products_next_patrol_at" in existing_indexes:
            batch_op.drop_index("ix_products_next_patrol_at")
        if "ix_products_last_patrolled_at" in existing_indexes:
            batch_op.drop_index("ix_products_last_patrolled_at")
        if "next_patrol_at" in existing_columns:
            batch_op.drop_column("next_patrol_at")
        if "last_patrolled_at" in existing_columns:
            batch_op.drop_column("last_patrolled_at")
