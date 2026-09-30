"""Opt-in gate: run the retained PR184 application against additive schema26.

Use PYTHONPATH=tests python -m pytest -p rollback_0026_plugin ...
Only isolated SQLite test-app fixtures are changed; production URLs are refused.
"""
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    outcome = yield
    if fixturedef.argname != "app" or outcome.excinfo is not None:
        return
    app = outcome.get_result()
    if not app.config.get("TESTING"):
        raise RuntimeError("Rollback schema gate requires the isolated test app")
    if str(os.environ.get("RECORDCITY_LISTING_ENABLED", "false")).lower() in ("true", "1", "yes", "on"):
        raise RuntimeError("Rollback schema gate requires RECORDCITY_LISTING_ENABLED=false")
    import database

    url = database.engine.url
    root = Path(__file__).resolve().parent / ".tmp"
    database_path = Path(str(url.database or "")).resolve()
    if url.get_backend_name() != "sqlite" or root.resolve() not in database_path.parents:
        raise RuntimeError("Rollback schema gate refuses non-fixture databases")
    if {"product_thumbnail_jobs", "catalog_request_notifications"}.intersection(database.Base.metadata.tables):
        raise RuntimeError("Rollback gate requires the unchanged PR184 model metadata")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["configured_sqlalchemy_url"] = str(url)
    config.attributes["skip_logging_config"] = True
    # This application's create_all metadata represents schema0024. Apply the
    # actual two retained standalone migrations rather than mock schema columns.
    command.stamp(config, "20260930_0024")
    command.upgrade(config, "head")
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20260930_0026"
        assert {"product_thumbnail_jobs", "catalog_request_notifications"}.issubset(inspect(connection).get_table_names())
        assert connection.execute(text("SELECT count(*) FROM product_thumbnail_jobs")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM catalog_request_notifications")).scalar_one() == 0
