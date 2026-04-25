"""add durable scrape job tables

Revision ID: 20260323_0002
Revises: 20260323_0001
Create Date: 2026-03-23 00:02:00
"""
from __future__ import annotations

from alembic import op

from database import Base
from models import ScrapeJob, ScrapeJobEvent


revision = "20260323_0002"
down_revision = "20260323_0001"
branch_labels = None
depends_on = None


SCRAPE_JOB_TABLES = [
    ScrapeJob.__table__,
    ScrapeJobEvent.__table__,
]


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, tables=SCRAPE_JOB_TABLES)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind, tables=list(reversed(SCRAPE_JOB_TABLES)))
