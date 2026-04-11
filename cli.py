"""
CLI commands for the application.
"""
import html
import json
import os
import socket
import time
import traceback
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import click
import requests
from redis import Redis

import offmall_db
import rakuma_db
import snkrdunk_db
import surugaya_db
import yahoo_db
import yahuoku_db
from database import (
    SessionLocal,
    describe_schema_bootstrap,
    inspect_additive_schema_drift,
    redact_database_url,
    run_alembic_upgrade_for_database_url,
    run_database_smoke_check,
)
from mercari_db import scrape_single_item as scrape_mercari_single_item
from models import Product, ProductSnapshot, Variant
from services.database_migration import (
    DEFAULT_EXISTING_WEB_SQLITE_URL,
    DEFAULT_MIGRATION_BATCH_SIZE,
    run_existing_web_database_migration,
)
from services.html_page_adapter import HtmlPageAdapter
from services.mercari_item_parser import parse_mercari_item_page
from services.pricing_service import update_product_selling_price
from services.repair_store import inspect_repair_store_state
from services.repair_worker import preview_pending_repair_candidates, process_pending_repair_candidates
from services.rich_text_maintenance import run_rich_text_maintenance
from services.scrape_result_policy import (
    build_policy_reason,
    evaluate_persistence,
    normalize_item_for_persistence,
)
from services.worker_runtime import get_worker_health_snapshot
from services.image_service import IMAGE_STORAGE_PATH
from time_utils import utc_now


def _get_single_item_scrapers() -> dict:
    return {
        "mercari": scrape_mercari_single_item,
        "yahoo": yahoo_db.scrape_single_item,
        "rakuma": rakuma_db.scrape_single_item,
        "surugaya": surugaya_db.scrape_single_item,
        "offmall": offmall_db.scrape_single_item,
        "yahuoku": yahuoku_db.scrape_single_item,
        "snkrdunk": snkrdunk_db.scrape_single_item,
    }


def _emit_json(payload: dict) -> None:
    click.echo(json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str))


def _build_local_split_env_defaults() -> dict[str, str]:
    return {
        "SECRET_KEY": "local-split-secret",
        "DATABASE_URL": "postgresql+psycopg://esp:esp@localhost:5432/esp_local",
        "REDIS_URL": "redis://localhost:6379/0",
        "SCRAPE_QUEUE_BACKEND": "rq",
        "WEB_SCHEDULER_MODE": "disabled",
        "SCHEMA_BOOTSTRAP_MODE": "auto",
        "WORKER_ENABLE_SCHEDULER": "1",
        "WARM_BROWSER_POOL": "1",
        "ENABLE_SHARED_BROWSER_RUNTIME": "1",
        "WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP": "0",
        "WORKER_SELECTOR_REPAIR_LIMIT": "1",
        "SELECTOR_REPAIR_MIN_SCORE": "90",
        "SELECTOR_REPAIR_MIN_CANARIES": "2",
        "IMAGE_STORAGE_PATH": os.path.abspath(str(IMAGE_STORAGE_PATH)),
    }


@contextmanager
def _temporary_env(overrides: dict[str, str]):
    original = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            os.environ[key] = str(value)
        yield
    finally:
        for key, previous in original.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def _probe_tcp_endpoint(name: str, url: str, *, timeout_seconds: float = 1.0) -> dict[str, object]:
    parsed = urlparse(str(url or "").strip())
    host = parsed.hostname
    port = parsed.port
    blockers: list[str] = []
    error = None
    if not host or not port:
        blockers.append(f"{name}_endpoint_invalid")
    else:
        try:
            with socket.create_connection((host, port), timeout=timeout_seconds):
                pass
        except Exception as exc:
            error = str(exc)
            blockers.append(f"{name}_endpoint_unreachable")

    return {
        "name": name,
        "url": url,
        "host": host,
        "port": port,
        "timeout_seconds": timeout_seconds,
        "ready": not blockers,
        "blockers": blockers,
        "error": error,
    }


def _probe_local_split_services(database_url: str, redis_url: str) -> dict[str, object]:
    postgres_probe = _probe_tcp_endpoint("postgres", database_url)
    redis_probe = _probe_tcp_endpoint("redis", redis_url)
    blockers = list(postgres_probe.get("blockers") or []) + list(redis_probe.get("blockers") or [])
    return {
        "ready": not blockers,
        "blockers": blockers,
        "probes": [postgres_probe, redis_probe],
    }


def _response_contains_title(page_body: str, title: str) -> bool:
    if not page_body or not title:
        return False
    if title in page_body:
        return True
    return title in html.unescape(page_body)


def _parse_detail_fixture(site: str, fixture_path: str, *, target_url: str = "") -> dict:
    normalized_site = str(site or "").strip().lower()
    resolved_path = Path(fixture_path).expanduser()
    if not resolved_path.is_absolute():
        resolved_path = (Path.cwd() / resolved_path).resolve()
    else:
        resolved_path = resolved_path.resolve()

    if not resolved_path.exists():
        raise FileNotFoundError(f"Fixture not found: {resolved_path}")

    if normalized_site == "mercari":
        item_url = str(target_url or "https://jp.mercari.com/item/m-fixture-smoke").strip()
        html_body = resolved_path.read_text(encoding="utf-8")
        page = HtmlPageAdapter(html_body, url=item_url)
        item, meta = parse_mercari_item_page(page, item_url)
    elif normalized_site == "snkrdunk":
        item_url = str(target_url or "https://snkrdunk.com/products/fixture-smoke").strip()
        html_body = resolved_path.read_text(encoding="utf-8", errors="ignore")
        page = HtmlPageAdapter(html_body, url=item_url)
        item = snkrdunk_db._parse_detail_page(page, item_url)
        meta = {
            "page_type": "active_detail" if item.get("title") else "unknown_page",
            "confidence": "high" if item.get("title") and item.get("price") is not None else "low",
        }
    else:
        raise ValueError(f"Unsupported fixture site: {site}")

    normalized_item = dict(item)
    normalized_item["url"] = str(normalized_item.get("url") or item_url)
    normalized_item["image_urls"] = list(normalized_item.get("image_urls") or [])
    normalized_item["variants"] = list(normalized_item.get("variants") or [])

    return {
        "site": normalized_site,
        "path": str(resolved_path),
        "item": normalized_item,
        "meta": dict(meta or {}),
    }


def _parse_search_fixture(site: str, fixture_path: str, *, target_url: str = "") -> dict:
    normalized_site = str(site or "").strip().lower()
    resolved_path = Path(fixture_path).expanduser()
    if not resolved_path.is_absolute():
        resolved_path = (Path.cwd() / resolved_path).resolve()
    else:
        resolved_path = resolved_path.resolve()

    if not resolved_path.exists():
        raise FileNotFoundError(f"Fixture not found: {resolved_path}")

    if normalized_site != "mercari":
        raise ValueError(f"Unsupported search fixture site: {site}")

    search_url = str(target_url or "https://jp.mercari.com/search?keyword=fixture-smoke").strip()
    html_body = resolved_path.read_text(encoding="utf-8", errors="ignore")
    page = HtmlPageAdapter(html_body, url=search_url)

    item_urls = []
    seen_urls = set()
    for selector in (
        "a[data-testid='thumbnail-link']",
        "a[href*='/item/']",
        "li[data-testid='item-cell'] a",
    ):
        for link in page.css(selector):
            href = str(link.attrib.get("href") or "").strip()
            if not href or "/item/" not in href:
                continue
            if href.startswith("/"):
                href = f"https://jp.mercari.com{href}"
            if href in seen_urls:
                continue
            seen_urls.add(href)
            item_urls.append(href)

    search_heading = page.find("h1")
    canonical = page.find("link[rel='canonical']")
    metadata_title = page.find("title")
    has_grid_skeleton = bool(page.css("[data-testid='item-grid-skeleton']"))
    has_item_skeleton = bool(page.css("[data-testid='item-cell-skeleton']"))
    has_item_cells = bool(page.css("li[data-testid='item-cell']"))
    all_text = page.get_all_text()

    if item_urls:
        page_type = "search_results"
        confidence = "high"
    elif has_grid_skeleton or has_item_skeleton:
        page_type = "search_skeleton"
        confidence = "low"
    elif "検索結果" in all_text:
        page_type = "search_empty"
        confidence = "medium"
    else:
        page_type = "unknown_search"
        confidence = "low"

    return {
        "site": normalized_site,
        "path": str(resolved_path),
        "search_url": search_url,
        "item_urls": item_urls,
        "meta": {
            "page_type": page_type,
            "confidence": confidence,
            "heading": search_heading.text if search_heading else "",
            "canonical_url": str(canonical.attrib.get("href") or "").strip() if canonical else "",
            "metadata_title": metadata_title.text if metadata_title else "",
            "has_grid_skeleton": has_grid_skeleton,
            "has_item_skeleton": has_item_skeleton,
            "has_item_cells": has_item_cells,
        },
    }


def _resolve_local_path(path: str) -> Path:
    resolved_path = Path(path).expanduser()
    if not resolved_path.is_absolute():
        resolved_path = (Path.cwd() / resolved_path).resolve()
    else:
        resolved_path = resolved_path.resolve()
    return resolved_path


def _strip_yaml_scalar(value: str) -> str:
    normalized = str(value or "").strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {"'", '"'}:
        return normalized[1:-1]
    return normalized


def _parse_render_blueprint(path: str = "render.yaml") -> dict:
    resolved_path = _resolve_local_path(path)
    if not resolved_path.exists():
        raise FileNotFoundError(f"Blueprint not found: {resolved_path}")

    lines = resolved_path.read_text(encoding="utf-8").splitlines()
    section = None
    services = []
    databases = []
    current_service = None
    current_database = None
    current_env = None
    service_subsection = None
    env_source_block = None

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))

        if stripped == "services:":
            section = "services"
            current_service = None
            current_database = None
            service_subsection = None
            current_env = None
            env_source_block = None
            continue
        if stripped == "databases:":
            section = "databases"
            current_service = None
            current_database = None
            service_subsection = None
            current_env = None
            env_source_block = None
            continue

        if section == "services":
            if indent == 2 and stripped.startswith("- type:"):
                current_service = {
                    "type": _strip_yaml_scalar(stripped.split(":", 1)[1]),
                    "name": "",
                    "plan": "",
                    "autoDeployTrigger": "",
                    "healthCheckPath": "",
                    "dockerCommand": "",
                    "env_vars": {},
                    "disk": {},
                }
                services.append(current_service)
                service_subsection = None
                current_env = None
                env_source_block = None
                continue

            if current_service is None:
                continue

            if indent == 4:
                current_env = None
                env_source_block = None
                if stripped == "envVars:":
                    service_subsection = "envVars"
                    continue
                if stripped == "disk:":
                    service_subsection = "disk"
                    continue

                service_subsection = None
                if ":" in stripped:
                    key, value = stripped.split(":", 1)
                    current_service[key.strip()] = _strip_yaml_scalar(value)
                continue

            if service_subsection == "disk" and indent == 6 and ":" in stripped:
                key, value = stripped.split(":", 1)
                current_service["disk"][key.strip()] = _strip_yaml_scalar(value)
                continue

            if service_subsection == "envVars":
                if indent == 6 and stripped.startswith("- key:"):
                    env_key = _strip_yaml_scalar(stripped.split(":", 1)[1])
                    current_env = {"key": env_key}
                    current_service["env_vars"][env_key] = current_env
                    env_source_block = None
                    continue

                if current_env is None:
                    continue

                if indent == 8:
                    if stripped.startswith("fromService:"):
                        env_source_block = "fromService"
                        current_env["source_type"] = "fromService"
                        continue
                    if stripped.startswith("fromDatabase:"):
                        env_source_block = "fromDatabase"
                        current_env["source_type"] = "fromDatabase"
                        continue
                    if ":" in stripped:
                        key, value = stripped.split(":", 1)
                        current_env[key.strip()] = _strip_yaml_scalar(value)
                    continue

                if indent == 10 and env_source_block and ":" in stripped:
                    key, value = stripped.split(":", 1)
                    current_env.setdefault(env_source_block, {})[key.strip()] = _strip_yaml_scalar(value)
                    continue

        if section == "databases":
            if indent == 2 and stripped.startswith("- name:"):
                current_database = {"name": _strip_yaml_scalar(stripped.split(":", 1)[1])}
                databases.append(current_database)
                continue

            if current_database is not None and indent == 4 and ":" in stripped:
                key, value = stripped.split(":", 1)
                current_database[key.strip()] = _strip_yaml_scalar(value)

    return {
        "path": str(resolved_path),
        "services": services,
        "databases": databases,
    }


def run_render_blueprint_audit(path: str = "render.yaml") -> dict:
    blueprint = _parse_render_blueprint(path)
    services = blueprint["services"]
    databases = blueprint["databases"]
    services_by_name = {service.get("name"): service for service in services if service.get("name")}
    databases_by_name = {database.get("name"): database for database in databases if database.get("name")}

    blockers = []
    warnings = []

    expected_services = {
        "esp-web": "web",
        "esp-worker": "worker",
        "esp-keyvalue": "keyvalue",
    }
    for service_name, expected_type in expected_services.items():
        service = services_by_name.get(service_name)
        if service is None:
            blockers.append(f"missing_service:{service_name}")
            continue
        if service.get("type") != expected_type:
            blockers.append(f"service_type_mismatch:{service_name}")

    if "esp-postgres" not in databases_by_name:
        blockers.append("missing_database:esp-postgres")

    web_service = services_by_name.get("esp-web", {})
    worker_service = services_by_name.get("esp-worker", {})
    keyvalue_service = services_by_name.get("esp-keyvalue", {})
    database_service = databases_by_name.get("esp-postgres", {})

    web_env = web_service.get("env_vars", {})
    worker_env = worker_service.get("env_vars", {})

    def _require_env(service_name: str, env_map: dict, key: str) -> None:
        if key not in env_map:
            blockers.append(f"missing_env:{service_name}:{key}")

    for key in (
        "SECRET_KEY",
        "DATABASE_URL",
        "REDIS_URL",
        "SCRAPE_QUEUE_BACKEND",
        "WEB_SCHEDULER_MODE",
        "SCHEMA_BOOTSTRAP_MODE",
        "IMAGE_STORAGE_PATH",
    ):
        _require_env("esp-web", web_env, key)

    for key in (
        "SECRET_KEY",
        "DATABASE_URL",
        "REDIS_URL",
        "SCRAPE_QUEUE_BACKEND",
        "WORKER_ENABLE_SCHEDULER",
        "WARM_BROWSER_POOL",
        "ENABLE_SHARED_BROWSER_RUNTIME",
        "BROWSER_POOL_WARM_SITES",
        "MERCARI_USE_BROWSER_POOL_DETAIL",
        "MERCARI_PATROL_USE_BROWSER_POOL",
        "SNKRDUNK_USE_BROWSER_POOL_DYNAMIC",
        "WORKER_RECONCILE_STALLED_JOBS_ON_STARTUP",
        "WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP",
        "WORKER_SELECTOR_REPAIR_LIMIT",
        "SELECTOR_REPAIR_MIN_SCORE",
        "SELECTOR_REPAIR_MIN_CANARIES",
        "SELECTOR_REPAIR_CANARY_URLS_MERCARI_DETAIL",
        "SELECTOR_REPAIR_CANARY_URLS_SNKRDUNK_DETAIL",
        "WORKER_BACKLOG_WARN_COUNT",
        "WORKER_BACKLOG_WARN_AGE_SECONDS",
    ):
        _require_env("esp-worker", worker_env, key)

    if web_service.get("autoDeployTrigger") != "off":
        blockers.append("web_auto_deploy_must_be_off")
    if worker_service.get("autoDeployTrigger") != "off":
        blockers.append("worker_auto_deploy_must_be_off")
    if web_service.get("healthCheckPath") != "/healthz":
        blockers.append("web_healthcheck_must_use_healthz")
    if worker_service.get("dockerCommand") != "python worker.py":
        blockers.append("worker_command_must_use_python_worker_py")
    if web_service.get("disk", {}).get("mountPath") != "/var/data":
        blockers.append("web_disk_mount_path_must_be_var_data")
    if web_env.get("SCRAPE_QUEUE_BACKEND", {}).get("value") != "rq":
        blockers.append("web_queue_backend_must_be_rq")
    if worker_env.get("SCRAPE_QUEUE_BACKEND", {}).get("value") != "rq":
        blockers.append("worker_queue_backend_must_be_rq")
    if web_env.get("WEB_SCHEDULER_MODE", {}).get("value") != "disabled":
        blockers.append("web_scheduler_mode_must_be_disabled")
    if web_env.get("SCHEMA_BOOTSTRAP_MODE", {}).get("value") != "auto":
        blockers.append("web_schema_bootstrap_mode_must_be_auto")
    if web_env.get("IMAGE_STORAGE_PATH", {}).get("value") != "/var/data/images":
        blockers.append("web_image_storage_path_must_be_var_data_images")
    if worker_env.get("WORKER_ENABLE_SCHEDULER", {}).get("value") != "1":
        blockers.append("worker_scheduler_owner_must_be_enabled")
    if worker_env.get("WARM_BROWSER_POOL", {}).get("value") != "1":
        blockers.append("worker_browser_pool_warm_must_be_enabled")
    if worker_env.get("ENABLE_SHARED_BROWSER_RUNTIME", {}).get("value") != "1":
        blockers.append("worker_shared_browser_runtime_must_be_enabled")
    if worker_env.get("WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP", {}).get("value") != "0":
        blockers.append("worker_selector_repairs_startup_must_be_disabled_initially")
    if worker_env.get("WORKER_SELECTOR_REPAIR_LIMIT", {}).get("value") != "1":
        blockers.append("worker_selector_repair_limit_must_start_at_1")
    if worker_env.get("SELECTOR_REPAIR_MIN_SCORE", {}).get("value") != "90":
        blockers.append("selector_repair_min_score_must_start_at_90")
    if worker_env.get("SELECTOR_REPAIR_MIN_CANARIES", {}).get("value") != "2":
        blockers.append("selector_repair_min_canaries_must_start_at_2")

    for service_name, env_map in (("esp-web", web_env), ("esp-worker", worker_env)):
        secret_env = env_map.get("SECRET_KEY", {})
        if str(secret_env.get("sync", "")).lower() != "false":
            blockers.append(f"secret_key_must_be_manual:{service_name}")
        if env_map.get("DATABASE_URL", {}).get("source_type") != "fromDatabase":
            blockers.append(f"database_url_must_be_managed:{service_name}")
        if env_map.get("REDIS_URL", {}).get("source_type") != "fromService":
            blockers.append(f"redis_url_must_be_managed:{service_name}")

    for key in (
        "SELECTOR_REPAIR_CANARY_URLS_MERCARI_DETAIL",
        "SELECTOR_REPAIR_CANARY_URLS_SNKRDUNK_DETAIL",
    ):
        if str((worker_env.get(key) or {}).get("sync", "")).lower() != "false":
            blockers.append(f"worker_selector_repair_canaries_must_be_manual:{key.lower()}")

    if web_env.get("SELECTOR_ALERT_WEBHOOK_URL"):
        if str(web_env["SELECTOR_ALERT_WEBHOOK_URL"].get("sync", "")).lower() != "false":
            warnings.append("selector_alert_webhook_should_remain_manual")
    else:
        warnings.append("selector_alert_webhook_missing_from_blueprint")

    if worker_env.get("OPERATIONAL_ALERT_WEBHOOK_URL"):
        if str(worker_env["OPERATIONAL_ALERT_WEBHOOK_URL"].get("sync", "")).lower() != "false":
            warnings.append("operational_alert_webhook_should_remain_manual")
    else:
        warnings.append("operational_alert_webhook_missing_from_blueprint")

    if keyvalue_service.get("plan") != "starter":
        warnings.append("keyvalue_plan_differs_from_budget_baseline")
    if database_service.get("plan") != "basic-1gb":
        warnings.append("postgres_plan_differs_from_budget_baseline")

    manual_secret_envs = [
        {"service": "esp-web", "key": "SECRET_KEY", "required": True},
        {"service": "esp-web", "key": "SELECTOR_ALERT_WEBHOOK_URL", "required": False},
        {"service": "esp-worker", "key": "SECRET_KEY", "required": True},
        {"service": "esp-worker", "key": "OPERATIONAL_ALERT_WEBHOOK_URL", "required": False},
    ]

    return {
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "blueprint_path": blueprint["path"],
        "service_names": sorted(services_by_name.keys()),
        "database_names": sorted(databases_by_name.keys()),
        "expected_render_services": ["esp-web", "esp-worker", "esp-keyvalue", "esp-postgres"],
        "manual_secret_envs": manual_secret_envs,
    }


RENDER_BUDGET_GUARDRAIL_USD = 80
RENDER_BUDGET_ASSUMPTION_DATE = "2026-03-23"
RENDER_GUARDRAIL_EXPECTED_PLANS = {
    "esp-web": ("web", "starter", 7),
    "esp-worker": ("worker", "standard", 25),
    "esp-keyvalue": ("keyvalue", "starter", 10),
    "esp-postgres": ("database", "basic-1gb", 19),
}


def run_render_budget_guardrail_audit(path: str = "render.yaml") -> dict:
    blueprint = _parse_render_blueprint(path)
    services = blueprint["services"]
    databases = blueprint["databases"]
    services_by_name = {service.get("name"): service for service in services if service.get("name")}
    databases_by_name = {database.get("name"): database for database in databases if database.get("name")}

    blockers = []
    warnings = []
    estimated_monthly_core_usd = 0
    plan_snapshot: dict[str, dict[str, object]] = {}

    for name, (kind, expected_plan, expected_cost) in RENDER_GUARDRAIL_EXPECTED_PLANS.items():
        entity = databases_by_name.get(name) if kind == "database" else services_by_name.get(name)
        actual_plan = str((entity or {}).get("plan") or "").strip().lower()

        if entity is None:
            blockers.append(f"missing_budget_subject:{name}")
        elif actual_plan != expected_plan:
            blockers.append(f"plan_guardrail_mismatch:{name}:{actual_plan or 'missing'}:{expected_plan}")
        else:
            estimated_monthly_core_usd += expected_cost

        plan_snapshot[name] = {
            "kind": kind,
            "expected_plan": expected_plan,
            "actual_plan": actual_plan or None,
            "expected_monthly_usd": expected_cost,
        }

    web_disk = dict((services_by_name.get("esp-web") or {}).get("disk") or {})
    disk_size_gb = None
    if web_disk:
        try:
            disk_size_gb = int(web_disk.get("sizeGB"))
        except (TypeError, ValueError):
            disk_size_gb = None
        if disk_size_gb and disk_size_gb > 10:
            blockers.append("web_disk_exceeds_small_disk_guardrail")
        elif disk_size_gb:
            warnings.append("web_persistent_disk_cost_not_included")

    return {
        "ready": not blockers,
        "blueprint_path": blueprint["path"],
        "budget_guardrail_usd": RENDER_BUDGET_GUARDRAIL_USD,
        "pricing_assumption_date": RENDER_BUDGET_ASSUMPTION_DATE,
        "estimated_monthly_core_usd": estimated_monthly_core_usd,
        "disk_configured": bool(web_disk),
        "disk_size_gb": disk_size_gb,
        "plan_snapshot": plan_snapshot,
        "blockers": blockers,
        "warnings": warnings,
    }


def _summarize_render_service_envs(service: dict) -> dict:
    env_map = dict(service.get("env_vars") or {})
    manual_envs = []
    managed_envs = []
    fixed_envs = []

    for key in sorted(env_map):
        metadata = dict(env_map.get(key) or {})
        if metadata.get("source_type"):
            source_type = str(metadata.get("source_type") or "")
            source_payload = dict(metadata.get(source_type) or {})
            managed_envs.append(
                {
                    "key": key,
                    "source_type": source_type,
                    "resource_name": source_payload.get("name"),
                    "property": source_payload.get("property"),
                }
            )
            continue

        if "value" in metadata:
            fixed_envs.append({"key": key, "value": metadata.get("value")})
            continue

        manual_envs.append(
            {
                "key": key,
                "sync": str(metadata.get("sync", "")).lower() == "false",
            }
        )

    return {
        "service": service.get("name"),
        "type": service.get("type"),
        "manual_envs": manual_envs,
        "managed_envs": managed_envs,
        "fixed_envs": fixed_envs,
    }


def run_render_dashboard_inputs(path: str = "render.yaml") -> dict:
    audit = run_render_blueprint_audit(path)
    blueprint = _parse_render_blueprint(path)
    service_inputs = [
        _summarize_render_service_envs(service)
        for service in blueprint.get("services") or []
        if service.get("type") in {"web", "worker"}
    ]

    return {
        "ready": audit.get("ready", False),
        "blockers": list(audit.get("blockers") or []),
        "warnings": list(audit.get("warnings") or []),
        "blueprint_path": audit.get("blueprint_path"),
        "service_inputs": service_inputs,
        "manual_secret_envs": list(audit.get("manual_secret_envs") or []),
        "expected_render_services": list(audit.get("expected_render_services") or []),
    }


def run_render_local_split_checklist(
    app,
    *,
    blueprint_path: str = "render.yaml",
    compose_path: str = "docker-compose.local.yml",
) -> dict:
    from app import create_app

    normalized_blueprint_path = str(blueprint_path or "render.yaml").strip() or "render.yaml"
    normalized_compose_path = str(compose_path or "docker-compose.local.yml").strip() or "docker-compose.local.yml"

    resolved_compose_path = Path(normalized_compose_path).expanduser()
    if not resolved_compose_path.is_absolute():
        resolved_compose_path = (Path.cwd() / resolved_compose_path).resolve()
    else:
        resolved_compose_path = resolved_compose_path.resolve()

    split_web_app = create_app(
        runtime_role="web",
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "rq",
            "WEB_SCHEDULER_MODE": "disabled",
        },
    )
    split_worker_app = create_app(
        runtime_role="worker",
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "rq",
            "ENABLE_SCHEDULER": True,
            "WORKER_ENABLE_SCHEDULER": "1",
            "WARM_BROWSER_POOL": "1",
            "ENABLE_SHARED_BROWSER_RUNTIME": "1",
        },
    )

    current_schema = describe_schema_bootstrap(app.config.get("SCHEMA_BOOTSTRAP_MODE", "auto"))
    split_predeploy = build_predeploy_snapshot(split_web_app, target="split-render")
    worker_health = get_worker_health_snapshot(split_worker_app)
    blueprint_audit = run_render_blueprint_audit(normalized_blueprint_path)
    budget_guardrail = run_render_budget_guardrail_audit(normalized_blueprint_path)
    recommended_env = _build_local_split_env_defaults()
    service_probes = _probe_local_split_services(
        recommended_env["DATABASE_URL"],
        recommended_env["REDIS_URL"],
    )

    local_env_contract = [
        {
            "scope": "shared",
            "key": "SECRET_KEY",
            "desired_value": "<non-default-secret>",
            "current_value": str(app.config.get("SECRET_KEY", "") or ""),
            "matches": str(app.config.get("SECRET_KEY", "") or "") != "dev-secret-key-change-this",
        },
        {
            "scope": "shared",
            "key": "DATABASE_URL",
            "desired_value": recommended_env["DATABASE_URL"],
            "current_value": current_schema.get("database_url"),
            "matches": current_schema.get("database_backend") == "postgresql"
            and "localhost" in str(current_schema.get("database_url") or ""),
        },
        {
            "scope": "shared",
            "key": "REDIS_URL",
            "desired_value": recommended_env["REDIS_URL"],
            "current_value": str(app.config.get("REDIS_URL", "") or ""),
            "matches": str(app.config.get("REDIS_URL", "") or "").startswith("redis://localhost")
            or str(app.config.get("REDIS_URL", "") or "").startswith("redis://127.0.0.1"),
        },
        {
            "scope": "web",
            "key": "SCRAPE_QUEUE_BACKEND",
            "desired_value": recommended_env["SCRAPE_QUEUE_BACKEND"],
            "current_value": str(app.config.get("SCRAPE_QUEUE_BACKEND", "") or ""),
            "matches": str(app.config.get("SCRAPE_QUEUE_BACKEND", "") or "").strip().lower() == "rq",
        },
        {
            "scope": "web",
            "key": "WEB_SCHEDULER_MODE",
            "desired_value": recommended_env["WEB_SCHEDULER_MODE"],
            "current_value": str(app.config.get("WEB_SCHEDULER_MODE", "") or ""),
            "matches": str(app.config.get("WEB_SCHEDULER_MODE", "") or "").strip().lower() == "disabled",
        },
        {
            "scope": "shared",
            "key": "SCHEMA_BOOTSTRAP_MODE",
            "desired_value": recommended_env["SCHEMA_BOOTSTRAP_MODE"],
            "current_value": str(app.config.get("SCHEMA_BOOTSTRAP_MODE", "") or ""),
            "matches": str(current_schema.get("effective_mode") or "") == "alembic",
        },
        {
            "scope": "worker",
            "key": "WORKER_ENABLE_SCHEDULER",
            "desired_value": recommended_env["WORKER_ENABLE_SCHEDULER"],
            "current_value": str(app.config.get("WORKER_ENABLE_SCHEDULER", "") or ""),
            "matches": str(app.config.get("WORKER_ENABLE_SCHEDULER", "") or "").strip().lower()
            in {"1", "true", "yes", "on"},
        },
        {
            "scope": "worker",
            "key": "WARM_BROWSER_POOL",
            "desired_value": recommended_env["WARM_BROWSER_POOL"],
            "current_value": str(app.config.get("WARM_BROWSER_POOL", "") or ""),
            "matches": str(app.config.get("WARM_BROWSER_POOL", "") or "").strip().lower()
            in {"1", "true", "yes", "on"},
        },
        {
            "scope": "worker",
            "key": "WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP",
            "desired_value": recommended_env["WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP"],
            "current_value": str(app.config.get("WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP", "") or ""),
            "matches": str(app.config.get("WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP", "") or "").strip().lower()
            in {"0", "false", "no", "off"},
        },
        {
            "scope": "worker",
            "key": "WORKER_SELECTOR_REPAIR_LIMIT",
            "desired_value": recommended_env["WORKER_SELECTOR_REPAIR_LIMIT"],
            "current_value": str(app.config.get("WORKER_SELECTOR_REPAIR_LIMIT", "") or ""),
            "matches": str(app.config.get("WORKER_SELECTOR_REPAIR_LIMIT", "") or "").strip() == "1",
        },
        {
            "scope": "worker",
            "key": "SELECTOR_REPAIR_MIN_SCORE",
            "desired_value": recommended_env["SELECTOR_REPAIR_MIN_SCORE"],
            "current_value": str(app.config.get("SELECTOR_REPAIR_MIN_SCORE", "") or ""),
            "matches": str(app.config.get("SELECTOR_REPAIR_MIN_SCORE", "") or "").strip() == "90",
        },
        {
            "scope": "worker",
            "key": "SELECTOR_REPAIR_MIN_CANARIES",
            "desired_value": recommended_env["SELECTOR_REPAIR_MIN_CANARIES"],
            "current_value": str(app.config.get("SELECTOR_REPAIR_MIN_CANARIES", "") or ""),
            "matches": str(app.config.get("SELECTOR_REPAIR_MIN_CANARIES", "") or "").strip() == "2",
        },
        {
            "scope": "shared",
            "key": "IMAGE_STORAGE_PATH",
            "desired_value": recommended_env["IMAGE_STORAGE_PATH"],
            "current_value": str(IMAGE_STORAGE_PATH),
            "matches": os.path.isabs(str(IMAGE_STORAGE_PATH)),
        },
    ]

    blockers = []
    warnings = []
    if not resolved_compose_path.exists():
        blockers.append("local_compose_missing")
    if blueprint_audit.get("blockers"):
        blockers.append("render-blueprint-audit")
    if budget_guardrail.get("blockers"):
        blockers.append("render-budget-guardrail-audit")
    if service_probes.get("blockers"):
        blockers.append("local-split-service-probes")
    if split_predeploy.get("blockers"):
        blockers.append("split-render-predeploy")
    if worker_health.get("queue_backend") == "rq" and worker_health.get("redis_ok") is False:
        blockers.append("split-render-worker-health")

    warnings.extend(list(blueprint_audit.get("warnings") or []))
    warnings.extend(list(budget_guardrail.get("warnings") or []))
    warnings.extend(list(split_predeploy.get("warnings") or []))
    warnings.extend(list(worker_health.get("backlog_issues") or []))

    rehearsal_commands = [
        f"docker compose -f {normalized_compose_path} up -d",
        "flask db-smoke --require-backend postgresql --apply-migrations",
        "flask worker-health",
        "flask local-verify --profile full --require-backend postgresql --apply-migrations",
        "flask render-cutover-readiness --require-backend postgresql --apply-migrations --strict",
    ]
    powershell_env_commands = [f"$env:{key}='{value}'" for key, value in recommended_env.items()]

    return {
        "ready": not blockers,
        "blueprint_path": normalized_blueprint_path,
        "compose_path": str(resolved_compose_path),
        "compose_file_present": resolved_compose_path.exists(),
        "runbook_path": "docs/RENDER_CUTOVER_RUNBOOK.md",
        "blockers": blockers,
        "warnings": warnings,
        "local_env_contract": local_env_contract,
        "powershell_env_commands": powershell_env_commands,
        "rehearsal_commands": rehearsal_commands,
        "service_probes": service_probes,
        "blueprint_audit": {
            "ready": blueprint_audit.get("ready", False),
            "blockers": list(blueprint_audit.get("blockers") or []),
            "warnings": list(blueprint_audit.get("warnings") or []),
        },
        "budget_guardrail": {
            "ready": budget_guardrail.get("ready", False),
            "blockers": list(budget_guardrail.get("blockers") or []),
            "warnings": list(budget_guardrail.get("warnings") or []),
            "estimated_monthly_core_usd": budget_guardrail.get("estimated_monthly_core_usd"),
        },
        "split_render_predeploy": {
            "ready": split_predeploy.get("ready", False),
            "blockers": list(split_predeploy.get("blockers") or []),
            "warnings": list(split_predeploy.get("warnings") or []),
            "queue_backend": split_predeploy.get("queue_backend"),
            "schema": split_predeploy.get("schema"),
        },
        "split_worker_health": {
            "ready": worker_health.get("redis_ok") is not False,
            "queue_backend": worker_health.get("queue_backend"),
            "redis_ok": worker_health.get("redis_ok"),
            "redis_error": worker_health.get("redis_error"),
            "backlog_issues": list(worker_health.get("backlog_issues") or []),
        },
    }


def run_render_local_split_readiness(
    app,
    *,
    blueprint_path: str = "render.yaml",
    compose_path: str = "docker-compose.local.yml",
    require_backend: str = "postgresql",
    apply_migrations: bool = True,
    strict: bool = True,
    strict_parser: bool = False,
) -> dict:
    from app import create_cli_app

    env_overrides = _build_local_split_env_defaults()
    with _temporary_env(env_overrides):
        rehearsal_app = create_cli_app()
        checklist = run_render_local_split_checklist(
            rehearsal_app,
            blueprint_path=blueprint_path,
            compose_path=compose_path,
        )
        readiness = run_render_cutover_readiness(
            rehearsal_app,
            require_backend=require_backend,
            apply_migrations=apply_migrations,
            strict=strict,
            strict_parser=strict_parser,
        )

    steps = [
        {
            "name": "render-local-split-checklist",
            "ready": bool(checklist.get("ready", not checklist.get("blockers"))),
            "blockers": list(checklist.get("blockers") or []),
            "warnings": list(checklist.get("warnings") or []),
        },
        {
            "name": "render-cutover-readiness",
            "ready": bool(readiness.get("ready", not readiness.get("blockers"))),
            "blockers": list(readiness.get("blockers") or []),
            "warnings": [],
        },
    ]
    blockers = [step["name"] for step in steps if step["blockers"]]

    return {
        "ready": not blockers,
        "blueprint_path": blueprint_path,
        "compose_path": compose_path,
        "require_backend": require_backend,
        "apply_migrations": apply_migrations,
        "strict": strict,
        "strict_parser": strict_parser,
        "runbook_path": "docs/RENDER_CUTOVER_RUNBOOK.md",
        "powershell_env_commands": list(checklist.get("powershell_env_commands") or []),
        "rehearsal_commands": list(checklist.get("rehearsal_commands") or []),
        "steps": steps,
        "blockers": blockers,
    }


def run_render_cutover_brief(
    app,
    *,
    blueprint_path: str = "render.yaml",
    compose_path: str = "docker-compose.local.yml",
    base_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> dict:
    budget_guardrail = run_render_budget_guardrail_audit(blueprint_path)
    dashboard_inputs = run_render_dashboard_inputs(blueprint_path)
    worker_checklist = run_render_worker_postdeploy_checklist(blueprint_path)
    local_split_readiness = run_render_local_split_readiness(
        app,
        blueprint_path=blueprint_path,
        compose_path=compose_path,
    )
    cutover_checklist = run_render_cutover_checklist(
        blueprint_path=blueprint_path,
        base_url=base_url,
        username=username,
        password=password,
    )

    sections = [
        ("render-budget-guardrail-audit", budget_guardrail),
        ("render-dashboard-inputs", dashboard_inputs),
        ("render-worker-postdeploy-checklist", worker_checklist),
        ("render-local-split-readiness", local_split_readiness),
        ("render-cutover-checklist", cutover_checklist),
    ]
    blockers = [name for name, snapshot in sections if list(snapshot.get("blockers") or [])]
    warnings = []
    for name, snapshot in sections:
        for warning in list(snapshot.get("warnings") or []):
            warnings.append(f"{name}:{warning}")

    return {
        "ready": not blockers,
        "blueprint_path": blueprint_path,
        "compose_path": compose_path,
        "base_url": str(base_url or "").strip() or None,
        "authenticated_smoke_configured": bool(str(base_url or "").strip() and str(username or "").strip() and str(password or "")),
        "blockers": blockers,
        "warnings": warnings,
        "runbook_path": "docs/RENDER_CUTOVER_RUNBOOK.md",
        "budget_guardrail": budget_guardrail,
        "dashboard_inputs": dashboard_inputs,
        "worker_postdeploy_checklist": worker_checklist,
        "local_split_readiness": local_split_readiness,
        "cutover_checklist": cutover_checklist,
    }


def _normalize_base_url(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    if not normalized:
        raise ValueError("base_url is required")
    return normalized


def _request_with_retries(
    request_fn,
    url: str,
    *,
    timeout_seconds: float,
    allow_redirects: bool = False,
    retries: int = 2,
    retry_delay_seconds: float = 1.0,
    retryable_status_codes: tuple[int, ...] = (502, 503, 504),
) -> dict[str, object]:
    safe_retries = max(0, int(retries))
    safe_retry_delay_seconds = max(0.0, float(retry_delay_seconds))
    attempts: list[dict[str, object]] = []
    response = None
    error = None

    for attempt_number in range(1, safe_retries + 2):
        try:
            response = request_fn(url, timeout=timeout_seconds, allow_redirects=allow_redirects)
            attempts.append({"attempt": attempt_number, "status_code": response.status_code})
            if response.status_code not in retryable_status_codes:
                error = None
                break
            error = f"retryable_status:{response.status_code}"
        except Exception as exc:
            response = None
            error = str(exc)
            attempts.append({"attempt": attempt_number, "error": error})

        if attempt_number <= safe_retries and safe_retry_delay_seconds > 0:
            time.sleep(safe_retry_delay_seconds)

    return {
        "response": response,
        "error": error,
        "attempts": attempts,
        "attempt_count": len(attempts),
        "retries": safe_retries,
        "retry_delay_seconds": safe_retry_delay_seconds,
        "retryable_status_codes": list(retryable_status_codes),
    }


def _check_render_route(
    request_fn,
    route_url: str,
    route_path: str,
    *,
    timeout_seconds: float,
    retries: int = 2,
    retry_delay_seconds: float = 1.0,
) -> dict[str, object]:
    request_snapshot = _request_with_retries(
        request_fn,
        route_url,
        timeout_seconds=timeout_seconds,
        allow_redirects=False,
        retries=retries,
        retry_delay_seconds=retry_delay_seconds,
    )
    response = request_snapshot.get("response")
    if response is None:
        error = str(request_snapshot.get("error") or "request_failed")
        raise RuntimeError(error)
    return {
        "path": route_path,
        "status_code": response.status_code,
        "location": response.headers.get("Location"),
        "attempt_count": request_snapshot.get("attempt_count", 1),
        "attempts": list(request_snapshot.get("attempts") or []),
    }


def _is_authenticated_redirect(status_code: int | None, location: str | None) -> bool:
    normalized_location = str(location or "").strip()
    return status_code == 302 and normalized_location not in {"", "/login", "/register"}


def run_render_postdeploy_smoke(
    base_url: str,
    *,
    timeout_seconds: float = 15.0,
    retries: int = 2,
    retry_delay_seconds: float = 1.0,
    expect_queue_backend: str = "rq",
    expect_runtime_role: str = "web",
    expect_scheduler_mode: str = "disabled",
    username: str | None = None,
    password: str | None = None,
    ensure_user: bool = False,
) -> dict:
    normalized_base_url = _normalize_base_url(base_url)
    health_url = f"{normalized_base_url}/healthz"
    blockers: list[str] = []
    warnings: list[str] = []
    route_checks: list[dict[str, object]] = []
    authenticated_route_checks: list[dict[str, object]] = []
    health_payload: dict | None = None
    health_error = None
    health_status_code = None
    login_status_code = None
    login_location = None
    login_error = None
    login_attempted = False
    login_success = False
    registration_attempted = False
    registration_status_code = None
    registration_location = None
    registration_error = None
    registration_success = False
    health_attempt_count = 0
    health_attempts: list[dict[str, object]] = []

    expected_scheduler_enabled = None
    normalized_scheduler_mode = str(expect_scheduler_mode or "disabled").strip().lower()
    if normalized_scheduler_mode == "enabled":
        expected_scheduler_enabled = True
    elif normalized_scheduler_mode == "disabled":
        expected_scheduler_enabled = False
    elif normalized_scheduler_mode != "any":
        raise ValueError(f"Unsupported expect_scheduler_mode: {expect_scheduler_mode}")

    normalized_username = str(username or "").strip()
    normalized_password = str(password or "")
    auth_requested = bool(normalized_username or normalized_password)
    if bool(normalized_username) != bool(normalized_password):
        blockers.append("auth_credentials_incomplete")

    health_snapshot = _request_with_retries(
        requests.get,
        health_url,
        timeout_seconds=timeout_seconds,
        allow_redirects=True,
        retries=retries,
        retry_delay_seconds=retry_delay_seconds,
    )
    health_attempt_count = int(health_snapshot.get("attempt_count") or 0)
    health_attempts = list(health_snapshot.get("attempts") or [])
    response = health_snapshot.get("response")
    if response is None:
        health_error = str(health_snapshot.get("error") or "request_failed")
        blockers.append("healthz_request_failed")
    else:
        health_status_code = response.status_code
        if health_attempt_count > 1:
            warnings.append(f"healthz_required_retry:{health_attempt_count}")
        if response.status_code != 200:
            blockers.append("healthz_status_not_200")
        else:
            try:
                health_payload = dict(response.json() or {})
            except Exception as exc:
                health_error = str(exc)
                blockers.append("healthz_invalid_json")

    if health_payload:
        if health_payload.get("status") != "ok":
            blockers.append("healthz_status_not_ok")
        if expect_runtime_role and health_payload.get("runtime_role") != expect_runtime_role:
            blockers.append("runtime_role_mismatch")
        if expect_queue_backend and health_payload.get("queue_backend") != expect_queue_backend:
            blockers.append("queue_backend_mismatch")
        if (
            expected_scheduler_enabled is not None
            and bool(health_payload.get("scheduler_enabled")) != expected_scheduler_enabled
        ):
            blockers.append("scheduler_mode_mismatch")

    for route_path in ("/login", "/scrape", "/api/scrape/jobs"):
        route_url = f"{normalized_base_url}{route_path}"
        try:
            route_snapshot = _check_render_route(
                requests.get,
                route_url,
                route_path,
                timeout_seconds=timeout_seconds,
                retries=retries,
                retry_delay_seconds=retry_delay_seconds,
            )
            route_checks.append(route_snapshot)
            if int(route_snapshot.get("attempt_count") or 1) > 1:
                warnings.append(f"route_required_retry:{route_path}:{route_snapshot['attempt_count']}")
            if route_snapshot["status_code"] == 404:
                blockers.append(f"route_missing:{route_path}")
            elif route_snapshot["status_code"] >= 500:
                blockers.append(f"route_server_error:{route_path}")
            elif route_snapshot["status_code"] not in {200, 302, 401, 403}:
                warnings.append(f"route_unexpected_status:{route_path}:{route_snapshot['status_code']}")
        except Exception as exc:
            route_checks.append({"path": route_path, "error": str(exc)})
            blockers.append(f"route_request_failed:{route_path}")

    if auth_requested and "auth_credentials_incomplete" not in blockers:
        login_attempted = True
        session = requests.Session()
        login_url = f"{normalized_base_url}/login"
        try:
            login_request = _request_with_retries(
                lambda url, **kwargs: session.post(
                    url,
                    data={"username": normalized_username, "password": normalized_password},
                    **kwargs,
                ),
                login_url,
                timeout_seconds=timeout_seconds,
                allow_redirects=False,
                retries=retries,
                retry_delay_seconds=retry_delay_seconds,
            )
            login_response = login_request.get("response")
            if login_response is None:
                raise RuntimeError(str(login_request.get("error") or "login_request_failed"))
            login_status_code = login_response.status_code
            login_location = login_response.headers.get("Location")
            login_success = _is_authenticated_redirect(login_response.status_code, login_location)
            if int(login_request.get("attempt_count") or 1) > 1:
                warnings.append(f"login_required_retry:{login_request['attempt_count']}")
            if login_response.status_code >= 500:
                blockers.append("login_route_server_error")
            elif not login_success:
                warnings.append("login_did_not_redirect_to_authenticated_page")
        except Exception as exc:
            login_error = str(exc)
            blockers.append("login_request_failed")
            session = None

        if session is not None and ensure_user and not login_success:
            register_url = f"{normalized_base_url}/register"
            registration_attempted = True
            try:
                registration_request = _request_with_retries(
                    lambda url, **kwargs: session.post(
                        url,
                        data={"username": normalized_username, "password": normalized_password},
                        **kwargs,
                    ),
                    register_url,
                    timeout_seconds=timeout_seconds,
                    allow_redirects=False,
                    retries=retries,
                    retry_delay_seconds=retry_delay_seconds,
                )
                register_response = registration_request.get("response")
                if register_response is None:
                    raise RuntimeError(str(registration_request.get("error") or "register_request_failed"))
                registration_status_code = register_response.status_code
                registration_location = register_response.headers.get("Location")
                registration_success = _is_authenticated_redirect(
                    registration_status_code,
                    registration_location,
                )
                if int(registration_request.get("attempt_count") or 1) > 1:
                    warnings.append(f"register_required_retry:{registration_request['attempt_count']}")
                if register_response.status_code >= 500:
                    blockers.append("register_route_server_error")
                elif not registration_success:
                    warnings.append("register_did_not_redirect_to_authenticated_page")
            except Exception as exc:
                registration_error = str(exc)
                blockers.append("register_request_failed")

        auth_session_established = login_success or registration_success
        if auth_requested and not auth_session_established:
            blockers.append("authenticated_session_not_established")

        if session is not None and auth_session_established:
            for route_path in ("/scrape", "/api/scrape/jobs"):
                route_url = f"{normalized_base_url}{route_path}"
                try:
                    route_snapshot = _check_render_route(
                        session.get,
                        route_url,
                        route_path,
                        timeout_seconds=timeout_seconds,
                        retries=retries,
                        retry_delay_seconds=retry_delay_seconds,
                    )
                    authenticated_route_checks.append(route_snapshot)
                    if int(route_snapshot.get("attempt_count") or 1) > 1:
                        warnings.append(
                            f"authenticated_route_required_retry:{route_path}:{route_snapshot['attempt_count']}"
                        )
                    if route_snapshot["status_code"] == 404:
                        blockers.append(f"authenticated_route_missing:{route_path}")
                    elif route_snapshot["status_code"] >= 500:
                        blockers.append(f"authenticated_route_server_error:{route_path}")
                    elif route_snapshot["status_code"] == 302 and route_snapshot.get("location") == "/login":
                        blockers.append(f"authenticated_route_redirected_to_login:{route_path}")
                    elif route_snapshot["status_code"] not in {200, 401, 403, 302}:
                        warnings.append(
                            f"authenticated_route_unexpected_status:{route_path}:{route_snapshot['status_code']}"
                        )
                except Exception as exc:
                    authenticated_route_checks.append({"path": route_path, "error": str(exc)})
                    blockers.append(f"authenticated_route_request_failed:{route_path}")

    return {
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "base_url": normalized_base_url,
        "health_url": health_url,
        "health_status_code": health_status_code,
        "health_payload": health_payload,
        "health_error": health_error,
        "health_attempt_count": health_attempt_count,
        "health_attempts": health_attempts,
        "route_checks": route_checks,
        "auth_requested": auth_requested,
        "login_attempted": login_attempted,
        "login_status_code": login_status_code,
        "login_location": login_location,
        "login_error": login_error,
        "login_success": login_success,
        "registration_attempted": registration_attempted,
        "registration_status_code": registration_status_code,
        "registration_location": registration_location,
        "registration_error": registration_error,
        "registration_success": registration_success,
        "authenticated_route_checks": authenticated_route_checks,
        "retry_policy": {
            "retries": max(0, int(retries)),
            "retry_delay_seconds": max(0.0, float(retry_delay_seconds)),
            "retryable_status_codes": [502, 503, 504],
        },
        "expect_queue_backend": expect_queue_backend,
        "expect_runtime_role": expect_runtime_role,
        "expect_scheduler_mode": normalized_scheduler_mode,
    }


def run_render_worker_postdeploy_checklist(blueprint_path: str = "render.yaml") -> dict:
    audit = run_render_blueprint_audit(blueprint_path)
    blueprint = _parse_render_blueprint(blueprint_path)
    services = blueprint["services"]
    services_by_name = {service.get("name"): service for service in services if service.get("name")}
    worker_service = services_by_name.get("esp-worker") or {}
    worker_env = worker_service.get("env_vars", {})

    blockers = list(audit.get("blockers") or [])
    warnings = list(audit.get("warnings") or [])

    def _env_value(key: str, default=None):
        env_entry = worker_env.get(key) or {}
        if "value" in env_entry:
            return env_entry.get("value")
        return default

    def _env_bool(value, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _env_int(value, default: int = 0) -> int:
        if value is None or value == "":
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    manual_envs = sorted(
        key
        for key, env_entry in worker_env.items()
        if str(env_entry.get("sync", "")).lower() == "false"
    )
    managed_envs = sorted(
        key
        for key, env_entry in worker_env.items()
        if env_entry.get("source_type") in {"fromService", "fromDatabase"}
    )
    fixed_envs = {
        key: env_entry.get("value")
        for key, env_entry in worker_env.items()
        if "value" in env_entry
    }

    scheduler_enabled = _env_bool(_env_value("WORKER_ENABLE_SCHEDULER", False))
    warm_browser_pool = _env_bool(_env_value("WARM_BROWSER_POOL", False))
    shared_browser_runtime = _env_bool(_env_value("ENABLE_SHARED_BROWSER_RUNTIME", False))
    reconcile_stalled_jobs = _env_bool(_env_value("WORKER_RECONCILE_STALLED_JOBS_ON_STARTUP", False))
    process_selector_repairs_on_startup = _env_bool(
        _env_value("WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP", False)
    )
    selector_repair_limit = max(1, _env_int(_env_value("WORKER_SELECTOR_REPAIR_LIMIT", 1), default=1))
    selector_repair_min_score = max(0, _env_int(_env_value("SELECTOR_REPAIR_MIN_SCORE", 90), default=90))
    selector_repair_min_canaries = max(2, _env_int(_env_value("SELECTOR_REPAIR_MIN_CANARIES", 2), default=2))
    browser_pool_warm_sites = str(_env_value("BROWSER_POOL_WARM_SITES", "") or "").strip()

    expected_log_markers = [
        "Worker durable job backlog before startup reconcile:",
        "Worker durable job backlog after startup reconcile:",
    ]
    if warm_browser_pool:
        expected_log_markers.append("Worker browser pool warmed:")
    if shared_browser_runtime:
        expected_log_markers.append("Worker browser pool closing:")
    if process_selector_repairs_on_startup:
        expected_log_markers.append("Worker selector repair startup summary:")

    optional_log_markers = []
    if reconcile_stalled_jobs:
        optional_log_markers.append("Reconciled stalled scrape jobs before worker start:")
    optional_log_markers.extend(
        [
            "Worker durable job backlog warning before startup reconcile:",
            "Worker durable job backlog warning after startup reconcile:",
        ]
    )

    manual_checks = [
        "Confirm the worker deploy uses `python worker.py`.",
        "Confirm startup logs do not show Redis connection failures or browser startup exceptions.",
        "Confirm the worker remains running after startup and does not crash-loop.",
        "Keep `WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP=0` for the first paid split deploy.",
        "Fill `SELECTOR_REPAIR_CANARY_URLS_MERCARI_DETAIL` and `SELECTOR_REPAIR_CANARY_URLS_SNKRDUNK_DETAIL` before any repair apply or auto-promotion.",
        "Run `flask process-selector-repairs --limit 1 --dry-run` before enabling automatic selector repair promotion.",
    ]
    if scheduler_enabled:
        manual_checks.append("Confirm this worker is the only scheduler owner for the paid split.")
    if warm_browser_pool:
        manual_checks.append("Confirm browser warm-up logs appear for the expected sites.")
    if process_selector_repairs_on_startup:
        manual_checks.append("If startup automation is enabled later, confirm `Worker selector repair startup summary:` appears once per boot.")

    expected_runtime = {
        "service_name": worker_service.get("name") or "esp-worker",
        "plan": worker_service.get("plan"),
        "docker_command": worker_service.get("dockerCommand"),
        "queue_backend": str(_env_value("SCRAPE_QUEUE_BACKEND", "") or ""),
        "scheduler_enabled": scheduler_enabled,
        "warm_browser_pool": warm_browser_pool,
        "shared_browser_runtime": shared_browser_runtime,
        "browser_pool_warm_sites": [site.strip() for site in browser_pool_warm_sites.split(",") if site.strip()],
        "reconcile_stalled_jobs_on_startup": reconcile_stalled_jobs,
        "process_selector_repairs_on_startup": process_selector_repairs_on_startup,
        "selector_repair_limit": selector_repair_limit,
        "selector_repair_min_score": selector_repair_min_score,
        "selector_repair_min_canaries": selector_repair_min_canaries,
        "backlog_warn_count": max(0, _env_int(_env_value("WORKER_BACKLOG_WARN_COUNT", 0), default=0)),
        "backlog_warn_age_seconds": max(0, _env_int(_env_value("WORKER_BACKLOG_WARN_AGE_SECONDS", 0), default=0)),
    }

    return {
        "ready": not blockers,
        "blueprint_path": blueprint_path,
        "runbook_path": "docs/RENDER_CUTOVER_RUNBOOK.md",
        "blockers": blockers,
        "warnings": warnings,
        "service_name": worker_service.get("name") or "esp-worker",
        "manual_envs": manual_envs,
        "managed_envs": managed_envs,
        "fixed_envs": fixed_envs,
        "expected_runtime": expected_runtime,
        "expected_log_markers": expected_log_markers,
        "optional_log_markers": optional_log_markers,
        "manual_checks": manual_checks,
    }


def run_single_web_postdeploy_smoke(
    base_url: str,
    *,
    timeout_seconds: float = 15.0,
    retries: int = 2,
    retry_delay_seconds: float = 1.0,
    username: str | None = None,
    password: str | None = None,
    ensure_user: bool = False,
) -> dict:
    snapshot = run_render_postdeploy_smoke(
        base_url,
        timeout_seconds=timeout_seconds,
        retries=retries,
        retry_delay_seconds=retry_delay_seconds,
        expect_queue_backend="inmemory",
        expect_runtime_role="web",
        expect_scheduler_mode="enabled",
        username=username,
        password=password,
        ensure_user=ensure_user,
    )
    snapshot = dict(snapshot)
    snapshot["runbook_path"] = "docs/SINGLE_WEB_REDEPLOY_RUNBOOK.md"
    return snapshot


def run_render_cutover_checklist(
    *,
    blueprint_path: str = "render.yaml",
    base_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> dict:
    normalized_base_url = str(base_url or "").strip()
    normalized_username = str(username or "").strip()
    normalized_password = str(password or "")
    cautious_smoke_flags = "--retries 4 --retry-delay-seconds 2"

    pre_cutover_commands = [
        "flask predeploy-check --target single-web",
        "flask schema-drift-check",
        f"flask render-blueprint-audit --blueprint-path {blueprint_path}",
        f"flask render-budget-guardrail-audit --blueprint-path {blueprint_path}",
        f"flask render-dashboard-inputs --blueprint-path {blueprint_path}",
        f"flask render-local-split-checklist --blueprint-path {blueprint_path}",
        "flask render-cutover-readiness --require-backend postgresql --apply-migrations --strict",
        "py -3 -m pytest tests -q",
    ]

    dashboard_steps = [
        "Leave the current single-web production service unchanged.",
        f"Import/sync {blueprint_path} as a dormant Blueprint with new service names only.",
        "Fill manual secret env vars for esp-web and esp-worker.",
        "Fill selector repair canary URL env vars for esp-worker, but keep startup auto-promotion disabled on the first deploy.",
        "Provision esp-postgres and esp-keyvalue before relying on esp-web/esp-worker traffic.",
        "Deploy esp-web first, then esp-worker.",
    ]

    postdeploy_commands = []
    postdeploy_commands.append(
        f"flask render-postdeploy-smoke --base-url {normalized_base_url or 'https://<esp-web-url>'} {cautious_smoke_flags}"
    )
    postdeploy_commands.append(f"flask render-worker-postdeploy-checklist --blueprint-path {blueprint_path}")
    postdeploy_commands.append("flask process-selector-repairs --limit 1 --dry-run")
    if normalized_base_url and normalized_username and normalized_password:
        postdeploy_commands.append(
            "flask render-postdeploy-smoke "
            f"--base-url {normalized_base_url} {cautious_smoke_flags} "
            f"--username {normalized_username} --password <redacted> --ensure-user"
        )
    elif normalized_base_url:
        postdeploy_commands.append(
            "flask render-postdeploy-smoke "
            f"--base-url {normalized_base_url} {cautious_smoke_flags} "
            "--username <smoke-user> --password <smoke-password> --ensure-user"
        )

    postdeploy_commands.extend(
        [
            "Only after the dry-run and canary URLs look correct, consider `flask process-selector-repairs --candidate-id <id> --apply`.",
            "Run one preview scrape smoke in the deployed environment.",
            "Run one persist scrape smoke in the deployed environment.",
            "Confirm /api/scrape/status/<job_id>, /api/scrape/jobs, and /scrape/result/<job_id> all work.",
        ]
    )

    rollback_steps = [
        "Keep the existing single-web production service as the live fallback.",
        "Do not mutate the legacy single-web service into rq mode during rollback.",
        "Fix the issue locally first.",
        "Re-run flask render-cutover-readiness --require-backend postgresql --apply-migrations --strict before retrying.",
    ]

    manual_secret_envs = [
        {"service": "esp-web", "key": "SECRET_KEY", "required": True},
        {"service": "esp-web", "key": "SELECTOR_ALERT_WEBHOOK_URL", "required": False},
        {"service": "esp-worker", "key": "SECRET_KEY", "required": True},
        {"service": "esp-worker", "key": "OPERATIONAL_ALERT_WEBHOOK_URL", "required": False},
    ]

    return {
        "ready": True,
        "blueprint_path": blueprint_path,
        "base_url": normalized_base_url or None,
        "authenticated_smoke_configured": bool(normalized_base_url and normalized_username and normalized_password),
        "pre_cutover_commands": pre_cutover_commands,
        "dashboard_steps": dashboard_steps,
        "manual_secret_envs": manual_secret_envs,
        "postdeploy_commands": postdeploy_commands,
        "rollback_steps": rollback_steps,
    }


def run_single_web_redeploy_checklist(
    *,
    base_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> dict:
    normalized_base_url = str(base_url or "").strip()
    normalized_username = str(username or "").strip()
    normalized_password = str(password or "")
    cautious_smoke_flags = "--retries 4 --retry-delay-seconds 2"

    predeploy_commands = [
        "flask single-web-redeploy-readiness",
    ]

    dashboard_steps = [
        "Keep the current Render service in single-web mode.",
        "Leave SCRAPE_QUEUE_BACKEND as inmemory.",
        "Do not add worker-only Redis/RQ env vars to the current single-web service.",
        "Keep WEB_SCHEDULER_MODE on auto or enabled so the single service remains the scheduler owner.",
        "Redeploy only after the local gate passes.",
    ]

    postdeploy_commands = [
        f"flask single-web-postdeploy-smoke --base-url {normalized_base_url or 'https://<current-web-url>'} {cautious_smoke_flags}"
    ]
    if normalized_base_url and normalized_username and normalized_password:
        postdeploy_commands.append(
            "flask single-web-postdeploy-smoke "
            f"--base-url {normalized_base_url} {cautious_smoke_flags} "
            f"--username {normalized_username} --password <redacted> --ensure-user"
        )
    elif normalized_base_url:
        postdeploy_commands.append(
            "flask single-web-postdeploy-smoke "
            f"--base-url {normalized_base_url} {cautious_smoke_flags} "
            "--username <smoke-user> --password <smoke-password> --ensure-user"
        )

    rollback_steps = [
        "Keep SCRAPE_QUEUE_BACKEND on inmemory during rollback.",
        "Restore the last known-good single-web deployment or release.",
        "Re-run flask predeploy-check --target single-web before retrying the redeploy.",
    ]

    return {
        "ready": True,
        "base_url": normalized_base_url or None,
        "authenticated_smoke_configured": bool(normalized_base_url and normalized_username and normalized_password),
        "predeploy_commands": predeploy_commands,
        "dashboard_steps": dashboard_steps,
        "postdeploy_commands": postdeploy_commands,
        "rollback_steps": rollback_steps,
        "runbook_path": "docs/SINGLE_WEB_REDEPLOY_RUNBOOK.md",
    }


def run_single_web_redeploy_readiness(
    app,
    *,
    strict_parser: bool = False,
) -> dict:
    steps = []
    suite_blockers = []

    def _append_step(name: str, snapshot: dict, *, enforce: bool = True) -> None:
        step_payload = {
            "name": name,
            "ready": bool(snapshot.get("ready", not snapshot.get("blockers"))),
            "blockers": list(snapshot.get("blockers") or []),
            "advisory": not enforce,
        }
        for key in (
            "warnings",
            "target",
            "profile",
            "step_count",
            "failed_step_names",
            "runbook_path",
        ):
            if key in snapshot:
                step_payload[key] = snapshot.get(key)
        steps.append(step_payload)
        if enforce and step_payload["blockers"]:
            suite_blockers.append(name)

    predeploy = build_predeploy_snapshot(app, target="single-web")
    _append_step("single-web-predeploy", predeploy)

    local_verify = run_local_verification_suite(
        app,
        profile="parser",
        strict_parser=strict_parser,
    )
    local_verify_snapshot = {
        "ready": local_verify.get("ready", False),
        "blockers": list(local_verify.get("blockers") or []),
        "profile": local_verify.get("profile"),
        "step_count": len(local_verify.get("steps") or []),
        "failed_step_names": list(local_verify.get("blockers") or []),
        "runbook_path": "docs/SINGLE_WEB_REDEPLOY_RUNBOOK.md",
    }
    _append_step("single-web-local-verify-parser", local_verify_snapshot)

    return {
        "ready": not suite_blockers,
        "strict_parser": strict_parser,
        "runbook_path": "docs/SINGLE_WEB_REDEPLOY_RUNBOOK.md",
        "steps": steps,
        "blockers": suite_blockers,
    }


def build_predeploy_snapshot(app, target: str = "single-web") -> dict:
    normalized_target = str(target or "single-web").strip().lower()
    if normalized_target not in {"single-web", "split-render"}:
        raise ValueError(f"Unsupported predeploy target: {target}")

    schema = describe_schema_bootstrap(app.config.get("SCHEMA_BOOTSTRAP_MODE", "auto"))
    queue_backend = str(app.config.get("SCRAPE_QUEUE_BACKEND", "inmemory") or "inmemory").strip().lower()
    redis_url = str(app.config.get("REDIS_URL", "") or "").strip()
    scheduler_enabled = bool(app.config.get("ENABLE_SCHEDULER", False))
    runtime_role = str(app.config.get("ESP_RUNTIME_ROLE", "base") or "base").strip().lower()
    web_scheduler_mode = str(app.config.get("WEB_SCHEDULER_MODE", "") or "").strip().lower()
    secret_key = str(app.config.get("SECRET_KEY", "") or "")
    configured_image_storage_path = str(IMAGE_STORAGE_PATH)
    image_storage_path = os.path.abspath(configured_image_storage_path)
    selector_repairs_startup_enabled = str(
        app.config.get("WORKER_PROCESS_SELECTOR_REPAIRS_ON_STARTUP", "") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    try:
        selector_repair_limit = max(1, int(app.config.get("WORKER_SELECTOR_REPAIR_LIMIT", 1) or 1))
    except (TypeError, ValueError):
        selector_repair_limit = 1
    try:
        selector_repair_min_score = max(0, int(app.config.get("SELECTOR_REPAIR_MIN_SCORE", 90) or 90))
    except (TypeError, ValueError):
        selector_repair_min_score = 90
    try:
        selector_repair_min_canaries = max(2, int(app.config.get("SELECTOR_REPAIR_MIN_CANARIES", 2) or 2))
    except (TypeError, ValueError):
        selector_repair_min_canaries = 2
    mercari_repair_canaries = str(app.config.get("SELECTOR_REPAIR_CANARY_URLS_MERCARI_DETAIL", "") or "").strip()
    snkrdunk_repair_canaries = str(app.config.get("SELECTOR_REPAIR_CANARY_URLS_SNKRDUNK_DETAIL", "") or "").strip()

    blockers: list[str] = []
    warnings: list[str] = []

    if secret_key == "dev-secret-key-change-this":
        if normalized_target == "split-render":
            blockers.append("secret_key_uses_default_dev_value")
        else:
            warnings.append("secret_key_uses_default_dev_value")

    if not os.path.isdir(image_storage_path):
        blockers.append("image_storage_path_missing")
    elif not os.access(image_storage_path, os.W_OK):
        blockers.append("image_storage_path_not_writable")

    if normalized_target == "single-web":
        if queue_backend != "inmemory":
            blockers.append("single_web_requires_inmemory_queue")
    else:
        if queue_backend != "rq":
            blockers.append("split_render_requires_rq_queue_backend")
        if schema["database_backend"] != "postgresql":
            blockers.append("split_render_requires_postgresql_database")
        if schema["effective_mode"] != "alembic":
            blockers.append("split_render_requires_alembic_schema_bootstrap")
        if scheduler_enabled:
            blockers.append("split_render_web_scheduler_must_be_disabled")
        if redis_url.startswith("redis://localhost") or redis_url.startswith("redis://127.0.0.1"):
            warnings.append("redis_url_points_to_localhost")
        if not os.path.isabs(configured_image_storage_path):
            warnings.append("image_storage_path_should_be_absolute")
        if selector_repairs_startup_enabled and not mercari_repair_canaries:
            blockers.append("selector_repair_canaries_missing:mercari_detail")
        if selector_repairs_startup_enabled and not snkrdunk_repair_canaries:
            blockers.append("selector_repair_canaries_missing:snkrdunk_detail")
        if selector_repairs_startup_enabled and selector_repair_limit > 1:
            warnings.append("selector_repairs_startup_limit_exceeds_cautious_baseline")
        if selector_repair_min_score < 90:
            warnings.append("selector_repair_min_score_less_conservative_than_repo_baseline")

    if queue_backend == "rq" and web_scheduler_mode not in {"", "disabled", "auto"}:
        warnings.append("web_scheduler_mode_is_unexpected")
    if queue_backend == "rq" and web_scheduler_mode == "auto" and runtime_role == "web":
        warnings.append("web_scheduler_mode_auto_relies_on_runtime_resolution")
    if schema["database_backend"] == "postgresql" and schema["effective_mode"] == "legacy":
        warnings.append("postgresql_would_bootstrap_with_legacy_schema_mode")

    return {
        "target": normalized_target,
        "runtime_role": runtime_role,
        "queue_backend": queue_backend,
        "redis_url": redis_url,
        "scheduler_enabled": scheduler_enabled,
        "web_scheduler_mode": web_scheduler_mode,
        "schema": schema,
        "image_storage_path": image_storage_path,
        "selector_repairs": {
            "process_on_startup": selector_repairs_startup_enabled,
            "limit": selector_repair_limit,
            "min_score": selector_repair_min_score,
            "min_canaries": selector_repair_min_canaries,
            "mercari_detail_canaries_configured": bool(mercari_repair_canaries),
            "snkrdunk_detail_canaries_configured": bool(snkrdunk_repair_canaries),
        },
        "blockers": blockers,
        "warnings": warnings,
        "ready": not blockers,
    }


def _load_stack_smoke_fixture(site: str, fixture_path: str, *, target_url: str = "") -> dict:
    parsed = _parse_detail_fixture(site, fixture_path, target_url=target_url)
    title = str(parsed["item"].get("title") or "").strip()
    status = str(parsed["item"].get("status") or "").strip().lower()
    page_type = str(parsed["meta"].get("page_type") or "").strip().lower()

    if page_type in {"deleted_detail", "unknown_page"} or status in {"deleted", "unknown", "error"}:
        raise ValueError(
            f"Stack smoke fixture must parse as an active or sold detail page: "
            f"site={parsed['site']} page_type={page_type or 'unknown'} status={status or 'unknown'}"
        )
    if not title:
        raise ValueError(f"Stack smoke fixture did not produce a title: {Path(parsed['path']).name}")

    return {
        "site": parsed["site"],
        "path": parsed["path"],
        "item": parsed["item"],
        "meta": parsed["meta"],
        "keyword": title,
        "search_url": f"fixture://{parsed['site']}/{Path(parsed['path']).name}",
    }


def run_detail_fixture_smoke(site: str, fixture_path: str, *, target_url: str = "", strict: bool = False) -> dict:
    parsed = _parse_detail_fixture(site, fixture_path, target_url=target_url)
    item = parsed["item"]
    meta = parsed["meta"]
    status = str(item.get("status") or "").strip().lower()
    title = str(item.get("title") or "").strip()
    page_type = str(meta.get("page_type") or "").strip().lower()

    warnings = []
    blockers = []

    if not title:
        warnings.append("title_missing")
    if item.get("price") is None:
        warnings.append("price_missing")
    if not item.get("image_urls"):
        warnings.append("image_urls_missing")
    if page_type in {"unknown_page", "unknown_detail"}:
        warnings.append("page_type_unknown")
    if status in {"deleted", "unknown", "error"}:
        warnings.append(f"status_{status}")

    if strict:
        for warning in warnings:
            if warning not in {"status_deleted"}:
                blockers.append(warning)

    return {
        "ready": not blockers,
        "site": parsed["site"],
        "fixture_path": parsed["path"],
        "target_url": item.get("url"),
        "status": status,
        "page_type": meta.get("page_type"),
        "confidence": meta.get("confidence"),
        "title": title,
        "price": item.get("price"),
        "image_count": len(item.get("image_urls") or []),
        "variant_count": len(item.get("variants") or []),
        "warnings": warnings,
        "blockers": blockers,
        "item": item,
        "meta": meta,
    }


def run_search_fixture_smoke(site: str, fixture_path: str, *, target_url: str = "", strict: bool = False) -> dict:
    parsed = _parse_search_fixture(site, fixture_path, target_url=target_url)
    meta = parsed["meta"]
    item_urls = list(parsed["item_urls"] or [])

    warnings = []
    blockers = []

    if not item_urls:
        blockers.append("item_urls_missing")
    if meta.get("page_type") == "search_skeleton":
        blockers.append("search_results_not_rendered")
    elif meta.get("page_type") == "unknown_search":
        blockers.append("page_type_unknown")
    elif meta.get("page_type") == "search_empty":
        warnings.append("search_results_empty")

    if not meta.get("heading"):
        warnings.append("search_heading_missing")
    if not meta.get("canonical_url"):
        warnings.append("canonical_url_missing")

    if strict and warnings:
        blockers.extend(warnings)

    return {
        "ready": not blockers,
        "site": parsed["site"],
        "fixture_path": parsed["path"],
        "target_url": parsed["search_url"],
        "page_type": meta.get("page_type"),
        "confidence": meta.get("confidence"),
        "heading": meta.get("heading"),
        "canonical_url": meta.get("canonical_url"),
        "item_count": len(item_urls),
        "sample_item_urls": item_urls[:5],
        "warnings": warnings,
        "blockers": blockers,
        "meta": meta,
    }


def run_selector_repair_cycle(
    app,
    *,
    limit: int = 10,
    candidate_id: int | None = None,
    apply: bool = False,
) -> dict:
    del app  # CLI parity with other helpers; current implementation only needs configured globals.

    store_state = inspect_repair_store_state()
    blockers = list(store_state.get("blockers") or [])
    warnings: list[str] = []
    safe_limit = max(1, int(limit or 10))

    if blockers:
        return {
            "ready": False,
            "mode": "apply" if apply else "dry_run",
            "apply": bool(apply),
            "limit": safe_limit,
            "candidate_id": candidate_id,
            "store": store_state,
            "blockers": blockers,
            "warnings": warnings,
            "results": [],
            "inspected": 0,
        }

    summary = (
        process_pending_repair_candidates(limit=safe_limit, candidate_id=candidate_id)
        if apply
        else preview_pending_repair_candidates(limit=safe_limit, candidate_id=candidate_id)
    )

    inspected = int(summary.get("inspected") or 0)
    if candidate_id is not None and inspected == 0:
        blockers.append("candidate_not_found")
    elif inspected == 0:
        warnings.append("no_pending_candidates")

    return {
        "ready": not blockers,
        "mode": "apply" if apply else "dry_run",
        "apply": bool(apply),
        "limit": safe_limit,
        "candidate_id": candidate_id,
        "store": store_state,
        "blockers": blockers,
        "warnings": warnings,
        **summary,
    }


def run_local_verification_suite(
    app,
    *,
    profile: str = "full",
    require_backend: str = "postgresql",
    apply_migrations: bool = True,
    strict_parser: bool = False,
) -> dict:
    normalized_profile = str(profile or "full").strip().lower()
    if normalized_profile not in {"parser", "stack", "full"}:
        raise ValueError(f"Unsupported local verification profile: {profile}")

    steps = []
    suite_blockers = []

    def _append_step(name: str, snapshot: dict, *, enforce: bool = True) -> None:
        step_payload = {
            "name": name,
            "ready": bool(snapshot.get("ready", not snapshot.get("blockers"))),
            "blockers": list(snapshot.get("blockers") or []),
            "advisory": not enforce,
        }
        for key in (
            "warnings",
            "site",
            "status",
            "page_type",
            "item_count",
            "queue_name",
            "job_id",
            "target",
            "mode",
        ):
            if key in snapshot:
                step_payload[key] = snapshot.get(key)
        steps.append(step_payload)
        if enforce and step_payload["blockers"]:
            suite_blockers.append(name)

    single_web_predeploy = build_predeploy_snapshot(app, target="single-web")
    _append_step("predeploy-single-web", single_web_predeploy, enforce=False)

    schema_drift = inspect_additive_schema_drift()
    _append_step("schema-drift-check", schema_drift)

    if normalized_profile in {"stack", "full"}:
        split_render_predeploy = build_predeploy_snapshot(app, target="split-render")
        _append_step("predeploy-split-render", split_render_predeploy, enforce=False)

    if normalized_profile in {"parser", "full"}:
        single_web_smoke = run_single_web_smoke(
            app,
            mode="preview",
            keep_artifacts=False,
        )
        _append_step("single-web-smoke-preview", single_web_smoke)

        if normalized_profile == "full":
            single_web_fixture_persist = run_single_web_smoke(
                app,
                mode="persist",
                keep_artifacts=False,
                fixture_site="mercari",
                fixture_path="mercari_page_dump_live.html",
                fixture_target_url="https://jp.mercari.com/item/m71383569733",
            )
            _append_step("single-web-smoke-mercari-persist", single_web_fixture_persist)

            single_web_snkrdunk_persist = run_single_web_smoke(
                app,
                mode="persist",
                keep_artifacts=False,
                fixture_site="snkrdunk",
                fixture_path="dump.html",
                fixture_target_url="https://snkrdunk.com/products/nike-air-max-95-og-big-bubble-neon-yellow-2025-2026",
            )
            _append_step("single-web-smoke-snkrdunk-persist", single_web_snkrdunk_persist)

        mercari_active = run_detail_fixture_smoke(
            "mercari",
            "mercari_page_dump_live.html",
            target_url="https://jp.mercari.com/item/m71383569733",
            strict=strict_parser,
        )
        _append_step("detail-mercari-active", mercari_active)

        mercari_deleted = run_detail_fixture_smoke(
            "mercari",
            "mercari_page_dump.html",
            target_url="https://jp.mercari.com/item/m71383569733",
            strict=False,
        )
        _append_step("detail-mercari-deleted", mercari_deleted)

        search_fixture_path = Path("search_dump.html")
        if search_fixture_path.exists():
            mercari_search = run_search_fixture_smoke(
                "mercari",
                str(search_fixture_path),
                target_url="https://jp.mercari.com/search?keyword=sneaker",
                strict=False,
            )
            _append_step("search-mercari-fixture", mercari_search, enforce=False)

        snkrdunk_active = run_detail_fixture_smoke(
            "snkrdunk",
            "dump.html",
            target_url="https://snkrdunk.com/products/nike-air-max-95-og-big-bubble-neon-yellow-2025-2026",
            strict=strict_parser,
        )
        _append_step("detail-snkrdunk-active", snkrdunk_active)

    if normalized_profile in {"stack", "full"}:
        db_snapshot = run_database_smoke_check(
            require_backend=require_backend,
            apply_migrations=apply_migrations,
            schema_mode=app.config.get("SCHEMA_BOOTSTRAP_MODE", "auto"),
        )
        _append_step("db-smoke", db_snapshot)

        mercari_stack = run_stack_smoke(
            app,
            require_backend=require_backend,
            apply_migrations=apply_migrations,
            mode="persist",
            keep_artifacts=False,
            fixture_site="mercari",
            fixture_path="mercari_page_dump_live.html",
            fixture_target_url="https://jp.mercari.com/item/m71383569733",
        )
        _append_step("stack-mercari-persist", mercari_stack)

        snkrdunk_stack = run_stack_smoke(
            app,
            require_backend=require_backend,
            apply_migrations=apply_migrations,
            mode="persist",
            keep_artifacts=False,
            fixture_site="snkrdunk",
            fixture_path="dump.html",
            fixture_target_url="https://snkrdunk.com/products/nike-air-max-95-og-big-bubble-neon-yellow-2025-2026",
        )
        _append_step("stack-snkrdunk-persist", snkrdunk_stack)

    return {
        "ready": not suite_blockers,
        "profile": normalized_profile,
        "require_backend": require_backend,
        "apply_migrations": apply_migrations,
        "strict_parser": strict_parser,
        "steps": steps,
        "blockers": suite_blockers,
    }


def run_render_cutover_readiness(
    app,
    *,
    require_backend: str = "postgresql",
    apply_migrations: bool = True,
    strict: bool = False,
    strict_parser: bool = False,
) -> dict:
    from app import create_web_app, create_worker_app

    local_only_split_warnings = {
        "redis_url_points_to_localhost",
        "image_storage_path_should_be_absolute",
    }

    current_web_app = (
        app
        if app.config.get("TESTING")
        else create_web_app(
            config_overrides={
                "SCRAPE_QUEUE_BACKEND": "inmemory",
                "WEB_SCHEDULER_MODE": "auto",
            }
        )
    )
    split_web_app = create_web_app(
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "rq",
            "WEB_SCHEDULER_MODE": "disabled",
        }
    )
    split_worker_app = create_worker_app(
        config_overrides={
            "SCRAPE_QUEUE_BACKEND": "rq",
            "WORKER_ENABLE_SCHEDULER": False,
        }
    )

    steps = []
    suite_blockers = []

    def _append_step(name: str, snapshot: dict, *, enforce: bool = True) -> None:
        step_payload = {
            "name": name,
            "ready": bool(snapshot.get("ready", not snapshot.get("blockers"))),
            "blockers": list(snapshot.get("blockers") or []),
            "advisory": not enforce,
        }
        for key in (
            "warnings",
            "target",
            "queue_backend",
            "redis_ok",
            "redis_error",
            "profile",
            "step_count",
            "failed_step_names",
            "runbook_path",
        ):
            if key in snapshot:
                step_payload[key] = snapshot.get(key)
        steps.append(step_payload)
        if enforce and step_payload["blockers"]:
            suite_blockers.append(name)

    current_prod_predeploy = build_predeploy_snapshot(current_web_app, target="single-web")
    _append_step("current-single-web-predeploy", current_prod_predeploy, enforce=False)

    schema_drift = inspect_additive_schema_drift()
    _append_step("schema-drift-check", schema_drift)

    split_render_predeploy = build_predeploy_snapshot(split_web_app, target="split-render")
    if strict and split_render_predeploy.get("warnings"):
        promoted_warnings = [
            warning
            for warning in list(split_render_predeploy.get("warnings") or [])
            if warning not in local_only_split_warnings
        ]
        split_render_predeploy = dict(split_render_predeploy)
        split_render_predeploy["blockers"] = list(split_render_predeploy.get("blockers") or []) + promoted_warnings
        split_render_predeploy["ready"] = not split_render_predeploy["blockers"]
    _append_step("split-render-predeploy", split_render_predeploy)

    blueprint_audit = run_render_blueprint_audit("render.yaml")
    if strict and blueprint_audit.get("warnings"):
        blueprint_audit = dict(blueprint_audit)
        blueprint_audit["blockers"] = list(blueprint_audit.get("blockers") or []) + list(
            blueprint_audit.get("warnings") or []
        )
        blueprint_audit["ready"] = not blueprint_audit["blockers"]
    _append_step("render-blueprint-audit", blueprint_audit)

    budget_guardrail = run_render_budget_guardrail_audit("render.yaml")
    _append_step("render-budget-guardrail-audit", budget_guardrail)

    worker_health = get_worker_health_snapshot(split_worker_app)
    worker_health_blockers = []
    if worker_health.get("queue_backend") == "rq" and worker_health.get("redis_ok") is False:
        worker_health_blockers.append("redis_connection_failed")
    if strict and worker_health.get("backlog_issues"):
        worker_health_blockers.extend(list(worker_health.get("backlog_issues") or []))
    worker_health_snapshot = dict(worker_health)
    worker_health_snapshot["blockers"] = worker_health_blockers
    worker_health_snapshot["ready"] = not worker_health_blockers
    _append_step("split-render-worker-health", worker_health_snapshot)

    local_verify = run_local_verification_suite(
        current_web_app,
        profile="full",
        require_backend=require_backend,
        apply_migrations=apply_migrations,
        strict_parser=strict_parser,
    )
    local_verify_snapshot = {
        "ready": local_verify.get("ready", False),
        "blockers": list(local_verify.get("blockers") or []),
        "profile": local_verify.get("profile"),
        "step_count": len(local_verify.get("steps") or []),
        "failed_step_names": list(local_verify.get("blockers") or []),
        "runbook_path": "docs/RENDER_CUTOVER_RUNBOOK.md",
    }
    _append_step("local-verify-full", local_verify_snapshot)

    return {
        "ready": not suite_blockers,
        "require_backend": require_backend,
        "apply_migrations": apply_migrations,
        "strict": strict,
        "strict_parser": strict_parser,
        "expected_render_services": ["esp-web", "esp-worker", "esp-keyvalue", "esp-postgres"],
        "runbook_path": "docs/RENDER_CUTOVER_RUNBOOK.md",
        "steps": steps,
        "blockers": suite_blockers,
    }


def run_single_web_smoke(
    app,
    *,
    mode: str = "preview",
    keep_artifacts: bool = False,
    fixture_site: str | None = None,
    fixture_path: str | None = None,
    fixture_target_url: str | None = None,
    poll_timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.1,
) -> dict:
    from jobs.scrape_tasks import execute_scrape_job
    from models import Product, ProductSnapshot, ScrapeJob, ScrapeJobEvent, User, Variant
    from services.queue_backend import get_queue_backend
    from services.scrape_request import build_scrape_job_context, build_scrape_task_request

    normalized_mode = str(mode or "preview").strip().lower()
    if normalized_mode not in {"preview", "persist"}:
        raise ValueError(f"Unsupported single-web smoke mode: {mode}")
    normalized_fixture_site = str(fixture_site or "").strip().lower()
    normalized_fixture_path = str(fixture_path or "").strip()
    normalized_fixture_target_url = str(fixture_target_url or "").strip()
    if bool(normalized_fixture_site) != bool(normalized_fixture_path):
        raise ValueError("single-web-smoke fixture requires both --fixture-site and --fixture-path")

    predeploy = build_predeploy_snapshot(app, target="single-web")
    blockers = []
    queue_backend = "inmemory"
    site = "mercari"
    keyword = "single web smoke"
    smoke_title = f"Single Web Smoke Item {uuid.uuid4().hex[:6]}"
    smoke_item_url = f"https://jp.mercari.com/item/m-single-web-smoke-{uuid.uuid4().hex[:10]}"
    smoke_items = [
        {
            "url": smoke_item_url,
            "title": smoke_title,
            "price": 1980,
            "status": "on_sale",
            "description": "single web smoke preview item",
            "image_urls": ["https://img.example.com/single-web-smoke.jpg"],
            "variants": [],
        }
    ]
    search_url = f"internal://single-web-smoke/{uuid.uuid4().hex[:10]}"
    fixture_details = None
    fixture_error = None
    if normalized_fixture_path:
        try:
            fixture_payload = _load_stack_smoke_fixture(
                normalized_fixture_site,
                normalized_fixture_path,
                target_url=normalized_fixture_target_url,
            )
            site = fixture_payload["site"]
            smoke_items = [fixture_payload["item"]]
            smoke_title = str(smoke_items[0].get("title") or smoke_title)
            smoke_item_url = str(smoke_items[0].get("url") or smoke_item_url)
            keyword = str(fixture_payload.get("keyword") or smoke_title or keyword)
            search_url = str(fixture_payload.get("search_url") or search_url)
            fixture_details = {
                "site": site,
                "path": fixture_payload["path"],
                "page_type": fixture_payload["meta"].get("page_type"),
                "confidence": fixture_payload["meta"].get("confidence"),
                "status": smoke_items[0].get("status"),
            }
        except Exception as exc:
            fixture_error = str(exc)
            blockers.append("single_web_smoke_fixture_invalid")

    persist_to_db = normalized_mode == "persist"
    result = {
        "ready": False,
        "blockers": blockers,
        "predeploy": predeploy,
        "mode": normalized_mode,
        "persist_to_db": persist_to_db,
        "queue_backend": queue_backend,
        "fixture": fixture_details,
        "fixture_error": fixture_error,
        "smoke_item_url": smoke_item_url,
        "job_id": None,
        "login_status_code": None,
        "status_api_status_code": None,
        "result_page_status_code": None,
        "result_page_contains_title": False,
        "status_payload": None,
        "jobs_payload_count": None,
        "poll_attempts": 0,
        "persistence": None,
        "artifacts_kept": keep_artifacts,
    }
    if blockers:
        return result

    config_keys = ("SCRAPE_QUEUE_BACKEND", "SCRAPE_QUEUE_NAME")
    original_config = {key: app.config.get(key) for key in config_keys}
    user_id = None
    job_id = None

    try:
        app.config.update(
            {
                "SCRAPE_QUEUE_BACKEND": "inmemory",
                "SCRAPE_QUEUE_NAME": "scrape",
            }
        )

        session_db = SessionLocal()
        try:
            user = User(username=f"single_web_smoke_{uuid.uuid4().hex[:10]}")
            user.set_password("single-web-smoke-password")
            session_db.add(user)
            session_db.commit()
            user_id = user.id
            username = user.username
        finally:
            session_db.close()

        client = app.test_client()
        login_response = client.post(
            "/login",
            data={"username": username, "password": "single-web-smoke-password"},
            follow_redirects=False,
        )
        result["login_status_code"] = login_response.status_code
        if login_response.status_code not in {200, 302}:
            result["blockers"].append("single_web_smoke_login_failed")
            return result

        request_payload = build_scrape_task_request(
            site=site,
            target_url="",
            keyword=keyword,
            price_min=None,
            price_max=None,
            sort="created_desc",
            category=None,
            limit=10,
            user_id=user_id,
            persist_to_db=persist_to_db,
            shop_id=None,
        )
        request_payload["__smoke_result"] = {
            "site": site,
            "items": smoke_items,
            "search_url": search_url,
            "keyword": keyword,
        }
        context = build_scrape_job_context(
            site=site,
            target_url="",
            keyword=keyword,
            limit=10,
            persist_to_db=persist_to_db,
        )

        with app.app_context():
            queue = get_queue_backend()
        job_id = queue.enqueue(
            site=site,
            task_fn=lambda: execute_scrape_job(request_payload),
            user_id=user_id,
            context=context,
            request_payload=request_payload,
            mode=normalized_mode,
        )
        result["job_id"] = job_id

        status_payload = None
        status_response_code = None
        deadline = time.time() + max(1.0, float(poll_timeout_seconds or 10.0))
        while time.time() < deadline:
            result["poll_attempts"] += 1
            status_response = client.get(f"/api/scrape/status/{job_id}")
            status_response_code = status_response.status_code
            if status_response.status_code == 200:
                status_payload = status_response.get_json() or {}
                if status_payload.get("status") in {"completed", "failed"}:
                    break
            time.sleep(max(0.01, float(poll_interval_seconds or 0.1)))

        result["status_api_status_code"] = status_response_code
        result["status_payload"] = status_payload
        if status_response_code != 200:
            result["blockers"].append("single_web_smoke_status_api_failed")
            return result
        if not status_payload or status_payload.get("status") != "completed":
            result["blockers"].append("single_web_smoke_job_not_completed")

        jobs_response = client.get("/api/scrape/jobs?limit=1")
        if jobs_response.status_code == 200:
            jobs_payload = jobs_response.get_json() or {}
            result["jobs_payload_count"] = len(jobs_payload.get("jobs") or [])
        else:
            result["blockers"].append("single_web_smoke_jobs_api_failed")

        result_page_response = client.get(f"/scrape/result/{job_id}")
        result["result_page_status_code"] = result_page_response.status_code
        result_page_body = result_page_response.get_data(as_text=True)
        result["result_page_contains_title"] = _response_contains_title(result_page_body, smoke_title)
        if result_page_response.status_code != 200:
            result["blockers"].append("single_web_smoke_result_page_failed")
        if not result["result_page_contains_title"]:
            result["blockers"].append("single_web_smoke_result_page_missing_title")

        persistence_session = SessionLocal()
        try:
            persisted_products = persistence_session.query(Product).filter(
                Product.user_id == user_id,
                Product.source_url == smoke_item_url,
            ).all()
            if persist_to_db:
                variant_count = 0
                snapshot_count = 0
                last_title = None
                last_status = None
                if persisted_products:
                    product = persisted_products[0]
                    variant_count = persistence_session.query(Variant).filter(
                        Variant.product_id == product.id
                    ).count()
                    snapshot_count = persistence_session.query(ProductSnapshot).filter(
                        ProductSnapshot.product_id == product.id
                    ).count()
                    last_title = product.last_title
                    last_status = product.last_status

                result["persistence"] = {
                    "product_count": len(persisted_products),
                    "variant_count": variant_count,
                    "snapshot_count": snapshot_count,
                    "last_title": last_title,
                    "last_status": last_status,
                }
                if len(persisted_products) != 1:
                    result["blockers"].append("single_web_smoke_persist_missing_product")
                if snapshot_count < 1:
                    result["blockers"].append("single_web_smoke_persist_missing_snapshot")
                if variant_count < 1:
                    result["blockers"].append("single_web_smoke_persist_missing_variant")
                if last_title != smoke_title:
                    result["blockers"].append("single_web_smoke_persist_title_mismatch")
            else:
                result["persistence"] = {"product_count": len(persisted_products)}
                if persisted_products:
                    result["blockers"].append("single_web_smoke_preview_persisted_product")
        finally:
            persistence_session.close()

        result["ready"] = not result["blockers"]
        return result
    finally:
        for key, value in original_config.items():
            app.config[key] = value

        if not keep_artifacts:
            cleanup_session = SessionLocal()
            try:
                if user_id is not None:
                    for product in cleanup_session.query(Product).filter(Product.user_id == user_id).all():
                        cleanup_session.delete(product)
                if job_id:
                    cleanup_session.query(ScrapeJobEvent).filter(ScrapeJobEvent.job_id == job_id).delete()
                    cleanup_session.query(ScrapeJob).filter(ScrapeJob.job_id == job_id).delete()
                if user_id is not None:
                    cleanup_session.query(User).filter(User.id == user_id).delete()
                cleanup_session.commit()
            finally:
                cleanup_session.close()


def run_stack_smoke(
    app,
    *,
    require_backend: str = "postgresql",
    apply_migrations: bool = True,
    mode: str = "preview",
    keep_artifacts: bool = False,
    fixture_site: str | None = None,
    fixture_path: str | None = None,
    fixture_target_url: str | None = None,
) -> dict:
    from app import create_worker_app
    from models import Product, ProductSnapshot, ScrapeJob, ScrapeJobEvent, User, Variant
    from services.queue_backend import RQQueueBackend
    from services.scrape_request import build_scrape_job_context, build_scrape_task_request
    from services.worker_runtime import run_worker

    normalized_mode = str(mode or "preview").strip().lower()
    if normalized_mode not in {"preview", "persist"}:
        raise ValueError(f"Unsupported stack smoke mode: {mode}")
    normalized_fixture_site = str(fixture_site or "").strip().lower()
    normalized_fixture_path = str(fixture_path or "").strip()
    normalized_fixture_target_url = str(fixture_target_url or "").strip()
    if bool(normalized_fixture_site) != bool(normalized_fixture_path):
        raise ValueError("stack-smoke fixture requires both --fixture-site and --fixture-path")

    db_snapshot = run_database_smoke_check(
        require_backend=require_backend,
        apply_migrations=apply_migrations,
        schema_mode=app.config.get("SCHEMA_BOOTSTRAP_MODE", "auto"),
    )
    blockers = list(db_snapshot.get("blockers") or [])

    redis_url = str(app.config.get("REDIS_URL", os.environ.get("REDIS_URL", "redis://localhost:6379/0")) or "").strip()
    queue_name = f"stack-smoke-{uuid.uuid4().hex[:10]}"
    site = "mercari"
    keyword = "stack smoke"
    smoke_title = f"Stack Smoke Item {queue_name[-6:]}"
    smoke_item_url = f"https://jp.mercari.com/item/m-stack-smoke-{queue_name[-6:]}"
    smoke_items = [
        {
            "url": smoke_item_url,
            "title": smoke_title,
            "price": 1980,
            "status": "on_sale",
            "description": "stack smoke preview item",
            "image_urls": ["https://img.example.com/stack-smoke.jpg"],
            "variants": [],
        }
    ]
    search_url = f"internal://stack-smoke/{queue_name}"
    fixture_details = None
    fixture_error = None
    if normalized_fixture_path:
        try:
            fixture_payload = _load_stack_smoke_fixture(
                normalized_fixture_site,
                normalized_fixture_path,
                target_url=normalized_fixture_target_url,
            )
            site = fixture_payload["site"]
            smoke_items = [fixture_payload["item"]]
            smoke_title = str(smoke_items[0].get("title") or smoke_title)
            smoke_item_url = str(smoke_items[0].get("url") or smoke_item_url)
            keyword = str(fixture_payload.get("keyword") or smoke_title or keyword)
            search_url = str(fixture_payload.get("search_url") or search_url)
            fixture_details = {
                "site": site,
                "path": fixture_payload["path"],
                "page_type": fixture_payload["meta"].get("page_type"),
                "confidence": fixture_payload["meta"].get("confidence"),
                "status": smoke_items[0].get("status"),
            }
        except Exception as exc:
            fixture_error = str(exc)
            blockers.append("stack_smoke_fixture_invalid")

    persist_to_db = normalized_mode == "persist"
    redis_ok = False
    redis_error = None
    try:
        Redis.from_url(redis_url).ping()
        redis_ok = True
    except Exception as exc:
        redis_error = str(exc)
        blockers.append("redis_connection_failed")

    result = {
        "ready": False,
        "blockers": blockers,
        "db": db_snapshot,
        "mode": normalized_mode,
        "persist_to_db": persist_to_db,
        "redis_url": redis_url,
        "redis_ok": redis_ok,
        "redis_error": redis_error,
        "fixture": fixture_details,
        "fixture_error": fixture_error,
        "queue_name": queue_name,
        "smoke_item_url": smoke_item_url,
        "job_id": None,
        "worker_exit_code": None,
        "login_status_code": None,
        "status_api_status_code": None,
        "result_page_status_code": None,
        "result_page_contains_title": False,
        "status_payload": None,
        "jobs_payload_count": None,
        "persistence": None,
        "artifacts_kept": keep_artifacts,
    }
    if blockers:
        return result

    config_keys = ("SCRAPE_QUEUE_BACKEND", "REDIS_URL", "SCRAPE_QUEUE_NAME")
    original_config = {key: app.config.get(key) for key in config_keys}
    user_id = None
    job_id = None

    try:
        app.config.update(
            {
                "SCRAPE_QUEUE_BACKEND": "rq",
                "REDIS_URL": redis_url,
                "SCRAPE_QUEUE_NAME": queue_name,
            }
        )

        session_db = SessionLocal()
        try:
            user = User(username=f"stack_smoke_{uuid.uuid4().hex[:10]}")
            user.set_password("stack-smoke-password")
            session_db.add(user)
            session_db.commit()
            user_id = user.id
            username = user.username
        finally:
            session_db.close()

        client = app.test_client()
        login_response = client.post(
            "/login",
            data={"username": username, "password": "stack-smoke-password"},
            follow_redirects=False,
        )
        result["login_status_code"] = login_response.status_code
        if login_response.status_code not in {200, 302}:
            result["blockers"].append("stack_smoke_login_failed")
            return result

        request_payload = build_scrape_task_request(
            site=site,
            target_url="",
            keyword=keyword,
            price_min=None,
            price_max=None,
            sort="created_desc",
            category=None,
            limit=10,
            user_id=user_id,
            persist_to_db=persist_to_db,
            shop_id=None,
        )
        request_payload["__smoke_result"] = {
            "site": site,
            "items": smoke_items,
            "search_url": search_url,
            "keyword": keyword,
        }
        context = build_scrape_job_context(
            site=site,
            target_url="",
            keyword=keyword,
            limit=10,
            persist_to_db=persist_to_db,
        )

        queue = RQQueueBackend(redis_url=redis_url, queue_name=queue_name)
        job_id = queue.enqueue(
            site=site,
            task_fn=lambda: None,
            user_id=user_id,
            context=context,
            request_payload=request_payload,
            mode=normalized_mode,
        )
        result["job_id"] = job_id

        worker_app = create_worker_app(
            config_overrides={
                "SCRAPE_QUEUE_BACKEND": "rq",
                "REDIS_URL": redis_url,
                "SCRAPE_QUEUE_NAME": queue_name,
                "RQ_BURST": True,
                "WARM_BROWSER_POOL": False,
                "ENABLE_SHARED_BROWSER_RUNTIME": False,
                "WORKER_ENABLE_SCHEDULER": False,
            }
        )
        worker_exit_code = run_worker(worker_app)
        result["worker_exit_code"] = worker_exit_code
        if worker_exit_code != 0:
            result["blockers"].append("stack_smoke_worker_failed")
            return result

        status_response = client.get(f"/api/scrape/status/{job_id}")
        result["status_api_status_code"] = status_response.status_code
        if status_response.status_code != 200:
            result["blockers"].append("stack_smoke_status_api_failed")
            return result

        status_payload = status_response.get_json()
        result["status_payload"] = status_payload
        if status_payload.get("status") != "completed":
            result["blockers"].append("stack_smoke_job_not_completed")

        jobs_response = client.get("/api/scrape/jobs?limit=1")
        if jobs_response.status_code == 200:
            jobs_payload = jobs_response.get_json() or {}
            result["jobs_payload_count"] = len(jobs_payload.get("jobs") or [])
        else:
            result["blockers"].append("stack_smoke_jobs_api_failed")

        result_page_response = client.get(f"/scrape/result/{job_id}")
        result["result_page_status_code"] = result_page_response.status_code
        result_page_body = result_page_response.get_data(as_text=True)
        result["result_page_contains_title"] = _response_contains_title(result_page_body, smoke_title)
        if result_page_response.status_code != 200:
            result["blockers"].append("stack_smoke_result_page_failed")

        if not result["result_page_contains_title"]:
            result["blockers"].append("stack_smoke_result_page_missing_title")

        persistence_session = SessionLocal()
        try:
            persisted_products = persistence_session.query(Product).filter(
                Product.user_id == user_id,
                Product.source_url == smoke_item_url,
            ).all()
            if persist_to_db:
                variant_count = 0
                snapshot_count = 0
                last_title = None
                last_status = None
                if persisted_products:
                    product = persisted_products[0]
                    variant_count = persistence_session.query(Variant).filter(
                        Variant.product_id == product.id
                    ).count()
                    snapshot_count = persistence_session.query(ProductSnapshot).filter(
                        ProductSnapshot.product_id == product.id
                    ).count()
                    last_title = product.last_title
                    last_status = product.last_status

                result["persistence"] = {
                    "product_count": len(persisted_products),
                    "variant_count": variant_count,
                    "snapshot_count": snapshot_count,
                    "last_title": last_title,
                    "last_status": last_status,
                }
                if len(persisted_products) != 1:
                    result["blockers"].append("stack_smoke_persist_missing_product")
                if snapshot_count < 1:
                    result["blockers"].append("stack_smoke_persist_missing_snapshot")
                if variant_count < 1:
                    result["blockers"].append("stack_smoke_persist_missing_variant")
                if last_title != smoke_title:
                    result["blockers"].append("stack_smoke_persist_title_mismatch")
            else:
                result["persistence"] = {"product_count": len(persisted_products)}
                if persisted_products:
                    result["blockers"].append("stack_smoke_preview_persisted_product")
        finally:
            persistence_session.close()

        result["ready"] = not result["blockers"]
        return result
    finally:
        for key, value in original_config.items():
            app.config[key] = value

        if not keep_artifacts:
            cleanup_session = SessionLocal()
            try:
                if user_id is not None:
                    for product in cleanup_session.query(Product).filter(Product.user_id == user_id).all():
                        cleanup_session.delete(product)
                if job_id:
                    cleanup_session.query(ScrapeJobEvent).filter(ScrapeJobEvent.job_id == job_id).delete()
                    cleanup_session.query(ScrapeJob).filter(ScrapeJob.job_id == job_id).delete()
                if user_id is not None:
                    cleanup_session.query(User).filter(User.id == user_id).delete()
                cleanup_session.commit()
            finally:
                cleanup_session.close()


def register_cli_commands(app):
    """Register CLI commands with the Flask app."""

    @app.cli.command("update-products")
    @click.option("--site", "site_filter", type=click.Choice(sorted(_get_single_item_scrapers().keys())), default=None)
    @click.option("--user-id", type=int, default=None)
    @click.option("--limit", type=int, default=None)
    @click.option("--dry-run", is_flag=True, default=False)
    def update_products(site_filter, user_id, limit, dry_run):
        """Re-check stored product price/status with site-aware scrapers."""
        session_db = SessionLocal()
        scraper_map = _get_single_item_scrapers()
        repricing_product_ids = set()

        try:
            query = session_db.query(Product).filter(
                Product.deleted_at == None,
                Product.archived != True,
            )
            if site_filter:
                query = query.filter(Product.site == site_filter)
            if user_id is not None:
                query = query.filter(Product.user_id == user_id)
            query = query.order_by(Product.updated_at.asc())
            if limit is not None and limit > 0:
                query = query.limit(limit)

            products = query.all()
            total = len(products)
            print(f"Start updating {total} products... dry_run={dry_run}")

            updated_count = 0
            skipped_uncertain = 0

            for index, product in enumerate(products, 1):
                scraper_fn = scraper_map.get(product.site)
                if scraper_fn is None:
                    print(f"[{index}/{total}] Unsupported site: {product.site}")
                    continue

                url = product.source_url
                print(f"[{index}/{total}] User:{product.user_id} Site:{product.site} | Checking: {url}")

                try:
                    items = scraper_fn(url, headless=True)
                    if not items:
                        print("  -> Failed to scrape.")
                        continue

                    raw_item = items[0]
                    item = normalize_item_for_persistence(raw_item)
                    meta = raw_item.get("_scrape_meta") or {}
                    action = evaluate_persistence(product.site, item, meta, product)

                    if action == "reject":
                        skipped_uncertain += 1
                        print(
                            "  -> SKIP uncertain"
                            f" site={product.site}"
                            f" url={url}"
                            f" reason={build_policy_reason(item, meta)}"
                            f" old_price={product.last_price}"
                            f" new_price_candidate={item.get('price')}"
                            f" status_candidate={item.get('status')}"
                        )
                        continue

                    status = item.get("status") or product.last_status or "unknown"
                    if action == "allow_status_only":
                        existing_variants = session_db.query(Variant).filter_by(product_id=product.id).all()
                        status_changed = status != product.last_status
                        inventory_changed = False
                        if status in {"sold", "deleted"}:
                            inventory_changed = any((variant.inventory_qty or 0) != 0 for variant in existing_variants)

                        if not status_changed and not inventory_changed:
                            print("  -> No change.")
                            continue

                        if dry_run:
                            print(f"  -> WOULD UPDATE status-only: {product.last_status}->{status}")
                        else:
                            product.last_status = status
                            product.updated_at = utc_now()
                            if status in {"sold", "deleted"}:
                                for variant in existing_variants:
                                    variant.inventory_qty = 0
                            updated_count += 1
                        continue

                    new_price = item.get("price")
                    new_status = status
                    new_title = item.get("title") or product.last_title or ""
                    price_changed = new_price is not None and product.last_price != new_price
                    status_changed = new_status != product.last_status
                    title_changed = new_title and product.last_title != new_title

                    existing_variants = session_db.query(Variant).filter_by(product_id=product.id).all()
                    inventory_changed = False
                    if new_status in {"sold", "deleted"}:
                        inventory_changed = any((variant.inventory_qty or 0) != 0 for variant in existing_variants)
                    elif any(variant.option1_value == "Default Title" and (variant.inventory_qty or 0) == 0 for variant in existing_variants):
                        inventory_changed = True

                    if not any((price_changed, status_changed, title_changed, inventory_changed)):
                        print("  -> No change.")
                        continue

                    if dry_run:
                        print(
                            "  -> WOULD UPDATE"
                            f" title={product.last_title}->{new_title}"
                            f" price={product.last_price}->{new_price}"
                            f" status={product.last_status}->{new_status}"
                        )
                    else:
                        if new_title:
                            product.last_title = new_title
                        if new_price is not None:
                            product.last_price = new_price
                        product.last_status = new_status
                        product.updated_at = utc_now()

                        for variant in existing_variants:
                            if new_price is not None and (variant.option1_value == "Default Title" or len(existing_variants) == 1):
                                variant.price = new_price
                            if new_status in {"sold", "deleted"}:
                                variant.inventory_qty = 0
                            elif variant.option1_value == "Default Title":
                                variant.inventory_qty = variant.inventory_qty or 1

                        snapshot = ProductSnapshot(
                            product_id=product.id,
                            scraped_at=utc_now(),
                            title=new_title,
                            price=new_price,
                            status=new_status,
                            description=item.get("description") or "",
                            image_urls="|".join(item.get("image_urls") or []),
                        )
                        session_db.add(snapshot)
                        updated_count += 1

                        if price_changed and product.pricing_rule_id:
                            repricing_product_ids.add(product.id)

                    time.sleep(2)

                except Exception as exc:
                    print(f"  -> Error: {exc}")
                    traceback.print_exc()

            if dry_run:
                session_db.rollback()
                print(
                    f"Dry-run finished. Would update: {updated_count}, "
                    f"skipped_uncertain: {skipped_uncertain}"
                )
                return

            session_db.commit()
            for product_id in repricing_product_ids:
                update_product_selling_price(product_id)
            print(f"Finished. Total updated: {updated_count}, skipped_uncertain: {skipped_uncertain}")

        finally:
            session_db.close()

    @app.cli.command("worker-health")
    @click.option("--fail-on-warning", is_flag=True, default=False)
    def worker_health(fail_on_warning):
        """Print worker/RQ health snapshot as JSON for local diagnostics."""
        snapshot = get_worker_health_snapshot(app)
        _emit_json(snapshot)

        if snapshot.get("queue_backend") == "rq" and snapshot.get("redis_ok") is False:
            raise SystemExit(1)
        if fail_on_warning and snapshot.get("backlog_issues"):
            raise SystemExit(1)

    @app.cli.command("process-selector-repairs")
    @click.option("--limit", type=int, default=10, show_default=True)
    @click.option("--candidate-id", type=int, default=None, help="Restrict processing to one pending candidate id.")
    @click.option("--apply/--dry-run", default=False, show_default=True)
    def process_selector_repairs(limit, candidate_id, apply):
        """Validate pending selector repair candidates and optionally promote them."""
        snapshot = run_selector_repair_cycle(
            app,
            limit=limit,
            candidate_id=candidate_id,
            apply=apply,
        )
        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)

    @app.cli.command("predeploy-check")
    @click.option(
        "--target",
        type=click.Choice(["single-web", "split-render"]),
        default="single-web",
        show_default=True,
    )
    @click.option("--strict", is_flag=True, default=False)
    def predeploy_check(target, strict):
        """Print local predeploy readiness checks as JSON."""
        snapshot = build_predeploy_snapshot(app, target=target)
        schema_drift = inspect_additive_schema_drift()
        snapshot = dict(snapshot)
        snapshot["schema_drift"] = {
            "ready": bool(schema_drift.get("ready", not schema_drift.get("blockers"))),
            "blockers": list(schema_drift.get("blockers") or []),
            "missing_tables": list(schema_drift.get("missing_tables") or []),
            "missing_columns": list(schema_drift.get("missing_columns") or []),
            "database_backend": schema_drift.get("database_backend"),
        }
        if schema_drift.get("blockers"):
            snapshot["blockers"] = list(snapshot.get("blockers") or []) + list(schema_drift.get("blockers") or [])
            snapshot["ready"] = not snapshot["blockers"]
        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)
        if strict and snapshot.get("warnings"):
            raise SystemExit(1)

    @app.cli.command("db-smoke")
    @click.option(
        "--require-backend",
        type=click.Choice(["sqlite", "postgresql", "mysql"]),
        default=None,
    )
    @click.option("--apply-migrations", is_flag=True, default=False)
    def db_smoke(require_backend, apply_migrations):
        """Run an explicit database smoke check against the configured DATABASE_URL."""
        snapshot = run_database_smoke_check(
            require_backend=require_backend,
            apply_migrations=apply_migrations,
            schema_mode=app.config.get("SCHEMA_BOOTSTRAP_MODE", "auto"),
        )
        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)

    @app.cli.command("rich-text-maintenance")
    @click.option("--apply", is_flag=True, default=False, help="Persist normalized rich-text back into the database.")
    @click.option("--user-id", type=int, default=None, help="Optionally restrict Product/PriceList/Snapshot rows to one user.")
    @click.option("--include-snapshots/--skip-snapshots", default=True, show_default=True)
    def rich_text_maintenance(apply, user_id, include_snapshots):
        """Normalize existing rich-text fields in the configured database."""
        snapshot = run_rich_text_maintenance(
            apply=apply,
            user_id=user_id,
            include_snapshots=include_snapshots,
        )
        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)

    @app.cli.command("existing-web-db-migrate")
    @click.option(
        "--source-url",
        type=str,
        default=DEFAULT_EXISTING_WEB_SQLITE_URL,
        show_default=True,
        help="Existing Render Web Service SQLite DATABASE_URL.",
    )
    @click.option("--destination-url", type=str, required=True, help="Destination Postgres DATABASE_URL.")
    @click.option("--prepare-destination-schema", is_flag=True, default=False)
    @click.option("--dry-run", is_flag=True, default=False)
    @click.option("--verify-only", is_flag=True, default=False)
    @click.option("--batch-size", type=int, default=DEFAULT_MIGRATION_BATCH_SIZE, show_default=True)
    @click.option("--table", "table_names", multiple=True, type=str)
    def existing_web_db_migrate(
        source_url,
        destination_url,
        prepare_destination_schema,
        dry_run,
        verify_only,
        batch_size,
        table_names,
    ):
        """Migrate or verify the existing Render Web SQLite database into Postgres."""
        prepared_destination_schema = None
        schema_prepare_error = None
        if prepare_destination_schema:
            try:
                run_alembic_upgrade_for_database_url(destination_url)
                prepared_destination_schema = "alembic"
            except Exception as exc:
                schema_prepare_error = str(exc)

        if schema_prepare_error:
            snapshot = {
                "ready": False,
                "mode": "verify-only" if verify_only else "dry-run" if dry_run else "migrate",
                "source_url": redact_database_url(source_url),
                "destination_url": redact_database_url(destination_url),
                "prepared_destination_schema": prepared_destination_schema,
                "blockers": ["destination_schema_prepare_failed"],
                "warnings": [],
                "migration_error": schema_prepare_error,
            }
        else:
            snapshot = run_existing_web_database_migration(
                source_url=source_url,
                destination_url=destination_url,
                dry_run=dry_run,
                verify_only=verify_only,
                batch_size=batch_size,
                table_names=table_names,
            )
            snapshot["prepared_destination_schema"] = prepared_destination_schema

        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)

    @app.cli.command("schema-drift-check")
    def schema_drift_check():
        """Inspect additive schema drift on the configured DATABASE_URL."""
        snapshot = inspect_additive_schema_drift()
        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)

    @app.cli.command("detail-fixture-smoke")
    @click.option(
        "--site",
        "fixture_site",
        type=click.Choice(["mercari", "snkrdunk"]),
        required=True,
    )
    @click.option("--fixture-path", type=click.Path(dir_okay=False, path_type=str), required=True)
    @click.option("--target-url", type=str, default=None)
    @click.option("--strict", is_flag=True, default=False)
    def detail_fixture_smoke(fixture_site, fixture_path, target_url, strict):
        """Parse a local detail-page HTML fixture and print the extraction summary as JSON."""
        snapshot = run_detail_fixture_smoke(
            fixture_site,
            fixture_path,
            target_url=str(target_url or ""),
            strict=strict,
        )
        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)

    @app.cli.command("search-fixture-smoke")
    @click.option(
        "--site",
        "fixture_site",
        type=click.Choice(["mercari"]),
        required=True,
    )
    @click.option("--fixture-path", type=click.Path(dir_okay=False, path_type=str), required=True)
    @click.option("--target-url", type=str, default=None)
    @click.option("--strict", is_flag=True, default=False)
    def search_fixture_smoke(fixture_site, fixture_path, target_url, strict):
        """Parse a local search-result HTML fixture and print the extraction summary as JSON."""
        snapshot = run_search_fixture_smoke(
            fixture_site,
            fixture_path,
            target_url=str(target_url or ""),
            strict=strict,
        )
        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)

    @app.cli.command("local-verify")
    @click.option(
        "--profile",
        type=click.Choice(["parser", "stack", "full"]),
        default="full",
        show_default=True,
    )
    @click.option(
        "--require-backend",
        type=click.Choice(["sqlite", "postgresql", "mysql"]),
        default="postgresql",
        show_default=True,
    )
    @click.option("--apply-migrations/--no-apply-migrations", default=True, show_default=True)
    @click.option("--strict-parser", is_flag=True, default=False)
    def local_verify(profile, require_backend, apply_migrations, strict_parser):
        """Run the ordered local-first verification suite."""
        from app import create_web_app

        verification_app = app if app.config.get("TESTING") else create_web_app()
        snapshot = run_local_verification_suite(
            verification_app,
            profile=profile,
            require_backend=require_backend,
            apply_migrations=apply_migrations,
            strict_parser=strict_parser,
        )
        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)

    @app.cli.command("render-cutover-readiness")
    @click.option(
        "--require-backend",
        type=click.Choice(["sqlite", "postgresql", "mysql"]),
        default="postgresql",
        show_default=True,
    )
    @click.option("--apply-migrations/--no-apply-migrations", default=True, show_default=True)
    @click.option("--strict", is_flag=True, default=False)
    @click.option("--strict-parser", is_flag=True, default=False)
    def render_cutover_readiness(require_backend, apply_migrations, strict, strict_parser):
        """Run the local gate required before the first paid Render split activation."""
        snapshot = run_render_cutover_readiness(
            app,
            require_backend=require_backend,
            apply_migrations=apply_migrations,
            strict=strict,
            strict_parser=strict_parser,
        )
        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)

    @app.cli.command("render-local-split-readiness")
    @click.option("--blueprint-path", type=click.Path(dir_okay=False, path_type=str), default="render.yaml")
    @click.option("--compose-path", type=click.Path(dir_okay=False, path_type=str), default="docker-compose.local.yml")
    @click.option(
        "--require-backend",
        type=click.Choice(["sqlite", "postgresql", "mysql"]),
        default="postgresql",
        show_default=True,
    )
    @click.option("--apply-migrations/--no-apply-migrations", default=True, show_default=True)
    @click.option("--strict/--no-strict", default=True, show_default=True)
    @click.option("--strict-parser", is_flag=True, default=False)
    def render_local_split_readiness(blueprint_path, compose_path, require_backend, apply_migrations, strict, strict_parser):
        """Run the paid-split local rehearsal gate with repo-pinned local env defaults."""
        snapshot = run_render_local_split_readiness(
            app,
            blueprint_path=blueprint_path,
            compose_path=compose_path,
            require_backend=require_backend,
            apply_migrations=apply_migrations,
            strict=strict,
            strict_parser=strict_parser,
        )
        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)

    @app.cli.command("render-cutover-brief")
    @click.option("--blueprint-path", type=click.Path(dir_okay=False, path_type=str), default="render.yaml")
    @click.option("--compose-path", type=click.Path(dir_okay=False, path_type=str), default="docker-compose.local.yml")
    @click.option("--base-url", type=str, default=None)
    @click.option("--username", type=str, default=None)
    @click.option("--password", type=str, default=None)
    def render_cutover_brief(blueprint_path, compose_path, base_url, username, password):
        """Print the pre-activation operator bundle for the first paid Render cutover."""
        snapshot = run_render_cutover_brief(
            app,
            blueprint_path=blueprint_path,
            compose_path=compose_path,
            base_url=base_url,
            username=username,
            password=password,
        )
        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)

    @app.cli.command("render-blueprint-audit")
    @click.option("--blueprint-path", type=click.Path(dir_okay=False, path_type=str), default="render.yaml")
    @click.option("--strict", is_flag=True, default=False)
    def render_blueprint_audit(blueprint_path, strict):
        """Audit the dormant Render Blueprint for C4 readiness."""
        snapshot = run_render_blueprint_audit(blueprint_path)
        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)
        if strict and snapshot.get("warnings"):
            raise SystemExit(1)

    @app.cli.command("render-budget-guardrail-audit")
    @click.option("--blueprint-path", type=click.Path(dir_okay=False, path_type=str), default="render.yaml")
    @click.option("--strict", is_flag=True, default=False)
    def render_budget_guardrail_audit(blueprint_path, strict):
        """Audit the dormant Render Blueprint against the repo budget guardrail assumptions."""
        snapshot = run_render_budget_guardrail_audit(blueprint_path)
        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)
        if strict and snapshot.get("warnings"):
            raise SystemExit(1)

    @app.cli.command("render-local-split-checklist")
    @click.option("--blueprint-path", type=click.Path(dir_okay=False, path_type=str), default="render.yaml")
    @click.option("--compose-path", type=click.Path(dir_okay=False, path_type=str), default="docker-compose.local.yml")
    def render_local_split_checklist(blueprint_path, compose_path):
        """Print the local PostgreSQL/Redis/RQ rehearsal contract for the first paid Render split."""
        snapshot = run_render_local_split_checklist(
            app,
            blueprint_path=blueprint_path,
            compose_path=compose_path,
        )
        _emit_json(snapshot)

    @app.cli.command("render-dashboard-inputs")
    @click.option("--blueprint-path", type=click.Path(dir_okay=False, path_type=str), default="render.yaml")
    def render_dashboard_inputs(blueprint_path):
        """Print the manual, managed, and fixed Render env inputs derived from the Blueprint."""
        snapshot = run_render_dashboard_inputs(blueprint_path)
        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)

    @app.cli.command("render-postdeploy-smoke")
    @click.option("--base-url", required=True, type=str)
    @click.option("--timeout-seconds", default=15.0, show_default=True, type=float)
    @click.option("--retries", default=2, show_default=True, type=int)
    @click.option("--retry-delay-seconds", default=1.0, show_default=True, type=float)
    @click.option("--expect-queue-backend", default="rq", show_default=True, type=str)
    @click.option("--expect-runtime-role", default="web", show_default=True, type=str)
    @click.option(
        "--expect-scheduler-mode",
        type=click.Choice(["disabled", "enabled", "any"]),
        default="disabled",
        show_default=True,
    )
    @click.option("--username", type=str, default=None, help="Optional login username for authenticated route smoke.")
    @click.option("--password", type=str, default=None, help="Optional login password for authenticated route smoke.")
    @click.option("--ensure-user", is_flag=True, default=False, help="Register the smoke user if login does not succeed.")
    def render_postdeploy_smoke(
        base_url,
        timeout_seconds,
        retries,
        retry_delay_seconds,
        expect_queue_backend,
        expect_runtime_role,
        expect_scheduler_mode,
        username,
        password,
        ensure_user,
    ):
        """Run the first post-deploy smoke checks against a live web URL."""
        snapshot = run_render_postdeploy_smoke(
            base_url,
            timeout_seconds=timeout_seconds,
            retries=retries,
            retry_delay_seconds=retry_delay_seconds,
            expect_queue_backend=expect_queue_backend,
            expect_runtime_role=expect_runtime_role,
            expect_scheduler_mode=expect_scheduler_mode,
            username=username,
            password=password,
            ensure_user=ensure_user,
        )
        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)

    @app.cli.command("single-web-postdeploy-smoke")
    @click.option("--base-url", type=str, required=True)
    @click.option("--timeout-seconds", type=float, default=15.0, show_default=True)
    @click.option("--retries", default=2, show_default=True, type=int)
    @click.option("--retry-delay-seconds", default=1.0, show_default=True, type=float)
    @click.option("--username", type=str, default=None, help="Optional login username for authenticated route smoke.")
    @click.option("--password", type=str, default=None, help="Optional login password for authenticated route smoke.")
    @click.option("--ensure-user", is_flag=True, default=False, help="Register the smoke user if login does not succeed.")
    def single_web_postdeploy_smoke(base_url, timeout_seconds, retries, retry_delay_seconds, username, password, ensure_user):
        """Run post-deploy smoke checks against the current single-web production shape."""
        snapshot = run_single_web_postdeploy_smoke(
            base_url,
            timeout_seconds=timeout_seconds,
            retries=retries,
            retry_delay_seconds=retry_delay_seconds,
            username=username,
            password=password,
            ensure_user=ensure_user,
        )
        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)

    @app.cli.command("render-worker-postdeploy-checklist")
    @click.option("--blueprint-path", type=click.Path(dir_okay=False, path_type=str), default="render.yaml")
    def render_worker_postdeploy_checklist(blueprint_path):
        """Print the expected worker log markers and runtime contract for paid Render activation."""
        snapshot = run_render_worker_postdeploy_checklist(blueprint_path=blueprint_path)
        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)

    @app.cli.command("render-cutover-checklist")
    @click.option("--blueprint-path", type=click.Path(dir_okay=False, path_type=str), default="render.yaml")
    @click.option("--base-url", type=str, default=None)
    @click.option("--username", type=str, default=None)
    @click.option("--password", type=str, default=None)
    def render_cutover_checklist(blueprint_path, base_url, username, password):
        """Print the ordered commands and manual steps for the first paid Render cutover."""
        snapshot = run_render_cutover_checklist(
            blueprint_path=blueprint_path,
            base_url=base_url,
            username=username,
            password=password,
        )
        _emit_json(snapshot)

    @app.cli.command("single-web-redeploy-checklist")
    @click.option("--base-url", type=str, default=None)
    @click.option("--username", type=str, default=None)
    @click.option("--password", type=str, default=None)
    def single_web_redeploy_checklist(base_url, username, password):
        """Print the ordered commands and manual steps for a current single-web redeploy."""
        snapshot = run_single_web_redeploy_checklist(
            base_url=base_url,
            username=username,
            password=password,
        )
        _emit_json(snapshot)

    @app.cli.command("single-web-redeploy-readiness")
    @click.option("--strict-parser", is_flag=True, default=False)
    def single_web_redeploy_readiness(strict_parser):
        """Run the local gate required before redeploying the current single-web production shape."""
        snapshot = run_single_web_redeploy_readiness(
            app,
            strict_parser=strict_parser,
        )
        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)

    @app.cli.command("single-web-smoke")
    @click.option(
        "--mode",
        type=click.Choice(["preview", "persist"]),
        default="preview",
        show_default=True,
    )
    @click.option("--keep-artifacts", is_flag=True, default=False)
    @click.option(
        "--fixture-site",
        type=click.Choice(["mercari", "snkrdunk"]),
        default=None,
        help="Use a local detail HTML fixture parsed through the real site parser.",
    )
    @click.option("--fixture-path", type=click.Path(dir_okay=False, path_type=str), default=None)
    @click.option(
        "--fixture-target-url",
        type=str,
        default=None,
        help="Optional canonical URL to associate with the fixture item.",
    )
    def single_web_smoke(mode, keep_artifacts, fixture_site, fixture_path, fixture_target_url):
        """Run current single-web + in-memory queue compatibility smoke without Redis/worker."""
        snapshot = run_single_web_smoke(
            app,
            mode=mode,
            keep_artifacts=keep_artifacts,
            fixture_site=fixture_site,
            fixture_path=fixture_path,
            fixture_target_url=fixture_target_url,
        )
        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)

    @app.cli.command("stack-smoke")
    @click.option(
        "--require-backend",
        type=click.Choice(["sqlite", "postgresql", "mysql"]),
        default="postgresql",
        show_default=True,
    )
    @click.option("--apply-migrations/--no-apply-migrations", default=True, show_default=True)
    @click.option(
        "--mode",
        type=click.Choice(["preview", "persist"]),
        default="preview",
        show_default=True,
    )
    @click.option("--keep-artifacts", is_flag=True, default=False)
    @click.option(
        "--fixture-site",
        type=click.Choice(["mercari", "snkrdunk"]),
        default=None,
        help="Use a local HTML fixture parsed through the real site parser.",
    )
    @click.option("--fixture-path", type=click.Path(dir_okay=False, path_type=str), default=None)
    @click.option(
        "--fixture-target-url",
        type=str,
        default=None,
        help="Optional canonical URL to associate with the fixture item.",
    )
    def stack_smoke(
        require_backend,
        apply_migrations,
        mode,
        keep_artifacts,
        fixture_site,
        fixture_path,
        fixture_target_url,
    ):
        """Run local RQ + worker + DB + status/result smoke without touching live sites."""
        snapshot = run_stack_smoke(
            app,
            require_backend=require_backend,
            apply_migrations=apply_migrations,
            mode=mode,
            keep_artifacts=keep_artifacts,
            fixture_site=fixture_site,
            fixture_path=fixture_path,
            fixture_target_url=fixture_target_url,
        )
        _emit_json(snapshot)

        if snapshot.get("blockers"):
            raise SystemExit(1)
