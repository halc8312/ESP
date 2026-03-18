import argparse
import importlib
import json
import os
import platform
import subprocess
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, UTC
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


SITE_MODULES = {
    "mercari": "mercari_db",
    "rakuma": "rakuma_db",
    "snkrdunk": "snkrdunk_db",
}

DETAIL_ENV_PREFIX = {
    "mercari": "MERCARI",
    "rakuma": "RAKUMA",
    "snkrdunk": "SNKRDUNK",
}


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


@contextmanager
def _temporary_env(overrides: dict[str, str | None]):
    previous = {}
    for key, value in overrides.items():
        previous[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def _detail_timing_probe(module):
    metrics = {
        "detail_expansion_seconds": 0.0,
        "detail_expansion_calls": 0,
    }
    original = getattr(module, "gather_with_concurrency", None)
    if original is None:
        yield metrics
        return

    async def _wrapped(*args, **kwargs):
        started = time.perf_counter()
        try:
            return await original(*args, **kwargs)
        finally:
            metrics["detail_expansion_seconds"] += time.perf_counter() - started
            metrics["detail_expansion_calls"] += 1

    setattr(module, "gather_with_concurrency", _wrapped)
    try:
        yield metrics
    finally:
        setattr(module, "gather_with_concurrency", original)


def _build_env_overrides(args) -> dict[str, str | None]:
    overrides = {}
    prefix = DETAIL_ENV_PREFIX[args.site]
    overrides[f"{prefix}_DETAIL_CONCURRENCY"] = str(args.detail_concurrency)

    if args.site == "mercari":
        overrides["MERCARI_CAPTURE_NETWORK_PAYLOAD"] = _bool_text(args.mercari_capture)
        overrides["MERCARI_USE_NETWORK_PAYLOAD"] = _bool_text(args.mercari_use_payload)
    return overrides


def _git_sha() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return str(completed.stdout or "").strip()
    except Exception:
        return "unknown"


def _aggregate_field_sources(items: list[dict]) -> dict[str, dict[str, int]]:
    counters = defaultdict(Counter)
    for item in items:
        meta = item.get("_scrape_meta") or {}
        for field, source in (meta.get("field_sources") or {}).items():
            if source:
                counters[field][source] += 1
    return {field: dict(counter) for field, counter in counters.items()}


def _aggregate_strategies(items: list[dict]) -> dict[str, int]:
    counter = Counter()
    for item in items:
        strategy = str((item.get("_scrape_meta") or {}).get("strategy") or "").strip()
        if strategy:
            counter[strategy] += 1
    return dict(counter)


def _aggregate_mercari_network_stats(items: list[dict]) -> dict:
    mismatch_fields = Counter()
    dom_fallback_fields = Counter()
    stats = Counter()

    for item in items:
        meta = item.get("_scrape_meta") or {}
        capture = meta.get("network_capture") or {}
        compare = meta.get("shadow_compare") or {}
        field_sources = meta.get("field_sources") or {}

        if capture.get("enabled"):
            stats["capture_enabled_items"] += 1
        if capture.get("captured"):
            stats["capture_success_items"] += 1
        if capture.get("used_payload"):
            stats["payload_used_items"] += 1
        if capture.get("capture_error"):
            stats["capture_error_items"] += 1
        if meta.get("fallback_mode") == "payload_without_dom":
            stats["payload_rescue_items"] += 1
        if compare.get("mismatch_fields"):
            stats["items_with_shadow_mismatch"] += 1
            for field in compare["mismatch_fields"]:
                mismatch_fields[field] += 1
        if capture.get("used_payload"):
            for field, source in field_sources.items():
                if source == "dom":
                    dom_fallback_fields[field] += 1

    return {
        **dict(stats),
        "shadow_mismatch_fields": dict(mismatch_fields),
        "dom_fallback_fields": dict(dom_fallback_fields),
    }


def _run_single_probe(args) -> dict:
    module = importlib.import_module(SITE_MODULES[args.site])
    env_overrides = _build_env_overrides(args)

    with _temporary_env(env_overrides):
        with _detail_timing_probe(module) as detail_metrics:
            started = time.perf_counter()
            items = module.scrape_search_result(
                args.search_url,
                max_items=args.max_items,
                max_scroll=args.max_scroll,
            )
            total_seconds = time.perf_counter() - started

    success_count = sum(1 for item in items if item.get("title") and item.get("status") != "error")
    failure_count = max(0, args.max_items - success_count)

    result = {
        "probe_metadata": {
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "read_only_probe": True,
            "site": args.site,
            "label": args.label or f"{args.site}-probe",
            "search_url": args.search_url,
            "env_label": args.env_label,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "git_sha": _git_sha(),
            "flags": {
                "mercari_capture": args.mercari_capture,
                "mercari_use_payload": args.mercari_use_payload,
            },
            "env_overrides": sorted(key for key, value in env_overrides.items() if value is not None),
        },
        "label": args.label or f"{args.site}-probe",
        "site": args.site,
        "search_url": args.search_url,
        "max_items": args.max_items,
        "max_scroll": args.max_scroll,
        "detail_concurrency": args.detail_concurrency,
        "total_duration_seconds": round(total_seconds, 3),
        "detail_expansion_seconds": round(detail_metrics["detail_expansion_seconds"], 3),
        "detail_expansion_calls": detail_metrics["detail_expansion_calls"],
        "item_count": len(items),
        "success_count": success_count,
        "failure_count": failure_count,
        "items_per_minute": round((len(items) / total_seconds) * 60, 2) if total_seconds > 0 else 0.0,
        "strategy_counts": _aggregate_strategies(items),
        "field_source_counts": _aggregate_field_sources(items),
    }

    if args.site == "mercari":
        result["mercari_flags"] = {
            "capture": args.mercari_capture or args.mercari_use_payload,
            "use_payload": args.mercari_use_payload,
        }
        result["mercari_network"] = _aggregate_mercari_network_stats(items)

    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Low-cost KPI probe for Arc 1 scrape throughput and extraction rollout."
    )
    parser.add_argument("--site", choices=sorted(SITE_MODULES), required=True)
    parser.add_argument("--search-url", required=True)
    parser.add_argument("--max-items", type=int, default=5)
    parser.add_argument("--max-scroll", type=int, default=3)
    parser.add_argument("--detail-concurrency", type=int, default=1)
    parser.add_argument("--label", default="")
    parser.add_argument("--env-label", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--mercari-capture", action="store_true")
    parser.add_argument("--mercari-use-payload", action="store_true")
    args = parser.parse_args()

    result = _run_single_probe(args)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
