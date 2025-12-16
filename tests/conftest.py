import pytest
import os
import sys

# Add the application root to the path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Set test database URL before importing database module
os.environ["DATABASE_URL"] = "sqlite:///test_mercari.db"

from app import app
from database import init_db, SessionLocal, Base, engine

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
            # Ensure clean state
            Base.metadata.drop_all(bind=engine)
            # Create tables
            Base.metadata.create_all(bind=engine)
            yield client
            # Cleanup
            Base.metadata.drop_all(bind=engine)

@pytest.fixture
def db_session():
    session = SessionLocal()
    yield session
    session.close()
