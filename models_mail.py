"""Owner-only, durable notification state separate from public catalog data."""
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint

from database import Base
from time_utils import utc_now


class CatalogRequestNotification(Base):
    __tablename__ = "catalog_request_notifications"
    __table_args__ = (
        UniqueConstraint("request_id", "notification_type", name="uq_catalog_request_notification_type"),
        Index("ix_catalog_request_notification_due", "status", "next_attempt_at"),
        Index("ix_catalog_request_notification_owner", "user_id", "created_at"),
    )
    id = Column(Integer, primary_key=True)
    request_id = Column(Integer, ForeignKey("catalog_requests.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    notification_type = Column(String(32), nullable=False, default="request_created")
    recipient = Column(String(255))
    sender = Column(String(255))
    subject = Column(String(200), nullable=False)
    body = Column(Text, nullable=False)
    idempotency_key = Column(String(128), nullable=False, unique=True)
    status = Column(String(32), nullable=False)
    result_code = Column(String(64))
    message_id = Column(String(36))
    attempt_count = Column(Integer, nullable=False, default=0)
    dispatch_attempt_count = Column(Integer, nullable=False, default=0)
    first_attempt_at = Column(DateTime)
    last_attempt_at = Column(DateTime)
    next_attempt_at = Column(DateTime)
    claim_token = Column(String(64))
    lease_expires_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=utc_now)
    updated_at = Column(DateTime, nullable=False, default=utc_now)
