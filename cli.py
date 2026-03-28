"""
CLI commands for the application.
"""
import html
import json
import os
import time
import traceback
import uuid
from pathlib import Path

import click
from redis import Redis

import offmall_db
import rakuma_db
import snkrdunk_db
import surugaya_db
import yahoo_db
import yahuoku_db
from database import SessionLocal, describe_schema_bootstrap, run_database_smoke_check
from mercari_db import scrape_single_item as scrape_mercari_single_item
from models import Product, ProductSnapshot, Variant
from services.html_page_adapter import HtmlPageAdapter
from services.mercari_item_parser import parse_mercari_item_page
from services.pricing_service import update_product_selling_price
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

    for service_name, env_map in (("esp-web", web_env), ("esp-worker", worker_env)):
        secret_env = env_map.get("SECRET_KEY", {})
        if str(secret_env.get("sync", "")).lower() != "false":
            blockers.append(f"secret_key_must_be_manual:{service_name}")
        if env_map.get("DATABASE_URL", {}).get("source_type") != "fromDatabase":
            blockers.append(f"database_url_must_be_managed:{service_name}")
        if env_map.get("REDIS_URL", {}).get("source_type") != "fromService":
            blockers.append(f"redis_url_must_be_managed:{service_name}")

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

    current_web_app = app if app.config.get("TESTING") else create_web_app()
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

    @app.cli.command("render-dashboard-inputs")
    @click.option("--blueprint-path", type=click.Path(dir_okay=False, path_type=str), default="render.yaml")
    def render_dashboard_inputs(blueprint_path):
        """Print the manual, managed, and fixed Render env inputs derived from the Blueprint."""
        snapshot = run_render_dashboard_inputs(blueprint_path)
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
