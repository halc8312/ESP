"""add tracker dismissal timestamp to durable scrape jobs

Revision ID: 20260411_0004
Revises: 20260329_0003
Create Date: 2026-04-11 00:04:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260411_0004"
down_revision = "20260329_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    if "scrape_jobs" not in existing_tables:
        return

    existing_columns = {column["name"] for column in inspector.get_columns("scrape_jobs")}
    existing_indexes = {index["name"] for index in inspector.get_indexes("scrape_jobs")}

    with op.batch_alter_table("scrape_jobs") as batch_op:
        if "tracker_dismissed_at" not in existing_columns:
            batch_op.add_column(sa.Column("tracker_dismissed_at", sa.DateTime(), nullable=True))
        if "ix_scrape_jobs_tracker_dismissed_at" not in existing_indexes:
            batch_op.create_index("ix_scrape_jobs_tracker_dismissed_at", ["tracker_dismissed_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    if "scrape_jobs" not in existing_tables:
        return

    existing_indexes = {index["name"] for index in inspector.get_indexes("scrape_jobs")}
    if "ix_scrape_jobs_tracker_dismissed_at" in existing_indexes:
        with op.batch_alter_table("scrape_jobs") as batch_op:
            batch_op.drop_index("ix_scrape_jobs_tracker_dismissed_at")

    # Leave the additive column in place for compatibility with older durable rows.
