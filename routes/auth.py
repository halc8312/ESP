"""
Authentication routes: login, register, logout, CLI create-user.
"""
from flask import Blueprint, current_app, render_template, request, redirect, url_for
from flask_login import login_user, logout_user, login_required, current_user

from database import SessionLocal
from models import User
from services.password_policy import validate_password_strength
from services.rate_limit_service import (
    get_client_ip,
    is_limited,
    record_attempt,
    reset_attempts,
)

auth_bp = Blueprint('auth', __name__)


def _rate_limit_response(decision):
    return render_template("login.html", error=decision.message), decision.status_code


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
                return _rate_limit_response(decision)
        
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
                return render_template('login.html', error="Invalid username or password")
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
                return render_template('register.html', error="Username already exists")

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
        except Exception as e:
            return render_template('register.html', error=f"Error: {e}")
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
