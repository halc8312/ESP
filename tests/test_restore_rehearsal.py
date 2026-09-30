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


def inspected_postgres(bindings):
    return [{"Id": "a" * 64, "Config": {"Image": "postgres:18"},
             "NetworkSettings": {"Ports": {"5432/tcp": bindings}}}]


@pytest.mark.parametrize("bindings", [
    None, [],
    [{"HostIp": "0.0.0.0", "HostPort": "5432"}],
    [{"HostIp": "::", "HostPort": "5432"}],
    [{"HostIp": "203.0.113.1", "HostPort": "5432"}],
    [{"HostIp": "127.0.0.1", "HostPort": "55432"}],
    [{"HostIp": "127.0.0.1"}],
    [{"HostIp": "127.0.0.1", "HostPort": "5432"}, {"HostIp": "0.0.0.0", "HostPort": "5432"}],
    [{"HostIp": "127.0.0.1", "HostPort": "5432"}, {"HostIp": "::1", "HostPort": "55432"}],
])
def test_pg_mapping_mismatch_refuses_before_database_contact(monkeypatch, bindings):
    import psycopg
    commands = []
    def inspect(arguments, **kwargs):
        commands.append(arguments)
        assert arguments == ["inspect", "synthetic-pg"]
        return SimpleNamespace(stdout=json.dumps(inspected_postgres(bindings)).encode())
    monkeypatch.setattr(rehearsal, "docker_command", inspect)
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: pytest.fail("unverified port must never contact PostgreSQL"))
    monkeypatch.setattr(rehearsal, "database_names", lambda: pytest.fail("unverified port must stop before database setup"))
    with pytest.raises(rehearsal.RehearsalRefused, match="postgres_port_binding_required"):
        rehearsal.execute_rehearsal("synthetic-pg", 5432, 6379, "a" * 40)
    assert commands == [["inspect", "synthetic-pg"]]


@pytest.mark.parametrize("hosts,expected", [(["127.0.0.1"], "127.0.0.1"), (["::1"], "::1"), (["::1", "127.0.0.1"], "127.0.0.1")])
def test_same_inspect_verifies_loopback_port_and_returns_stable_container_id(monkeypatch, hosts, expected):
    bindings = [{"HostIp": host, "HostPort": "55432"} for host in hosts]
    monkeypatch.setattr(rehearsal, "docker_command", lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(inspected_postgres(bindings)).encode()))
    assert rehearsal.postgres_container_endpoint("synthetic-pg", 55432) == ("a" * 64, expected)
    from sqlalchemy.engine import make_url
    url = make_url(rehearsal.local_url(55432, rehearsal.database_names()[0], expected))
    assert url.host == expected and url.port == 55432


@pytest.mark.parametrize("bad_metadata", [[], [{}], {"Id": "a" * 64}, [{"Config": {"Image": "postgres:18"}, "NetworkSettings": {"Ports": {}}}]])
def test_missing_inspect_metadata_fails_closed(monkeypatch, bad_metadata):
    monkeypatch.setattr(rehearsal, "docker_command", lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(bad_metadata).encode()))
    with pytest.raises(rehearsal.RehearsalRefused):
        rehearsal.postgres_container_endpoint("synthetic-pg", 5432)


def test_authoritative_inspect_requires_the_same_pg18_image(monkeypatch):
    metadata = inspected_postgres([{"HostIp": "127.0.0.1", "HostPort": "5432"}])
    metadata[0]["Config"]["Image"] = "postgres:16"
    monkeypatch.setattr(rehearsal, "docker_command", lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(metadata).encode()))
    with pytest.raises(rehearsal.RehearsalRefused, match="postgres18_container_required"):
        rehearsal.postgres_container_endpoint("synthetic-pg", 5432)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_verified_endpoint_drives_both_container_cli_and_psycopg(monkeypatch, host):
    import psycopg
    import redis
    commands, connections = [], []
    def docker(arguments, **kwargs):
        commands.append(arguments)
        if arguments[0] == "inspect":
            return SimpleNamespace(stdout=json.dumps(inspected_postgres([{"HostIp": host, "HostPort": "55432"}])).encode())
        assert arguments[:2] == ["exec", "a" * 64]
        return SimpleNamespace(stdout=b"pg client (PostgreSQL) 18.0")
    class VerifiedContact(RuntimeError):
        pass
    def connect(**kwargs):
        connections.append(kwargs)
        raise VerifiedContact
    monkeypatch.setattr(rehearsal, "docker_command", docker)
    monkeypatch.setattr(redis, "Redis", lambda **kwargs: SimpleNamespace(ping=lambda: True, dbsize=lambda: 0))
    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(VerifiedContact):
        rehearsal.execute_rehearsal("synthetic-pg", 55432, 6379, "a" * 40)
    assert commands == [["inspect", "synthetic-pg"], ["exec", "a" * 64, "pg_dump", "--version"], ["exec", "a" * 64, "pg_restore", "--version"]]
    assert connections[0]["host"] == host and connections[0]["port"] == 55432
    assert connections[0]["dbname"] == "postgres"


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
