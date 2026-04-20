"""add image_processing_jobs table for background removal Phase 1

Revision ID: 20260420_0007
Revises: 20260420_0006
Create Date: 2026-04-20 00:07:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260420_0007"
down_revision = "20260420_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    existing_tables = set(inspector.get_table_names())
    if "image_processing_jobs" in existing_tables:
        return

    op.create_table(
        "image_processing_jobs",
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
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("source_image_url", sa.String(), nullable=False),
        sa.Column("result_image_url", sa.String(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("job_id", name="uq_image_processing_jobs_job_id"),
    )
    op.create_index(
        "ix_image_processing_jobs_product_lookup",
        "image_processing_jobs",
        ["product_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_image_processing_jobs_job_id",
        "image_processing_jobs",
        ["job_id"],
        unique=False,
    )
    op.create_index(
        "ix_image_processing_jobs_product_id",
        "image_processing_jobs",
        ["product_id"],
        unique=False,
    )
    op.create_index(
        "ix_image_processing_jobs_user_id",
        "image_processing_jobs",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_image_processing_jobs_status",
        "image_processing_jobs",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_image_processing_jobs_created_at",
        "image_processing_jobs",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "image_processing_jobs" not in existing_tables:
        return

    existing_indexes = {
        index["name"] for index in inspector.get_indexes("image_processing_jobs")
    }
    for index_name in (
        "ix_image_processing_jobs_created_at",
        "ix_image_processing_jobs_status",
        "ix_image_processing_jobs_user_id",
        "ix_image_processing_jobs_product_id",
        "ix_image_processing_jobs_job_id",
        "ix_image_processing_jobs_product_lookup",
    ):
        if index_name in existing_indexes:
            op.drop_index(index_name, table_name="image_processing_jobs")
    op.drop_table("image_processing_jobs")
