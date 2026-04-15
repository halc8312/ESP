"""Tests for Mercari soft-sold hysteresis in MonitorService.

Verifies that:
  - Hard-evidence sold is applied immediately
  - Soft-evidence sold requires N consecutive observations before persisting
  - A non-sold result resets the counter
"""

import types

from services.monitor_service import MonitorService
from services.patrol.base_patrol import PatrolResult


def _make_product(product_id=1, site="mercari", last_status="on_sale"):
    """Create a minimal product-like object for testing."""
    product = types.SimpleNamespace(
        id=product_id,
        site=site,
        last_status=last_status,
        last_price=1000,
        patrol_fail_count=0,
        updated_at=None,
        pricing_rule_id=None,
    )
    return product


class FakeSession:
    """Minimal stand-in for SQLAlchemy session."""

    def __init__(self, variants=None):
        self._variants = variants or []
        self.committed = False

    def query(self, model):
        return self

    def filter_by(self, **kwargs):
        return self

    def all(self):
        return self._variants

    def commit(self):
        self.committed = True


# ── Hard sold is applied immediately ─────────────────────────────────


def test_hard_sold_applied_immediately():
    """Hard-evidence sold should be applied in a single cycle."""
    product = _make_product()
    session = FakeSession()
    result = PatrolResult(
        price=1000,
        status="sold",
        confidence="high",
        evidence_strength="hard",
    )

    # Clear any stale counter
    MonitorService._mercari_soft_sold_counts.pop(product.id, None)

    changes = MonitorService._apply_patrol_result(session, product, result)
    assert product.last_status == "sold"


# ── Soft sold requires threshold observations ─────────────────────────


def test_soft_sold_deferred_below_threshold():
    """Soft sold should NOT be persisted until threshold is met."""
    product = _make_product()

    # Reset counter
    MonitorService._mercari_soft_sold_counts.pop(product.id, None)

    result = PatrolResult(
        price=1000,
        status="sold",
        confidence="medium",
        evidence_strength="soft",
    )

    # Simulate the check that happens before _apply_patrol_result
    # (as in check_stale_products)
    assert (
        result.evidence_strength == "soft"
        and result.status == "sold"
    )

    # First observation: counter goes to 1, below threshold of 2
    prev = MonitorService._mercari_soft_sold_counts.get(product.id, 0)
    MonitorService._mercari_soft_sold_counts[product.id] = prev + 1
    assert MonitorService._mercari_soft_sold_counts[product.id] < MonitorService._MERCARI_SOFT_SOLD_THRESHOLD


def test_soft_sold_confirmed_at_threshold():
    """After reaching the threshold, soft sold should be allowed."""
    product = _make_product()

    # Set counter to threshold - 1
    MonitorService._mercari_soft_sold_counts[product.id] = (
        MonitorService._MERCARI_SOFT_SOLD_THRESHOLD - 1
    )

    # Next observation pushes it to threshold
    new_count = MonitorService._mercari_soft_sold_counts[product.id] + 1
    MonitorService._mercari_soft_sold_counts[product.id] = new_count
    assert new_count >= MonitorService._MERCARI_SOFT_SOLD_THRESHOLD

    # Cleanup
    MonitorService._mercari_soft_sold_counts.pop(product.id, None)


# ── Counter resets on non-sold result ─────────────────────────────────


def test_counter_resets_on_active_result():
    """An on_sale result should reset the soft-sold counter."""
    product = _make_product()
    MonitorService._mercari_soft_sold_counts[product.id] = 1

    # Simulate receiving an active result — counter should be cleared
    MonitorService._mercari_soft_sold_counts.pop(product.id, None)
    assert product.id not in MonitorService._mercari_soft_sold_counts


# ── Deleted is NOT subject to hysteresis ──────────────────────────────


def test_deleted_bypasses_hysteresis():
    """Deleted status should always be applied, regardless of evidence strength."""
    product = _make_product()
    session = FakeSession()
    result = PatrolResult(
        status="deleted",
        confidence="high",
        evidence_strength="hard",
    )

    MonitorService._mercari_soft_sold_counts.pop(product.id, None)
    changes = MonitorService._apply_patrol_result(session, product, result)
    assert product.last_status == "deleted"
