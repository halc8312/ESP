"""add translation suggestions table and product source-hash columns

Revision ID: 20260420_0006
Revises: 20260411_0005
Create Date: 2026-04-20 00:06:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260420_0006"
down_revision = "20260411_0005"
branch_labels = None
depends_on = None


def _product_column_names(inspector) -> set[str]:
    try:
        return {column["name"] for column in inspector.get_columns("products")}
    except Exception:
        return set()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    product_columns = _product_column_names(inspector)
    with op.batch_alter_table("products") as batch:
        if "custom_title_en_source_hash" not in product_columns:
            batch.add_column(
                sa.Column("custom_title_en_source_hash", sa.String(length=64), nullable=True)
            )
        if "custom_description_en_source_hash" not in product_columns:
            batch.add_column(
                sa.Column(
                    "custom_description_en_source_hash",
                    sa.String(length=64),
                    nullable=True,
                )
            )

    existing_tables = set(inspector.get_table_names())
    if "translation_suggestions" not in existing_tables:
        op.create_table(
            "translation_suggestions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("job_id", sa.String(length=64), nullable=False),
            sa.Column(
                "product_id",
                sa.Integer(),
                sa.ForeignKey("products.id"),
                nullable=False,
            ),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id"),
                nullable=False,
            ),
            sa.Column("scope", sa.String(length=16), nullable=False),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("source_title", sa.Text(), nullable=True),
            sa.Column("source_description", sa.Text(), nullable=True),
            sa.Column("source_title_hash", sa.String(length=64), nullable=True),
            sa.Column("source_description_hash", sa.String(length=64), nullable=True),
            sa.Column("translated_title", sa.Text(), nullable=True),
            sa.Column("translated_description", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("job_id", name="uq_translation_suggestions_job_id"),
        )
        op.create_index(
            "ix_translation_suggestions_product_lookup",
            "translation_suggestions",
            ["product_id", "status", "created_at"],
            unique=False,
        )
        op.create_index(
            "ix_translation_suggestions_user_id",
            "translation_suggestions",
            ["user_id"],
            unique=False,
        )
        op.create_index(
            "ix_translation_suggestions_status",
            "translation_suggestions",
            ["status"],
            unique=False,
        )
        op.create_index(
            "ix_translation_suggestions_created_at",
            "translation_suggestions",
            ["created_at"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "translation_suggestions" in existing_tables:
        existing_indexes = {
            index["name"] for index in inspector.get_indexes("translation_suggestions")
        }
        for index_name in (
            "ix_translation_suggestions_created_at",
            "ix_translation_suggestions_status",
            "ix_translation_suggestions_user_id",
            "ix_translation_suggestions_product_lookup",
        ):
            if index_name in existing_indexes:
                op.drop_index(index_name, table_name="translation_suggestions")
        op.drop_table("translation_suggestions")

    product_columns = _product_column_names(inspector)
    with op.batch_alter_table("products") as batch:
        if "custom_description_en_source_hash" in product_columns:
            batch.drop_column("custom_description_en_source_hash")
        if "custom_title_en_source_hash" in product_columns:
            batch.drop_column("custom_title_en_source_hash")
