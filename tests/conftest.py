import pytest
import os
import sys
import tempfile
import uuid

# Add the application root to the path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Set test database URL before importing database module
TEST_DB_PATH = os.path.join(tempfile.gettempdir(), f"esp_test_mercari_{uuid.uuid4().hex}.db")
TEST_DB_URL_PATH = TEST_DB_PATH.replace(os.sep, "/")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB_URL_PATH}"

from app import app
from database import init_db, SessionLocal, Base, engine
from services.rate_limit_service import reset_rate_limiter_for_tests

@pytest.fixture
def client():
    # Use an in-memory database for testing
    app.config['TESTING'] = True
    # For SQLite, in-memory DB is created by path
    # But since our app uses a global SessionLocal/engine in database.py dependent on env var,
    # we might need to patch or ensure env var is set before import or handle it here.
    # Currently app.py calls init_db() on start.
    
    # Ideally we should override the database URL for tests, but database.py reads os.environ at import time.
    # So we can't easily switch to in-memory unless we reload or structure differently.
    # For now, let's assume we use a test.db file to avoid messing with production.
    
    # We rely on drop_all to clean up, avoiding file lock issues on Windows
    
    with app.test_client() as client:
        with app.app_context():
            reset_rate_limiter_for_tests()
            app.config.update(
                ALLOW_PUBLIC_SIGNUP=True,
                FORCE_HTTPS=False,
                HSTS_ENABLED=False,
                SESSION_COOKIE_SECURE=False,
                LOGIN_RATE_LIMIT=5,
                LOGIN_RATE_WINDOW_SECONDS=900,
                REGISTER_RATE_LIMIT=3,
                REGISTER_RATE_WINDOW_SECONDS=3600,
            )
            # Ensure clean state
            Base.metadata.drop_all(bind=engine)
            # Create tables
            Base.metadata.create_all(bind=engine)
            yield client
            reset_rate_limiter_for_tests()
            # Cleanup
            Base.metadata.drop_all(bind=engine)

@pytest.fixture
def db_session():
    session = SessionLocal()
    yield session
    session.close()
