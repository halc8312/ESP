import pytest

import database
from models import ScrapeJob, ScrapeJobEvent


def test_bootstrap_schema_auto_falls_back_to_legacy(monkeypatch):
    calls = []

    monkeypatch.setattr(database, "alembic_available", lambda: False)
    monkeypatch.setattr(database, "init_db", lambda bind=None: calls.append(("legacy", bind)))

    applied_mode = database.bootstrap_schema("auto")

    assert applied_mode == "legacy"
    assert calls == [("legacy", None)]


def test_bootstrap_schema_alembic_requires_dependency(monkeypatch):
    monkeypatch.setattr(database, "alembic_available", lambda: False)

    with pytest.raises(RuntimeError, match="Alembic bootstrap requested"):
        database.bootstrap_schema("alembic")


def test_scrape_job_tables_registered_in_metadata():
    assert ScrapeJob.__tablename__ in database.Base.metadata.tables
    assert ScrapeJobEvent.__tablename__ in database.Base.metadata.tables
