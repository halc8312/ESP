import pytest
import os
import sys
import uuid
from pathlib import Path

from sqlalchemy import inspect, text

# Add the application root to the path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Set test database URL before importing database module
os.environ["DATABASE_URL"] = "sqlite:///test_mercari.db"

import database
from app import create_app
from database import SessionLocal, Base
from services.rate_limit_service import reset_rate_limiter_for_tests


def _reset_sqlite_test_database_schema(target_engine):
    target_engine.dispose()
    with target_engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        inspector = inspect(connection)
        table_names = inspector.get_table_names()
        for table_name in table_names:
            connection.execute(text(f'DROP TABLE IF EXISTS "{table_name}"'))
        connection.execute(text("PRAGMA foreign_keys=ON"))


@pytest.fixture(autouse=True)
def _reset_feature_flag_env(monkeypatch):
    for env_name in (
        "ENABLE_SHARED_BROWSER_RUNTIME",
        "WARM_BROWSER_POOL",
        "BROWSER_POOL_WARM_SITES",
        "BROWSER_POOL_MAX_TASKS_BEFORE_RESTART",
        "BROWSER_POOL_MAX_RUNTIME_SECONDS",
        "MERCARI_USE_BROWSER_POOL_DETAIL",
        "MERCARI_BROWSER_POOL_MAX_TASKS_BEFORE_RESTART",
        "MERCARI_BROWSER_POOL_MAX_RUNTIME_SECONDS",
        "MERCARI_PATROL_USE_BROWSER_POOL",
        "SNKRDUNK_USE_BROWSER_POOL_DYNAMIC",
        "SNKRDUNK_BROWSER_POOL_MAX_TASKS_BEFORE_RESTART",
        "SNKRDUNK_BROWSER_POOL_MAX_RUNTIME_SECONDS",
        "SCRAPE_JOB_ORPHAN_TIMEOUT_SECONDS",
        "SELECTOR_REPAIR_STORE_MODE",
        "SELECTOR_REPAIR_MIN_SCORE",
        "SELECTOR_REPAIR_MIN_CANARIES",
        "SELECTOR_REPAIR_CANARY_URLS_MERCARI_DETAIL",
        "SELECTOR_REPAIR_CANARY_URLS_SNKRDUNK_DETAIL",
        "WORKER_BACKLOG_WARN_COUNT",
        "WORKER_BACKLOG_WARN_AGE_SECONDS",
        "WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP",
        "WORKER_SELECTOR_REPAIR_LIMIT",
        "SCRAPE_ALERT_WEBHOOK_URL",
        "SCRAPE_ALERT_COOLDOWN_SECONDS",
        "SCRAPE_ALERT_MAX_PER_WINDOW",
        "SCRAPE_ALERT_WINDOW_SECONDS",
        "SELECTOR_ALERT_WEBHOOK_URL",
        "OPERATIONAL_ALERT_WEBHOOK_URL",
        "OPERATIONAL_ALERT_COOLDOWN_SECONDS",
        "OPERATIONAL_ALERT_MAX_PER_WINDOW",
        "OPERATIONAL_ALERT_WINDOW_SECONDS",
        "APP_ENV",
        "ENVIRONMENT",
        "PYTHON_ENV",
        "RENDER",
        "RUNTIME_ROLE",
        "SECRET_KEY",
        "REDIS_URL",
        "VALKEY_URL",
        "ALLOW_PUBLIC_SIGNUP",
        "FORCE_HTTPS",
        "HSTS_ENABLED",
        "SESSION_COOKIE_SECURE",
    ):
        monkeypatch.delenv(env_name, raising=False)


@pytest.fixture
def app(monkeypatch):
    tmp_dir = Path(__file__).resolve().parent / ".tmp"
    tmp_dir.mkdir(exist_ok=True)
    database_path = tmp_dir / f"test_app_{uuid.uuid4().hex}.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)

    previous_engine = database.engine
    test_engine = database.create_app_engine(database_url)
    database.engine = test_engine
    SessionLocal.remove()
    SessionLocal.configure(bind=test_engine)

    # Reset the in-memory scrape queue singleton to prevent stale
    # thread-local sessions in reused ThreadPoolExecutor threads
    # from pointing at a disposed/deleted test database.
    import services.scrape_queue as _sq
    _sq._queue = None

    app = create_app(runtime_role="test", config_overrides={"TESTING": True, "WTF_CSRF_ENABLED": False})
    with app.app_context():
        reset_rate_limiter_for_tests()
        _reset_sqlite_test_database_schema(test_engine)
        Base.metadata.create_all(bind=test_engine)
        yield app
        reset_rate_limiter_for_tests()
        _reset_sqlite_test_database_schema(test_engine)

    SessionLocal.remove()
    test_engine.dispose()
    database.engine = previous_engine
    SessionLocal.configure(bind=database.engine)
    if database_path.exists():
        database_path.unlink()


@pytest.fixture
def client(app):
    with app.test_client() as client:
        yield client

@pytest.fixture
def db_session(app):
    session = database._session_factory()
    yield session
    session.close()
