import pytest
import os
import sys

# Add the application root to the path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Set test database URL before importing database module
os.environ["DATABASE_URL"] = "sqlite:///test_mercari.db"

from app import create_app
from database import SessionLocal, Base, engine


@pytest.fixture
def app():
    app = create_app(runtime_role="test", config_overrides={"TESTING": True})
    with app.app_context():
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        yield app
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(app):
    with app.test_client() as client:
        yield client

@pytest.fixture
def db_session():
    session = SessionLocal()
    yield session
    session.close()
