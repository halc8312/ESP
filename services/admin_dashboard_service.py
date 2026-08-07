"""
Student activity summary for the school office.

Students who get stuck rarely ask for help; they go quiet and drift away. The
signals that predict that are already in the database, so this reads them and
names the students worth a check-in.

Deliberately absent: sales and profit figures, and any ranking between
students. The office needs to know who to help, not who is earning what.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func

from models import CatalogPageView, PriceList, Product, User
from time_utils import utc_now

#: Days without a login before a student counts as having gone quiet.
INACTIVE_LOGIN_DAYS = 14
#: Window used for the "published but nobody looked" check.
NO_VIEW_WINDOW_DAYS = 7
#: A month-on-month view count below this share of the previous month is a drop.
VIEW_DROP_RATIO = 0.5

FOLLOW_UP_REASONS = {
    "inactive": f"{INACTIVE_LOGIN_DAYS}日以上ログインがありません",
    "no_pricelist": "商品はあるのに価格表がまだありません",
    "no_views": f"公開中の価格表が直近{NO_VIEW_WINDOW_DAYS}日で見られていません",
    "view_drop": "閲覧数が先月の半分以下に減っています",
}


def _counts_by_user(session_db, entity, *filters):
    query = session_db.query(entity.user_id, func.count(entity.id))
    for condition in filters:
        query = query.filter(condition)
    return dict(query.group_by(entity.user_id).all())


def _view_counts_by_user(session_db, since=None, until=None):
    query = (
        session_db.query(PriceList.user_id, func.count(CatalogPageView.id))
        .join(CatalogPageView, CatalogPageView.pricelist_id == PriceList.id)
    )
    if since is not None:
        query = query.filter(CatalogPageView.viewed_at >= since)
    if until is not None:
        query = query.filter(CatalogPageView.viewed_at < until)
    return dict(query.group_by(PriceList.user_id).all())


def build_student_activity(session_db, now=None):
    """Return one row per student plus the follow-up reasons that apply."""
    reference_time = now or utc_now()
    inactive_before = reference_time - timedelta(days=INACTIVE_LOGIN_DAYS)
    recent_views_since = reference_time - timedelta(days=NO_VIEW_WINDOW_DAYS)
    this_month_since = reference_time - timedelta(days=30)
    last_month_since = reference_time - timedelta(days=60)

    students = (
        session_db.query(User)
        .filter(User.role != "admin")
        .order_by(User.username)
        .all()
    )

    active_products = _counts_by_user(
        session_db, Product, Product.status == "active", Product.deleted_at.is_(None)
    )
    draft_products = _counts_by_user(
        session_db, Product, Product.status != "active", Product.deleted_at.is_(None)
    )
    permanent_lists = _counts_by_user(session_db, PriceList, PriceList.list_type != "limited")
    limited_lists = _counts_by_user(session_db, PriceList, PriceList.list_type == "limited")
    published_lists = _counts_by_user(session_db, PriceList, PriceList.is_active.is_(True))

    total_views = _view_counts_by_user(session_db)
    recent_views = _view_counts_by_user(session_db, since=recent_views_since)
    this_month_views = _view_counts_by_user(session_db, since=this_month_since)
    last_month_views = _view_counts_by_user(
        session_db, since=last_month_since, until=this_month_since
    )

    rows = []
    for student in students:
        active_count = active_products.get(student.id, 0)
        draft_count = draft_products.get(student.id, 0)
        product_count = active_count + draft_count
        permanent_count = permanent_lists.get(student.id, 0)
        limited_count = limited_lists.get(student.id, 0)
        pricelist_count = permanent_count + limited_count
        published_count = published_lists.get(student.id, 0)

        this_month = this_month_views.get(student.id, 0)
        last_month = last_month_views.get(student.id, 0)

        reasons = []
        if student.last_login_at is None or student.last_login_at < inactive_before:
            reasons.append("inactive")
        if product_count > 0 and pricelist_count == 0:
            reasons.append("no_pricelist")
        if published_count > 0 and recent_views.get(student.id, 0) == 0:
            reasons.append("no_views")
        if last_month > 0 and this_month < last_month * VIEW_DROP_RATIO:
            reasons.append("view_drop")

        rows.append(
            {
                "user_id": student.id,
                "username": student.username,
                "last_login_at": student.last_login_at,
                "active_product_count": active_count,
                "draft_product_count": draft_count,
                "product_count": product_count,
                "permanent_list_count": permanent_count,
                "limited_list_count": limited_count,
                "pricelist_count": pricelist_count,
                "published_list_count": published_count,
                "total_view_count": total_views.get(student.id, 0),
                "recent_view_count": recent_views.get(student.id, 0),
                "this_month_view_count": this_month,
                "last_month_view_count": last_month,
                "follow_up_reasons": [FOLLOW_UP_REASONS[reason] for reason in reasons],
                "needs_follow_up": bool(reasons),
            }
        )

    return rows


def summarize_student_activity(rows):
    """Headline counts shown above the student table."""
    return {
        "student_count": len(rows),
        "follow_up_count": sum(1 for row in rows if row["needs_follow_up"]),
        "product_count": sum(row["product_count"] for row in rows),
        "pricelist_count": sum(row["pricelist_count"] for row in rows),
        "started_count": sum(1 for row in rows if row["product_count"] > 0),
    }
