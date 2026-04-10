"""
Authentication routes: login, register, logout, account.
"""
import logging
import threading
import time

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from database import SessionLocal
from models import Shop, User

auth_bp = Blueprint('auth', __name__)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Simple in-memory login rate limiter
# ---------------------------------------------------------------------------
_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 900  # 15 minutes
_login_attempts: dict[str, list[float]] = {}
_login_lock = threading.Lock()


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() if forwarded else (request.remote_addr or "unknown")


def _is_rate_limited() -> bool:
    ip = _client_ip()
    now = time.monotonic()
    with _login_lock:
        timestamps = _login_attempts.get(ip, [])
        timestamps = [t for t in timestamps if now - t < _WINDOW_SECONDS]
        _login_attempts[ip] = timestamps
        return len(timestamps) >= _MAX_ATTEMPTS


def _record_failed_attempt() -> None:
    ip = _client_ip()
    with _login_lock:
        _login_attempts.setdefault(ip, []).append(time.monotonic())


def _clear_attempts() -> None:
    ip = _client_ip()
    with _login_lock:
        _login_attempts.pop(ip, None)


def _build_account_context(session_db):
    all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
    return {
        "all_shops": all_shops,
        "current_shop_id": session.get("current_shop_id"),
    }


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))

    if request.method == 'POST':
        if _is_rate_limited():
            return render_template(
                'login.html',
                error="ログイン試行回数が上限に達しました。しばらく経ってから再度お試しください。",
            )

        username = request.form['username'].strip()
        password = request.form['password']

        session_db = SessionLocal()
        try:
            user = session_db.query(User).filter_by(username=username).first()
            if user and user.check_password(password):
                _clear_attempts()
                login_user(user)
                return redirect(url_for('main.index'))
            else:
                _record_failed_attempt()
                return render_template('login.html', error="ユーザー名またはパスワードが違います")
        finally:
            session_db.close()

    return render_template('login.html')


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))

    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']

        session_db = SessionLocal()
        try:
            if not username or not password:
                return render_template('register.html', error="ユーザー名とパスワードを入力してください。")

            if session_db.query(User).filter_by(username=username).first():
                return render_template('register.html', error="このユーザー名はすでに使われています。")

            new_user = User(username=username)
            new_user.set_password(password)
            session_db.add(new_user)
            session_db.commit()

            # Auto login after registration
            login_user(new_user)
            return redirect(url_for('main.index'))
        except Exception:
            logger.exception("Registration failed for user %s", username)
            return render_template('register.html', error="登録中にエラーが発生しました。再度お試しください。")
        finally:
            session_db.close()

    return render_template('register.html')


@auth_bp.route('/account', methods=['GET', 'POST'])
@login_required
def account():
    session_db = SessionLocal()
    try:
        if request.method == 'POST':
            current_password = request.form.get('current_password', '')
            new_password = request.form.get('new_password', '')
            confirm_password = request.form.get('confirm_password', '')

            user = session_db.query(User).filter_by(id=current_user.id).first()
            if user is None:
                logout_user()
                return redirect(url_for('auth.login'))

            if not current_password or not new_password or not confirm_password:
                flash('3つとも入力してください。', 'error')
                return redirect(url_for('auth.account'))

            if not user.check_password(current_password):
                flash('今のパスワードが違います。', 'error')
                return redirect(url_for('auth.account'))

            if len(new_password) < 8:
                flash('新しいパスワードは8文字以上にしてください。', 'error')
                return redirect(url_for('auth.account'))

            if new_password != confirm_password:
                flash('新しいパスワードが一致していません。', 'error')
                return redirect(url_for('auth.account'))

            if user.check_password(new_password):
                flash('今と同じパスワードは使えません。', 'error')
                return redirect(url_for('auth.account'))

            user.set_password(new_password)
            try:
                session_db.commit()
            except Exception:
                logger.exception("Password change failed for user %s", user.username)
                session_db.rollback()
                flash('変更できませんでした。', 'error')
                return redirect(url_for('auth.account'))
            flash('パスワードを変更しました。', 'success')
            return redirect(url_for('auth.account'))

        return render_template('account.html', **_build_account_context(session_db))
    finally:
        session_db.close()


@auth_bp.route('/logout', methods=['GET', 'POST'])
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))


def register_cli_commands(app):
    """Register CLI commands related to authentication."""

    @app.cli.command("create-user")
    def create_user():
        import getpass
        username = input("Username: ")
        password = getpass.getpass("Password: ")

        session_db = SessionLocal()
        try:
            if session_db.query(User).filter_by(username=username).first():
                print("User already exists.")
                return

            new_user = User(username=username)
            new_user.set_password(password)
            session_db.add(new_user)
            session_db.commit()
            print(f"User {username} created successfully.")
        except Exception as e:
            print(f"Error: {e}")
            session_db.rollback()
        finally:
            session_db.close()
