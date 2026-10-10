import pytest

import database


def test_postgres_driver_and_sql_defaults_are_distinct(monkeypatch):
    for key in ("DB_CONNECT_TIMEOUT_SECONDS", "DB_STATEMENT_TIMEOUT_MS", "DB_LOCK_TIMEOUT_MS", "PGCONNECT_TIMEOUT", "PGOPTIONS"):
        monkeypatch.delenv(key, raising=False)
    assert database.postgres_timeout_connect_args("postgresql://u:p@db/esp") == {
        "connect_timeout": 5,
        "options": "-c statement_timeout=15000 -c lock_timeout=5000",
    }


def test_explicit_url_settings_are_preserved(monkeypatch):
    monkeypatch.setenv("DB_CONNECT_TIMEOUT_SECONDS", "invalid-unused-value")
    args = database.postgres_timeout_connect_args(
        "postgresql://u:p@db/esp?connect_timeout=7&options=-c%20statement_timeout%3D23000"
    )
    assert "connect_timeout" not in args
    assert args["options"] == "-c statement_timeout=23000 -c lock_timeout=5000"


def test_configurable_limits_preserve_other_postgres_options(monkeypatch):
    monkeypatch.setenv("DB_CONNECT_TIMEOUT_SECONDS", "8")
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "21000")
    monkeypatch.setenv("DB_LOCK_TIMEOUT_MS", "4000")
    args = database.postgres_timeout_connect_args("postgresql://u:p@db/esp?options=-c%20application_name%3Desp")
    assert args["connect_timeout"] == 8
    assert args["options"] == "-c application_name=esp -c statement_timeout=21000 -c lock_timeout=4000"


def test_libpq_environment_settings_are_preserved(monkeypatch):
    monkeypatch.delenv("DB_CONNECT_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "9")
    monkeypatch.setenv("PGOPTIONS", "-c search_path=esp -c statement_timeout=22000")
    args = database.postgres_timeout_connect_args("postgresql://u:p@db/esp")
    assert args["connect_timeout"] == 9
    assert args["options"] == "-c search_path=esp -c statement_timeout=22000 -c lock_timeout=5000"


@pytest.mark.parametrize("value", ["0", "-1", "bad"])
def test_unbounded_or_invalid_url_connect_timeout_is_rejected(value):
    with pytest.raises(ValueError, match="connect_timeout"):
        database.postgres_timeout_connect_args(f"postgresql://u:p@db/esp?connect_timeout={value}")


def test_sqlite_receives_no_postgres_options(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_CONNECT_TIMEOUT_SECONDS", "invalid")
    assert database.postgres_timeout_connect_args("sqlite:///:memory:") == {}
    engine = database.create_app_engine(f"sqlite:///{tmp_path / 'timeouts.db'}")
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
    finally:
        engine.dispose()


def test_app_engine_passes_driver_limits_without_changing_pool_contract(monkeypatch):
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(database, "create_engine", fake_create_engine)
    database.create_app_engine("postgresql://u:p@db/esp")
    assert captured["pool_timeout"] == 30
    assert captured["pool_size"] == 5
    assert captured["max_overflow"] == 10
    assert captured["connect_args"]["connect_timeout"] == 5
