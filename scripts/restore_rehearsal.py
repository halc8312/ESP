#!/usr/bin/env python3
"""Explicit synthetic-only PostgreSQL 18 dump/restore rehearsal.

No production URL, credentials, database name, or existing restore target is
accepted. This creates two uniquely named local databases, never drops/cleans
data, and leaves cleanup to the disposable CI service lifecycle.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tarfile
import tempfile
import uuid


CI_USER = "esp_restore_ci"
CI_PASSWORD = "esp-restore-ci-only"
HEAD = "20260930_0026"
TABLES = (
    "users", "shops", "products", "variants", "product_snapshots", "price_lists",
    "price_list_items", "catalog_requests", "catalog_request_items",
    "product_thumbnail_jobs", "catalog_request_notifications",
)


class RehearsalRefused(RuntimeError):
    pass


def database_names():
    suffix = uuid.uuid4().hex
    return f"esp_restore_source_{suffix}", f"esp_restore_target_{suffix}"


def local_url(port, database_name):
    if not 1 <= port <= 65535 or not re.fullmatch(r"esp_restore_(source|target)_[0-9a-f]{32}", database_name):
        raise RehearsalRefused("invalid_local_target")
    return f"postgresql+psycopg://{CI_USER}:{CI_PASSWORD}@127.0.0.1:{port}/{database_name}?sslmode=disable"


@contextmanager
def synthetic_environment(pg_port, redis_port, database_name, images):
    safe_url = local_url(pg_port, database_name)
    if not 1 <= redis_port <= 65535:
        raise RehearsalRefused("invalid_local_target")
    previous = dict(os.environ)
    path = previous.get("PATH", os.defpath)
    os.environ.clear()
    os.environ.update({
        "PATH": path, "APP_ENV": "test", "RUNTIME_ROLE": "web",
        "DATABASE_URL": safe_url,
        "REDIS_URL": f"redis://127.0.0.1:{redis_port}/0",
        "SECRET_KEY": "synthetic-restore-only-secret-key-32-characters-minimum",
        "SCRAPE_QUEUE_BACKEND": "rq", "SCHEMA_BOOTSTRAP_MODE": "disabled",
        "WEB_SCHEDULER_MODE": "disabled", "WORKER_ENABLE_SCHEDULER": "0",
        "RQ_WITH_SCHEDULER": "0", "WARM_BROWSER_POOL": "0",
        "WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP": "0",
        "WORKER_RECONCILE_STALLED_JOBS_ON_STARTUP": "0",
        "MAIL_ENABLED": "false", "CATALOG_REQUEST_NOTIFICATIONS_ENABLED": "false",
        "RECORDCITY_LISTING_ENABLED": "false", "FORCE_HTTPS": "false",
        "SESSION_COOKIE_SECURE": "false", "IMAGE_STORAGE_PATH": str(images),
    })
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


@contextmanager
def loopback_only(ports):
    """Also block accidental Python DNS/HTTP/mail calls during app checks."""
    connect, connect_ex, getaddrinfo = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo

    def allow(address):
        if not isinstance(address, tuple) or len(address) < 2 or address[0] not in {"127.0.0.1", "::1"} or address[1] not in ports:
            raise RehearsalRefused("external_network_forbidden")

    def safe_connect(sock, address):
        allow(address)
        return connect(sock, address)

    def safe_connect_ex(sock, address):
        allow(address)
        return connect_ex(sock, address)

    def safe_getaddrinfo(host, port, *args, **kwargs):
        if host not in {"127.0.0.1", "::1"}:
            raise RehearsalRefused("external_network_forbidden")
        return getaddrinfo(host, port, *args, **kwargs)

    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = safe_connect, safe_connect_ex, safe_getaddrinfo
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, getaddrinfo


def docker_command(arguments, **kwargs):
    # Ignore inherited DOCKER_HOST/context and always use the local daemon.
    result = subprocess.run(
        ["docker", "--host=unix:///var/run/docker.sock", *arguments],
        env={"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8"},
        stderr=subprocess.PIPE, timeout=120, **kwargs,
    )
    if result.returncode:
        raise RehearsalRefused("local_docker_command_failed")
    return result


def seed_database(engine, images):
    from PIL import Image
    from sqlalchemy import text
    from sqlalchemy.orm import Session
    from models import CatalogRequest, CatalogRequestItem, PriceList, PriceListItem, Product, ProductSnapshot, Shop, User, Variant
    from models_mail import CatalogRequestNotification
    from time_utils import utc_now

    images.mkdir(parents=True, exist_ok=False)
    with Session(engine) as session:
        for marker in ("a", "b"):
            image_url = f"/media/restore-{marker}.png"
            Image.new("RGB", (8, 8), "blue").save(images / f"restore-{marker}.png")
            Image.new("RGB", (8, 8), "green").save(images / f"logo-{marker}.png")
            owner = User(username=f"restore_owner_{marker}", email=f"owner-{marker}@example.invalid")
            owner.set_password("restore-ci-login-only")
            session.add(owner)
            session.flush()
            shop = Shop(user_id=owner.id, name=f"Restore shop {marker}", logo_url=f"/media/logo-{marker}.png")
            session.add(shop)
            session.flush()
            product = Product(user_id=owner.id, shop_id=shop.id, site="manual",
                              source_url=f"https://supplier.example.invalid/private-{marker}",
                              last_title=f"Restore product {marker}", custom_title=f"Restore product {marker}",
                              last_price=333, selling_price=1400, last_status="on_sale",
                              variants=[Variant(price=333, inventory_qty=2)])
            catalog = PriceList(user_id=owner.id, shop_id=shop.id, name=f"Restore catalog {marker}", token=f"restore-catalog-{marker}")
            session.add_all([product, catalog])
            session.flush()
            snapshot = ProductSnapshot(product_id=product.id, title=product.last_title, price=333,
                                       status="on_sale", image_urls=image_url)
            session.add(snapshot)
            session.flush()
            session.add(PriceListItem(price_list_id=catalog.id, product_id=product.id))
            request = CatalogRequest(user_id=owner.id, shop_id=shop.id, pricelist_id=catalog.id,
                                     reference=marker * 20, submission_key=marker * 32, payload_hash=marker * 64,
                                     pricelist_name=catalog.name, shop_name=shop.name, buyer_instagram=f"fake_buyer_{marker}",
                                     message=f"Synthetic private message {marker}",
                                     items=[CatalogRequestItem(product_id=product.id, title_snapshot=product.last_title,
                                                               price_jpy_snapshot=1400, quantity=1)])
            session.add(request)
            session.flush()
            session.add(CatalogRequestNotification(request_id=request.id, user_id=owner.id,
                        notification_type="request_created", recipient=owner.email, sender="sender@example.invalid",
                        subject="Synthetic restore notification", body="Synthetic private body", idempotency_key=f"restore-only/{request.id}",
                        status="accepted" if marker == "a" else "pending", attempt_count=1 if marker == "a" else 0,
                        message_id="11111111-1111-4111-8111-111111111111" if marker == "a" else None))
            session.execute(text("""INSERT INTO product_thumbnail_jobs
                (product_id,user_id,shop_id,product_source_url,source_snapshot_id,source_image_url,state,
                 managed_image_url,created_at,updated_at) VALUES
                (:product,:owner,:shop,:source,:snapshot,:image,'complete',:managed,:created,:updated)"""),
                {"product": product.id, "owner": owner.id, "shop": shop.id, "source": product.source_url,
                 "snapshot": snapshot.id, "image": f"https://cdn.example.invalid/{marker}.png",
                 "managed": image_url, "created": utc_now(), "updated": utc_now()})
        session.commit()


def snapshot_database(engine):
    from sqlalchemy import text
    result = {}
    with engine.connect() as connection:
        if connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all() != [HEAD]:
            raise RehearsalRefused("unexpected_migration_head")
        for table in TABLES:
            # The identifiers are a fixed source-code allowlist.
            rows = [dict(row) for row in connection.execute(text(f'SELECT * FROM "{table}" ORDER BY 1')).mappings()]
            payload = json.dumps(rows, default=str, sort_keys=True).encode()
            result[table] = {"count": len(rows), "sha256": hashlib.sha256(payload).hexdigest()}
    return result


def verify_application(images):
    from app import create_app
    from database import create_isolated_session
    from models import CatalogRequest, PriceList, PriceListItem, Product, Shop, User
    from models_mail import CatalogRequestNotification
    from services.mail_service import ResendMailer

    def refuse_send(*args, **kwargs):
        raise RehearsalRefused("mail_send_forbidden")

    ResendMailer.send = refuse_send
    app = create_app(runtime_role="web", config_overrides={
        "TESTING": True, "WTF_CSRF_ENABLED": False, "RUN_SCHEMA_BOOTSTRAP_ON_STARTUP": False,
        "ENABLE_LEGACY_SCHEMA_PATCHSET": False, "ENABLE_SCHEDULER": False,
    })
    if app.config["ENABLE_SCHEDULER"]:
        raise RehearsalRefused("scheduler_must_be_disabled")
    client = app.test_client()
    response = client.get("/readyz")
    if response.status_code != 200 or response.json["checks"].get("database") != "ok" or response.json["checks"].get("redis") != "ok":
        raise RehearsalRefused("restored_app_not_ready")
    with create_isolated_session() as session:
        owners = session.query(User).order_by(User.id).all()
        for owner in owners:
            shop = session.query(Shop).filter_by(user_id=owner.id).one()
            catalog = session.query(PriceList).filter_by(user_id=owner.id).one()
            product = session.query(Product).filter_by(user_id=owner.id).one()
            request = session.query(CatalogRequest).filter_by(user_id=owner.id).one()
            note = session.query(CatalogRequestNotification).filter_by(user_id=owner.id).one()
            item = session.query(PriceListItem).filter_by(price_list_id=catalog.id).one()
            if not (product.shop_id == catalog.shop_id == request.shop_id == shop.id and item.product_id == product.id and request.pricelist_id == catalog.id and note.request_id == request.id and note.recipient == owner.email):
                raise RehearsalRefused("restored_tenant_relationship_failed")
            public = client.get(f"/catalog/{catalog.token}")
            html = public.get_data(as_text=True)
            if public.status_code != 200 or product.last_title not in html or "supplier.example.invalid" in html or "source_url" in html:
                raise RehearsalRefused("public_catalog_boundary_failed")
            for filename in (f"restore-{owner.username[-1]}.png", f"logo-{owner.username[-1]}.png"):
                media = client.get(f"/media/{filename}")
                if media.status_code != 200 or media.data != (images / filename).read_bytes():
                    raise RehearsalRefused("restored_media_failed")
            # Login session avoids changing last_login_at during this comparison.
            with client.session_transaction() as browser_session:
                browser_session["_user_id"], browser_session["_fresh"] = str(owner.id), True
            if client.get(f"/requests/{request.id}").status_code != 200:
                raise RehearsalRefused("owner_request_unavailable")
            other_request = session.query(CatalogRequest).filter(CatalogRequest.user_id != owner.id).one()
            other_product = session.query(Product).filter(Product.user_id != owner.id).one()
            if client.get(f"/requests/{other_request.id}").status_code != 404 or client.get(f"/product/{other_product.id}").status_code != 404:
                raise RehearsalRefused("cross_tenant_access_allowed")
    return {"readyz": "ok", "tenant_relationships": "ok", "cross_tenant_access": "denied", "managed_media": "ok", "mail_sent": False}


def execute_rehearsal(container, pg_port, redis_port, revision=None):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", container) or not all(1 <= port <= 65535 for port in (pg_port, redis_port)):
        raise RehearsalRefused("invalid_local_arguments")
    revision = revision or os.environ.get("GITHUB_SHA", "")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise RehearsalRefused("release_sha_required")
    image = docker_command(["inspect", "--format", "{{.Config.Image}}", container], stdout=subprocess.PIPE).stdout.decode().strip()
    if image != "postgres:18":
        raise RehearsalRefused("postgres18_container_required")
    for tool in ("pg_dump", "pg_restore"):
        version = docker_command(["exec", container, tool, "--version"], stdout=subprocess.PIPE).stdout.decode()
        if not re.search(r"PostgreSQL\) 18(?:\.|\s)", version):
            raise RehearsalRefused("postgres18_client_required")
    source, target = database_names()
    with tempfile.TemporaryDirectory(prefix="esp-synthetic-restore-") as work:
        work = Path(work)
        original_media, restored_media = work / "original" / "images", work / "restored" / "images"
        with synthetic_environment(pg_port, redis_port, source, restored_media), loopback_only({pg_port, redis_port}):
            import psycopg
            from psycopg import sql
            import redis
            from sqlalchemy import create_engine, text
            import database
            from scripts.backup_restore_plan import inspect_backup

            redis_client = redis.Redis(host="127.0.0.1", port=redis_port, socket_timeout=5)
            if not redis_client.ping() or redis_client.dbsize() != 0:
                raise RehearsalRefused("fresh_redis_required")
            with psycopg.connect(host="127.0.0.1", port=pg_port, user=CI_USER, password=CI_PASSWORD,
                                 dbname="postgres", sslmode="disable", connect_timeout=5, autocommit=True) as admin:
                if not 180000 <= int(admin.execute("SHOW server_version_num").fetchone()[0]) < 190000:
                    raise RehearsalRefused("postgres18_server_required")
                for name in (source, target):
                    # Plain CREATE fails if an existing name is encountered.
                    admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
            print("Rehearsal stage: migrate_and_seed", file=sys.stderr, flush=True)
            database.run_alembic_upgrade_for_database_url(local_url(pg_port, source), revision=HEAD)
            source_engine = create_engine(local_url(pg_port, source))
            seed_database(source_engine, original_media)
            expected = snapshot_database(source_engine)
            if any(data["count"] != 2 for data in expected.values()):
                raise RehearsalRefused("incomplete_synthetic_fixture")
            dump, media_archive = work / "database.dump", work / "images.tar.gz"
            print("Rehearsal stage: pg18_dump", file=sys.stderr, flush=True)
            with dump.open("xb") as destination:
                docker_command(["exec", container, "pg_dump", "--host=/var/run/postgresql", f"--username={CI_USER}",
                                f"--dbname={source}", "--format=custom", "--no-owner", "--no-privileges"], stdout=destination)
            with tarfile.open(media_archive, "w:gz") as archive:
                archive.add(original_media, arcname="images")
            manifest = inspect_backup(dump, media_archive, revision)
            target_engine = create_engine(local_url(pg_port, target))
            with target_engine.connect() as connection:
                objects = connection.execute(text("SELECT count(*) FROM pg_depend d JOIN pg_namespace n ON d.refclassid='pg_namespace'::regclass AND n.oid=d.refobjid WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname !~ '^pg_' ")).scalar_one()
                schemas = connection.execute(text("SELECT count(*) FROM pg_namespace WHERE nspname NOT IN ('public','pg_catalog','information_schema') AND nspname !~ '^pg_' ")).scalar_one()
                if objects or schemas:
                    raise RehearsalRefused("restore_target_not_empty")
            with dump.open("rb") as source_file:
                print("Rehearsal stage: pg18_restore_into_empty_target", file=sys.stderr, flush=True)
                docker_command(["exec", "-i", container, "pg_restore", "--host=/var/run/postgresql", f"--username={CI_USER}",
                                f"--dbname={target}", "--no-owner", "--no-privileges", "--exit-on-error", "--single-transaction"],
                               stdin=source_file, stdout=subprocess.DEVNULL)
            if snapshot_database(target_engine) != expected:
                raise RehearsalRefused("restored_row_identity_failed")
            restored_media.parent.mkdir()
            with tarfile.open(media_archive, "r:gz") as archive:
                # Already inspected, synthetic-only, immutable archive; exclusive writes.
                for member in archive:
                    destination = restored_media.parent / member.name
                    if member.isdir():
                        destination.mkdir(parents=True, exist_ok=True)
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with archive.extractfile(member) as media_source, destination.open("xb") as output:
                            output.write(media_source.read())
            database.SessionLocal.remove()
            database.engine.dispose()
            database.engine = target_engine
            database._session_factory.configure(bind=target_engine)
            database.SessionLocal.configure(bind=target_engine)
            os.environ["DATABASE_URL"] = local_url(pg_port, target)
            print("Rehearsal stage: restored_application_and_tenants", file=sys.stderr, flush=True)
            application = verify_application(restored_media)
            if snapshot_database(target_engine) != expected:
                raise RehearsalRefused("application_changed_restored_business_rows")
            database.SessionLocal.remove()
            source_engine.dispose()
            target_engine.dispose()
            return {"status": "passed", "scope": "synthetic_local_only", "source_revision": revision.lower(), "postgresql_major": 18, "migration_head": HEAD,
                    "fresh_distinct_databases": True, "counts": {table: data["count"] for table, data in expected.items()},
                    "backup_sha256": manifest["database"]["sha256"], "media_files": manifest["media"]["files"],
                    "application": application, "production_backup_performed": False, "production_restore_performed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Explicitly create and restore new synthetic local databases")
    parser.add_argument("--postgres-container")
    parser.add_argument("--revision", help="Full checked-out commit SHA; defaults to GITHUB_SHA")
    parser.add_argument("--postgres-port", type=int, default=5432)
    parser.add_argument("--redis-port", type=int, default=6379)
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({"status": "dry_run", "network_used": False, "restore_executed": False, "scope": "synthetic_local_only"}))
        return 0
    try:
        result = execute_rehearsal(args.postgres_container or "", args.postgres_port, args.redis_port, args.revision)
    except RehearsalRefused as error:
        print(json.dumps({"status": "refused", "code": str(error)}))
        return 2
    except Exception as error:
        # No raw subprocess/driver errors, SQL values, or environment values.
        print(json.dumps({"status": "failed", "code": "synthetic_rehearsal_failed", "error_type": type(error).__name__}))
        return 1
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    # Direct script execution must resolve the repository package imports.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
