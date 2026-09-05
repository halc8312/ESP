"""Add passive scrape-health aggregates, bounded observations, and alert outbox.

Revision ID: 20260905_0021
Revises: 20260827_0020
"""
import sqlalchemy as sa
from alembic import op

revision = "20260905_0021"
down_revision = "20260827_0020"
branch_labels = None
depends_on = None


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "scrape_health_states" not in existing:
        op.create_table(
            "scrape_health_states",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("site", sa.String(24), nullable=False),
            sa.Column("route", sa.String(16), nullable=False),
            sa.Column("last_observed_at", sa.DateTime(), nullable=False),
            sa.Column("last_success_at", sa.DateTime()),
            sa.Column("last_failure_at", sa.DateTime()),
            sa.Column("last_outcome", sa.String(24), nullable=False),
            sa.Column("reason", sa.String(32)),
            sa.Column("consecutive_failures", sa.Integer(), nullable=False),
            sa.Column("incident_open", sa.Boolean(), nullable=False),
            sa.Column("incident_number", sa.Integer(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.UniqueConstraint("site", "route", name="uq_scrape_health_site_route"),
        )
    if "scrape_health_observations" not in existing:
        op.create_table(
            "scrape_health_observations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("site", sa.String(24), nullable=False),
            sa.Column("route", sa.String(16), nullable=False),
            sa.Column("observed_at", sa.DateTime(), nullable=False),
            sa.Column("outcome", sa.String(24), nullable=False),
            sa.Column("reason", sa.String(32)),
            sa.Column("success_count", sa.Integer(), nullable=False),
            sa.Column("error_count", sa.Integer(), nullable=False),
        )
        op.create_index("ix_scrape_health_observation_route_time", "scrape_health_observations", ["site", "route", "observed_at"])
    if "scrape_health_deliveries" not in existing:
        op.create_table(
            "scrape_health_deliveries",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("site", sa.String(24), nullable=False),
            sa.Column("route", sa.String(16), nullable=False),
            sa.Column("incident_number", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(16), nullable=False),
            sa.Column("reason", sa.String(32)),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
            sa.Column("last_attempt_at", sa.DateTime()),
            sa.Column("delivered_at", sa.DateTime()),
            sa.Column("attempt_count", sa.Integer(), nullable=False),
            sa.Column("failure_count", sa.Integer(), nullable=False),
            sa.Column("error_type", sa.String(80)),
            sa.Column("claim_token", sa.String(32)),
            sa.Column("lease_expires_at", sa.DateTime()),
            sa.UniqueConstraint("site", "route", "incident_number", "event_type", name="uq_scrape_health_delivery_event"),
        )
        op.create_index("ix_scrape_health_delivery_due", "scrape_health_deliveries", ["status", "next_attempt_at"])


def downgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for table in ("scrape_health_deliveries", "scrape_health_observations", "scrape_health_states"):
        if table in existing:
            op.drop_table(table)
