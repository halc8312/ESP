"""
Admin routes - the school office's view of student activity.

Only accounts with role='admin' can reach anything here; students get a 404 so
the screen's existence is not advertised to them.
"""
from functools import wraps

from flask import Blueprint, abort, render_template
from flask_login import current_user, login_required

from database import SessionLocal
from services.admin_dashboard_service import (
    INACTIVE_LOGIN_DAYS,
    NO_VIEW_WINDOW_DAYS,
    build_student_activity,
    summarize_student_activity,
)

admin_bp = Blueprint("admin", __name__)


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not getattr(current_user, "is_admin", False):
            abort(404)
        return view(*args, **kwargs)

    return wrapper


@admin_bp.route("/admin")
@admin_required
def admin_dashboard():
    """Student activity overview with the follow-up list on top."""
    session_db = SessionLocal()
    try:
        rows = build_student_activity(session_db)
        return render_template(
            "admin_dashboard.html",
            students=rows,
            follow_ups=[row for row in rows if row["needs_follow_up"]],
            summary=summarize_student_activity(rows),
            inactive_login_days=INACTIVE_LOGIN_DAYS,
            no_view_window_days=NO_VIEW_WINDOW_DAYS,
            all_shops=[],
            current_shop_id=None,
        )
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()
