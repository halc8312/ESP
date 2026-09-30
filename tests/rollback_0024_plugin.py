"""Explicit compatibility gate: exercise old app fixtures on schema 0024.

Use PYTHONPATH=tests python -m pytest -p rollback_0024_plugin ...
Only isolated SQLite test-app databases are changed. No production URL is used.
"""
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
    import database

    url = database.engine.url
    root = Path(__file__).resolve().parent / ".tmp"
    database_path = Path(str(url.database or "")).resolve()
    if url.get_backend_name() != "sqlite" or root.resolve() not in database_path.parents:
        raise RuntimeError("Rollback schema gate refuses non-fixture databases")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["configured_sqlalchemy_url"] = str(url)
    config.attributes["skip_logging_config"] = True
    # Base.metadata.create_all() builds the old 0023 model. Record that known
    # test-only baseline and apply the actual retained additive migration.
    command.stamp(config, "20260929_0023")
    command.upgrade(config, "head")
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20260930_0024"
        columns = {column["name"]: column for column in inspect(connection).get_columns("products")}
        expected = {
            "detail_fetch_state", "detail_job_id", "detail_source_url", "detail_scope_key",
            "detail_lease_expires_at", "detail_retry_at", "detail_fail_count", "detail_error_code",
            "detail_translate_requested",
        }
        assert all(columns[name]["nullable"] for name in expected)
