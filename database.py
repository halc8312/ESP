from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker


DEFAULT_DATABASE_URL = "sqlite:///mercari.db"


def get_database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def create_app_engine(database_url: str | None = None):
    resolved_url = database_url or get_database_url()
    print(f"DEBUG: Using database URL: {resolved_url}")
    engine = create_engine(resolved_url, echo=False)

    if "sqlite" in engine.url.drivername:
        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))

    return engine


engine = create_app_engine()
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


def init_db(bind=None):
    Base.metadata.create_all(bind or engine)


def alembic_available() -> bool:
    return bool(importlib.util.find_spec("alembic"))


def run_alembic_upgrade(revision: str = "head", config_path: str = "alembic.ini") -> str:
    if not alembic_available():
        raise RuntimeError("Alembic is not installed")

    from alembic import command
    from alembic.config import Config

    config = Config(config_path)
    config.set_main_option("sqlalchemy.url", get_database_url())
    command.upgrade(config, revision)
    return revision


def bootstrap_schema(schema_mode: str = "auto") -> str:
    normalized_mode = str(schema_mode or "auto").strip().lower()

    if normalized_mode == "disabled":
        return "disabled"

    if normalized_mode not in {"auto", "alembic", "legacy"}:
        raise ValueError(f"Unsupported schema bootstrap mode: {schema_mode}")

    if normalized_mode in {"auto", "alembic"} and Path("alembic.ini").exists():
        if alembic_available():
            run_alembic_upgrade()
            return "alembic"
        if normalized_mode == "alembic":
            raise RuntimeError("Alembic bootstrap requested but Alembic is not installed")

    init_db()
    return "legacy"
