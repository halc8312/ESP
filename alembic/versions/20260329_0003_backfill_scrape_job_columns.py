"""backfill additive scrape job columns on existing databases

Revision ID: 20260329_0003
Revises: 20260323_0002
Create Date: 2026-03-29 00:03:00
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260329_0003"
down_revision = "20260323_0002"
branch_labels = None
depends_on = None


SCRAPE_JOB_COLUMNS = (
    sa.Column("logical_job_id", sa.String(length=64), nullable=True),
    sa.Column("parent_job_id", sa.String(length=64), nullable=True),
    sa.Column("context_payload", sa.Text(), nullable=True),
    sa.Column("progress_current", sa.Integer(), nullable=True),
    sa.Column("progress_total", sa.Integer(), nullable=True),
    sa.Column("result_payload", sa.Text(), nullable=True),
    sa.Column("error_payload", sa.Text(), nullable=True),
)

SCRAPE_JOB_EVENT_COLUMNS = (
    sa.Column("payload", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(), nullable=True),
)


def _get_existing_columns(bind, table_name: str) -> set[str]:
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    if table_name not in existing_tables:
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    scrape_job_columns = _get_existing_columns(bind, "scrape_jobs")
    if scrape_job_columns:
        with op.batch_alter_table("scrape_jobs") as batch_op:
            for column in SCRAPE_JOB_COLUMNS:
                if column.name not in scrape_job_columns:
                    batch_op.add_column(column.copy())

    scrape_job_event_columns = _get_existing_columns(bind, "scrape_job_events")
    if scrape_job_event_columns:
        with op.batch_alter_table("scrape_job_events") as batch_op:
            for column in SCRAPE_JOB_EVENT_COLUMNS:
                if column.name not in scrape_job_event_columns:
                    batch_op.add_column(column.copy())


def downgrade() -> None:
    # Additive compatibility columns are intentionally left in place.
    pass
