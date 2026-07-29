"""add product translation manual-ownership flags

Revision ID: 20260729_0012
Revises: 20260729_0011
Create Date: 2026-07-29 00:00:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260729_0012"
down_revision = "20260729_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "products" not in set(inspector.get_table_names()):
        return

    existing_columns = {
        column["name"] for column in inspector.get_columns("products")
    }
    with op.batch_alter_table("products") as batch_op:
        if "custom_title_en_manually_edited" not in existing_columns:
            batch_op.add_column(
                sa.Column(
                    "custom_title_en_manually_edited",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )
        if "custom_description_en_manually_edited" not in existing_columns:
            batch_op.add_column(
                sa.Column(
                    "custom_description_en_manually_edited",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )

    # Before ownership flags existed, the editor could intentionally clear an
    # automatically applied translation while leaving its source hash behind.
    # That state is distinguishable from "never translated" and must remain
    # protected from a late automatic job.
    products = sa.table(
        "products",
        sa.column("custom_title_en", sa.Text()),
        sa.column("custom_description_en", sa.Text()),
        sa.column("custom_title_en_source_hash", sa.String(64)),
        sa.column("custom_description_en_source_hash", sa.String(64)),
        sa.column("custom_title_en_manually_edited", sa.Boolean()),
        sa.column("custom_description_en_manually_edited", sa.Boolean()),
    )
    if {
        "custom_title_en",
        "custom_title_en_source_hash",
    }.issubset(existing_columns):
        op.execute(
            products.update()
            .where(
                sa.and_(
                    products.c.custom_title_en_source_hash.is_not(None),
                    sa.or_(
                        products.c.custom_title_en.is_(None),
                        sa.func.trim(products.c.custom_title_en) == "",
                    ),
                )
            )
            .values(custom_title_en_manually_edited=sa.true())
        )
    if {
        "custom_description_en",
        "custom_description_en_source_hash",
    }.issubset(existing_columns):
        op.execute(
            products.update()
            .where(
                sa.and_(
                    products.c.custom_description_en_source_hash.is_not(None),
                    sa.or_(
                        products.c.custom_description_en.is_(None),
                        sa.func.trim(products.c.custom_description_en) == "",
                    ),
                )
            )
            .values(custom_description_en_manually_edited=sa.true())
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "products" not in set(inspector.get_table_names()):
        return

    existing_columns = {
        column["name"] for column in inspector.get_columns("products")
    }
    with op.batch_alter_table("products") as batch_op:
        if "custom_description_en_manually_edited" in existing_columns:
            batch_op.drop_column("custom_description_en_manually_edited")
        if "custom_title_en_manually_edited" in existing_columns:
            batch_op.drop_column("custom_title_en_manually_edited")
