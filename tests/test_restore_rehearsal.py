import json
import os
import socket
from types import SimpleNamespace

import pytest

from scripts import restore_rehearsal as rehearsal


def test_default_preview_cannot_run_docker_or_restore(monkeypatch, capsys):
    monkeypatch.setattr(rehearsal, "execute_rehearsal", lambda *args: pytest.fail("execute requires opt-in"))
    assert rehearsal.main([]) == 0
    assert json.loads(capsys.readouterr().out)["restore_executed"] is False


def test_target_names_are_distinct_unique_and_unselectable():
    first, second = rehearsal.database_names(), rehearsal.database_names()
    assert len(set((*first, *second))) == 4
    assert "127.0.0.1" in rehearsal.local_url(5432, first[0])
    for name in ("postgres", "production", "esp_restore_target_existing"):
        with pytest.raises(rehearsal.RehearsalRefused):
            rehearsal.local_url(5432, name)


def test_execution_rejects_invalid_inputs_before_docker(monkeypatch):
    monkeypatch.setattr(rehearsal, "docker_command", lambda *args, **kwargs: pytest.fail("invalid inputs must not execute"))
    for container, port in (("remote; unsafe", 5432), ("ci", 0), ("", 5432)):
        with pytest.raises(rehearsal.RehearsalRefused, match="invalid_local_arguments"):
            rehearsal.execute_rehearsal(container, port, 6379)


def test_synthetic_environment_excludes_production_secrets_and_restores_context(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://private-production.invalid/db")
    monkeypatch.setenv("RESEND_API_KEY", "private")
    monkeypatch.setenv("MAIL_ENABLED", "true")
    name = rehearsal.database_names()[0]
    with rehearsal.synthetic_environment(5432, 6379, name, tmp_path):
        assert "127.0.0.1" in os.environ["DATABASE_URL"]
        assert "RESEND_API_KEY" not in os.environ
        assert os.environ["MAIL_ENABLED"] == "false"
        assert os.environ["WORKER_ENABLE_SCHEDULER"] == "0"
        assert os.environ["SCHEMA_BOOTSTRAP_MODE"] == "disabled"
    assert os.environ["MAIL_ENABLED"] == "true"
    assert os.environ["RESEND_API_KEY"] == "private"


def test_socket_guard_rejects_external_dns_and_connect_without_contact():
    original = socket.socket.connect
    with rehearsal.loopback_only({5432, 6379}):
        with pytest.raises(rehearsal.RehearsalRefused, match="external_network_forbidden"):
            socket.getaddrinfo("api.resend.com", 443)
        # Exercise rejection before the real socket method; never open a socket.
        for address in (("203.0.113.1", 443), ("127.0.0.1", 443)):
            with pytest.raises(rehearsal.RehearsalRefused, match="external_network_forbidden"):
                socket.socket.connect(object(), address)
    assert socket.socket.connect is original


def test_synthetic_fixture_covers_both_owners_and_current_outbox_schema(tmp_path):
    from sqlalchemy import create_engine, text
    from database import run_alembic_upgrade_for_database_url

    # This validates fixture construction, not PostgreSQL dump/restore.
    url = f"sqlite:///{tmp_path / 'fresh-synthetic.db'}"
    run_alembic_upgrade_for_database_url(url, revision=rehearsal.HEAD)
    engine = create_engine(url)
    try:
        rehearsal.seed_database(engine, tmp_path / "images")
        snapshot = rehearsal.snapshot_database(engine)
        assert all(row["count"] == 2 for row in snapshot.values())
        with engine.connect() as connection:
            notes = connection.execute(text("SELECT status,attempt_count FROM catalog_request_notifications ORDER BY id")).all()
            assert notes == [("accepted", 1), ("pending", 0)]
            assert connection.execute(text("SELECT count(*) FROM price_list_items i JOIN products p ON p.id=i.product_id JOIN price_lists l ON l.id=i.price_list_id WHERE p.user_id != l.user_id")).scalar_one() == 0
            assert connection.execute(text("SELECT count(*) FROM catalog_request_notifications n JOIN catalog_requests r ON r.id=n.request_id WHERE n.user_id != r.user_id")).scalar_one() == 0
        assert len(list((tmp_path / "images").glob("*.png"))) == 4
    finally:
        engine.dispose()


def test_restored_application_checks_use_preserved_rows_and_managed_images(tmp_path, monkeypatch):
    import app as app_module
    import database
    from services.mail_service import ResendMailer
    from sqlalchemy import create_engine

    url = f"sqlite:///{tmp_path / 'fresh-app.db'}"
    database.run_alembic_upgrade_for_database_url(url, revision=rehearsal.HEAD)
    engine = create_engine(url)
    images = tmp_path / "images"
    rehearsal.seed_database(engine, images)
    expected = rehearsal.snapshot_database(engine)
    previous_engine = database.engine
    database.SessionLocal.remove()
    monkeypatch.setattr(database, "engine", engine)
    database._session_factory.configure(bind=engine)
    database.SessionLocal.configure(bind=engine)
    monkeypatch.setattr(app_module, "IMAGE_STORAGE_PATH", str(images))
    monkeypatch.setattr(app_module, "_create_readiness_redis_client", lambda _url: SimpleNamespace(ping=lambda: True, close=lambda: None))
    # Ensure the helper's temporary send refusal is restored after this test.
    monkeypatch.setattr(ResendMailer, "send", ResendMailer.send)
    try:
        with rehearsal.synthetic_environment(5432, 6379, rehearsal.database_names()[1], images):
            result = rehearsal.verify_application(images)
        assert result == {"readyz": "ok", "tenant_relationships": "ok", "cross_tenant_access": "denied", "managed_media": "ok", "mail_sent": False}
        assert rehearsal.snapshot_database(engine) == expected
    finally:
        database.SessionLocal.remove()
        database._session_factory.configure(bind=previous_engine)
        database.SessionLocal.configure(bind=previous_engine)
        engine.dispose()
