"""
Authentication routes: login, register, logout, CLI create-user.
"""
import logging
import time
import threading

from flask import Blueprint, render_template, request, redirect, url_for
from flask_login import login_user, logout_user, login_required, current_user

from database import SessionLocal
from models import User

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

        username = request.form['username']
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
                return render_template('login.html', error="Invalid username or password")
        finally:
            session_db.close()

    return render_template('login.html')


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))

    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        session_db = SessionLocal()
        try:
            if session_db.query(User).filter_by(username=username).first():
                return render_template('register.html', error="Username already exists")

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


@auth_bp.route('/logout')
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
