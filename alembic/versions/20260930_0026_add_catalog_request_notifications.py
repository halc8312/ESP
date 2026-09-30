"""Owner notification outbox; existing requests are never backfilled.

Revision ID: 20260930_0026
Revises: 20260930_0025
"""
import sqlalchemy as sa
from alembic import op

revision = "20260930_0026"
down_revision = "20260930_0025"
branch_labels = None
depends_on = None


def upgrade():
    if "catalog_request_notifications" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "catalog_request_notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("catalog_requests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("notification_type", sa.String(32), nullable=False),
        sa.Column("recipient", sa.String(255)), sa.Column("sender", sa.String(255)),
        sa.Column("subject", sa.String(200), nullable=False), sa.Column("body", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False), sa.Column("result_code", sa.String(64)),
        sa.Column("message_id", sa.String(36)), sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dispatch_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_attempt_at", sa.DateTime()), sa.Column("last_attempt_at", sa.DateTime()),
        sa.Column("next_attempt_at", sa.DateTime()), sa.Column("claim_token", sa.String(64)),
        sa.Column("lease_expires_at", sa.DateTime()), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("request_id", "notification_type", name="uq_catalog_request_notification_type"),
    )
    op.create_index("ix_catalog_request_notification_due", "catalog_request_notifications", ["status", "next_attempt_at"])
    op.create_index("ix_catalog_request_notification_owner", "catalog_request_notifications", ["user_id", "created_at"])


def downgrade():
    if "catalog_request_notifications" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("catalog_request_notifications")
