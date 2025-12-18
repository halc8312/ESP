"""
Authentication routes: login, register, logout, CLI create-user.
"""
from flask import Blueprint, render_template, request, redirect, url_for
from flask_login import login_user, logout_user, login_required, current_user

from database import SessionLocal
from models import User

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
        
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        session_db = SessionLocal()
        try:
            user = session_db.query(User).filter_by(username=username).first()
            if user and user.check_password(password):
                login_user(user)
                return redirect(url_for('main.index'))
            else:
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
