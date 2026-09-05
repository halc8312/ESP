"""
Admin routes - the school office's view of student activity.

Only accounts with role='admin' can reach anything here; students get a 404 so
the screen's existence is not advertised to them.
"""
from datetime import datetime
from functools import wraps

from flask import (
    Blueprint,
    abort,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required

from database import SessionLocal
from services.admin_dashboard_service import (
    INACTIVE_LOGIN_DAYS,
    NO_VIEW_WINDOW_DAYS,
    build_student_activity,
    summarize_student_activity,
)
from services.student_account_service import (
    StudentAccountError,
    create_student,
    remember_issued_password,
    reset_student_password,
    resume_student,
    set_student_email,
    suspend_student,
    take_issued_password,
)

admin_bp = Blueprint("admin", __name__)

#: Names the freshly issued password held server-side for the next page load.
#: The cookie carries this token and never the password: Flask signs the session
#: but does not encrypt it. Not flashed either — the flash area is a toast that
#: dismisses itself, and the office needs long enough to write a password down.
_ISSUED_PASSWORD_KEY = "office_issued_password_token"


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
            issued_password=take_issued_password(session.pop(_ISSUED_PASSWORD_KEY, None)),
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


def _back_to_dashboard():
    return redirect(url_for("admin.admin_dashboard"))


def _office_action(handler):
    """
    Run one office action, and put its outcome in front of the operator.

    A failure the office can correct is shown as its own sentence; anything
    unexpected is left to the error handler rather than dressed up as advice.
    """
    session_db = SessionLocal()
    try:
        message, category = handler(session_db)
        flash(message, category)
    except StudentAccountError as exc:
        session_db.rollback()
        flash(str(exc), "error")
    except Exception:
        session_db.rollback()
        raise
    finally:
        session_db.close()
    return _back_to_dashboard()


@admin_bp.route("/admin/students/create", methods=["POST"])
@admin_required
def admin_create_student():
    def _handler(session_db):
        student, password = create_student(
            session_db,
            username=request.form.get("username"),
            email=request.form.get("email"),
        )
        session[_ISSUED_PASSWORD_KEY] = remember_issued_password(
            student.username, password, "created"
        )
        return f"生徒「{student.username}」を登録しました。", "success"

    return _office_action(_handler)


@admin_bp.route("/admin/students/<int:user_id>/email", methods=["POST"])
@admin_required
def admin_set_student_email(user_id):
    def _handler(session_db):
        student = set_student_email(session_db, user_id, request.form.get("email"))
        if student.email:
            return f"{student.username} のメールアドレスを {student.email} にしました。", "success"
        return f"{student.username} のメールアドレスを削除しました。", "success"

    return _office_action(_handler)


@admin_bp.route("/admin/students/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def admin_reset_student_password(user_id):
    def _handler(session_db):
        student, password = reset_student_password(session_db, user_id)
        session[_ISSUED_PASSWORD_KEY] = remember_issued_password(
            student.username, password, "reset"
        )
        return f"{student.username} の仮パスワードを再発行しました。", "success"

    return _office_action(_handler)


@admin_bp.route("/admin/students/<int:user_id>/suspend", methods=["POST"])
@admin_required
def admin_suspend_student(user_id):
    def _handler(session_db):
        student = suspend_student(
            session_db, user_id, acting_user_id=current_user.id
        )
        return (
            f"{student.username} の利用を停止しました。"
            "ログインと公開リストが止まります。商品や設定はそのまま残ります。",
            "success",
        )

    return _office_action(_handler)


@admin_bp.route("/admin/students/<int:user_id>/resume", methods=["POST"])
@admin_required
def admin_resume_student(user_id):
    def _handler(session_db):
        student = resume_student(session_db, user_id)
        return (
            f"{student.username} の利用を再開しました。公開リストも元どおり見られます。",
            "success",
        )

    return _office_action(_handler)


def _load_scrape_health_rows():
    # Keep the monitoring read separate from student/account administration.
    from services.scrape_health import list_scrape_health

    return list_scrape_health()


def _scrape_health_datetime(value):
    """Normalize the health service's ISO UTC timestamps for the JST filter."""
    if isinstance(value, datetime) or value is None:
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return None


@admin_bp.route("/admin/scrape-health")
@admin_required
def admin_scrape_health():
    """Read passive, aggregate health evidence; never run a scrape on page load."""
    rows = []
    for observation in _load_scrape_health_rows():
        # Explicit projection: do not accidentally expose future service fields
        # such as request payloads, product URLs, or notification destinations.
        row = {
            key: observation.get(key)
            for key in (
                "site", "route", "status", "reason", "latest_delivery_status",
                "consecutive_failures",
            )
        }
        count = row["consecutive_failures"]
        if type(count) is not int or count < 0:
            row["consecutive_failures"] = None
        row["incident_open"] = observation.get("incident_open") is True
        configured = observation.get("scrape_alert_configured")
        row["scrape_alert_configured"] = configured if type(configured) is bool else None
        for key in ("last_observed_at", "last_success_at", "last_failure_at"):
            row[key] = _scrape_health_datetime(observation.get(key))
        rows.append(row)

    response = make_response(render_template(
        "admin_scrape_health.html",
        health_rows=rows,
        all_shops=[],
        current_shop_id=None,
    ))
    response.headers["Cache-Control"] = "private, no-store"
    return response
