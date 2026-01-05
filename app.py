"""
Flask Application Entry Point

This module serves as the main application facade that:
- Creates and configures the Flask app
- Registers all blueprints (routes)
- Sets up Flask-Login
- Registers CLI commands
"""
import os
from flask import Flask, send_from_directory
from flask_login import LoginManager
from werkzeug.middleware.proxy_fix import ProxyFix

from database import SessionLocal, init_db
from models import User
from services.image_service import IMAGE_STORAGE_PATH

# ============================== 
# Flask アプリ設定
# ============================== 

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-this")


# ==============================
# Auto-Migration: Add missing columns
# ==============================
def run_migrations():
    """Safely add missing columns to existing tables."""
    from sqlalchemy import text
    from database import engine
    
    migrations = [
        # Pricing columns
        ("products", "pricing_rule_id", "ALTER TABLE products ADD COLUMN pricing_rule_id INTEGER"),
        ("products", "selling_price", "ALTER TABLE products ADD COLUMN selling_price INTEGER"),
        # Archive column
        ("products", "archived", "ALTER TABLE products ADD COLUMN archived BOOLEAN DEFAULT FALSE"),
        # Trash column
        ("products", "deleted_at", "ALTER TABLE products ADD COLUMN deleted_at DATETIME"),
    ]
    
    with engine.connect() as conn:
        for table, column, sql in migrations:
            try:
                # Check if column exists
                result = conn.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
            except Exception:
                # Column doesn't exist, add it
                try:
                    conn.execute(text(sql))
                    conn.commit()
                    print(f"Migration: Added column {column} to {table}")
                except Exception as e:
                    print(f"Migration error for {column}: {e}")

# Run migrations on startup
run_migrations()



# Scheduler Config
class SchedulerConfig:
    SCHEDULER_API_ENABLED = True

app.config.from_object(SchedulerConfig())

from flask_apscheduler import APScheduler
from services.monitor_service import MonitorService

scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()

# Register Job
@scheduler.task('interval', id='patrol_job', minutes=15)
def patrol_job():
    with app.app_context():
        MonitorService.check_stale_products(limit=15)

# Trash auto-purge job (runs daily at 3 AM)
@scheduler.task('cron', id='trash_purge_job', hour=3)
def trash_purge_job():
    with app.app_context():
        from routes.trash import purge_old_trash
        import logging
        logger = logging.getLogger("trash")
        count = purge_old_trash()
        if count > 0:
            logger.info(f"Auto-purged {count} items from trash")

# Render/Herokuなどのプロキシ環境下で正しいURLスキーム(https)を取得するための設定
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# ==============================
# Flask-Login setup
# ==============================
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'auth.login'


@login_manager.user_loader
def load_user(user_id):
    session_db = SessionLocal()
    try:
        return session_db.query(User).get(int(user_id))
    finally:
        session_db.close()


# ==============================
# Register Blueprints
# ==============================
from routes.auth import auth_bp, register_cli_commands as register_auth_cli
from routes.shops import shops_bp
from routes.main import main_bp
from routes.products import products_bp
from routes.scrape import scrape_bp
from routes.export import export_bp
from routes.pricing import pricing_bp
from routes.settings import settings_bp
from routes.import_routes import import_bp
from routes.archive import archive_bp
from routes.trash import trash_bp

app.register_blueprint(auth_bp)
app.register_blueprint(shops_bp)
app.register_blueprint(main_bp)
app.register_blueprint(products_bp)
app.register_blueprint(scrape_bp)
app.register_blueprint(export_bp)
app.register_blueprint(pricing_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(import_bp)
app.register_blueprint(archive_bp)
app.register_blueprint(trash_bp)






# ==============================
# Backward Compatibility Aliases
# For templates using old endpoint names
# ==============================
app.add_url_rule('/', endpoint='index', view_func=lambda: None, build_only=True)
app.add_url_rule('/dashboard', endpoint='dashboard', view_func=lambda: None, build_only=True)
app.add_url_rule('/login', endpoint='login', view_func=lambda: None, build_only=True)
app.add_url_rule('/register', endpoint='register', view_func=lambda: None, build_only=True)
app.add_url_rule('/logout', endpoint='logout', view_func=lambda: None, build_only=True)
app.add_url_rule('/shops', endpoint='manage_shops', view_func=lambda: None, build_only=True)
app.add_url_rule('/templates', endpoint='manage_templates', view_func=lambda: None, build_only=True)
app.add_url_rule('/set_current_shop', endpoint='set_current_shop', view_func=lambda: None, build_only=True)
app.add_url_rule('/product/<int:product_id>', endpoint='product_detail', view_func=lambda product_id: None, build_only=True)
app.add_url_rule('/shops/<int:shop_id>/delete', endpoint='delete_shop', view_func=lambda shop_id: None, build_only=True)
app.add_url_rule('/templates/<int:template_id>/delete', endpoint='delete_template', view_func=lambda template_id: None, build_only=True)
app.add_url_rule('/scrape', endpoint='scrape_form', view_func=lambda: None, build_only=True)
app.add_url_rule('/scrape/run', endpoint='scrape_run', view_func=lambda: None, build_only=True)
app.add_url_rule('/export/shopify', endpoint='export_shopify', view_func=lambda: None, build_only=True)
app.add_url_rule('/export_ebay', endpoint='export_ebay', view_func=lambda: None, build_only=True)
app.add_url_rule('/export_stock_update', endpoint='export_stock_update', view_func=lambda: None, build_only=True)
app.add_url_rule('/export_price_update', endpoint='export_price_update', view_func=lambda: None, build_only=True)


# ==============================
# Static Media Route
# ==============================
@app.route("/media/<path:filename>")
def serve_image(filename):
    return send_from_directory(IMAGE_STORAGE_PATH, filename)


# ==============================
# CLI Commands
# ==============================
register_auth_cli(app)

from cli import register_cli_commands
register_cli_commands(app)


# ==============================
# DB Initialization
# ==============================
with app.app_context():
    init_db()


# ==============================
# Entry Point
# ==============================
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))