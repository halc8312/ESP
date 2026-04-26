"""
Authentication routes: login, register, logout, account.
"""
import logging

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from database import SessionLocal
from models import Shop, User
from services.password_policy import validate_password_strength
from services.rate_limit_service import (
    get_client_ip,
    is_limited,
    record_attempt,
    reset_attempts,
)

auth_bp = Blueprint('auth', __name__)
logger = logging.getLogger(__name__)

def _build_account_context(session_db):
    all_shops = session_db.query(Shop).filter_by(user_id=current_user.id).all()
    return {
        "all_shops": all_shops,
        "current_shop_id": session.get("current_shop_id"),
    }


def _login_identifiers(username):
    client_ip = get_client_ip(request)
    normalized_username = (username or "").strip().lower() or "unknown"
    return client_ip, normalized_username


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        client_ip, normalized_username = _login_identifiers(username)
        limit = current_app.config["LOGIN_RATE_LIMIT"]
        window = current_app.config["LOGIN_RATE_WINDOW_SECONDS"]

        for scope, identifier in (
            ("login-ip", client_ip),
            ("login-user", normalized_username),
        ):
            decision = is_limited(scope, identifier, limit, window)
            if not decision.allowed:
                return render_template('login.html', error=decision.message), decision.status_code

        session_db = SessionLocal()
        try:
            user = session_db.query(User).filter_by(username=username).first()
            if user and user.check_password(password):
                login_user(user)
                reset_attempts("login-ip", client_ip)
                reset_attempts("login-user", normalized_username)
                return redirect(url_for('main.index'))
            else:
                record_attempt("login-ip", client_ip, window)
                record_attempt("login-user", normalized_username, window)
                return render_template('login.html', error="ユーザー名またはパスワードが違います")
        except Exception:
            session_db.rollback()
            raise
        finally:
            session_db.close()

    return render_template('login.html')


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))

    if not current_app.config.get("ALLOW_PUBLIC_SIGNUP", False):
        return render_template(
            'register.html',
            error="Public signup is disabled. Ask an administrator to create an account.",
        ), 403

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        client_ip = get_client_ip(request)
        limit = current_app.config["REGISTER_RATE_LIMIT"]
        window = current_app.config["REGISTER_RATE_WINDOW_SECONDS"]
        decision = is_limited("register-ip", client_ip, limit, window)
        if not decision.allowed:
            return render_template('register.html', error=decision.message), decision.status_code

        session_db = SessionLocal()
        try:
            if not username:
                record_attempt("register-ip", client_ip, window)
                return render_template('register.html', error="Username is required."), 400

            if session_db.query(User).filter_by(username=username).first():
                record_attempt("register-ip", client_ip, window)
                return render_template('register.html', error="このユーザー名はすでに使われています。")

            password_errors = validate_password_strength(password, username=username)
            if password_errors:
                record_attempt("register-ip", client_ip, window)
                return render_template('register.html', error=" ".join(password_errors)), 400

            new_user = User(username=username)
            new_user.set_password(password)
            session_db.add(new_user)
            session_db.commit()

            # Auto login after registration
            login_user(new_user)
            return redirect(url_for('main.index'))
        except Exception:
            logger.exception("Registration failed for user %s", username)
            session_db.rollback()
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

            password_errors = validate_password_strength(new_password, username=user.username)
            if password_errors:
                flash(' '.join(password_errors), 'error')
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
    except Exception:
        session_db.rollback()
        raise
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
        password_errors = validate_password_strength(password, username=username)
        if password_errors:
            print("Password rejected: " + " ".join(password_errors))
            return

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
