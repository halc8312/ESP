"""Run an explicit live search-URL acceptance check for marketplace adapters.

This script is intentionally opt-in: it makes real requests to third-party
marketplaces and is not part of the normal pull-request test suite.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

# Keep direct execution (`python scripts/live_search_acceptance.py`) working:
# Python otherwise places only the scripts directory on the import path.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import mercari_db
import offmall_db
import rakuma_db
import snkrdunk_db
import surugaya_db
import yahoo_db
import yahuoku_db

from services.scrape_request import classify_target_url, get_search_depth
from services.scrape_safety import is_usable_detail_result, validate_marketplace_url


SUPPORTED_SITES = (
    "mercari",
    "rakuma",
    "yahoo",
    "surugaya",
    "offmall",
    "yahuoku",
    "snkrdunk",
)

SEARCH_SCRAPERS: dict[str, Callable[..., list[dict]]] = {
    "mercari": mercari_db.scrape_search_result,
    "rakuma": rakuma_db.scrape_search_result,
    "yahoo": yahoo_db.scrape_search_result,
    "surugaya": surugaya_db.scrape_search_result,
    "offmall": offmall_db.scrape_search_result,
    "yahuoku": yahuoku_db.scrape_search_result,
    "snkrdunk": snkrdunk_db.scrape_search_result,
}


@dataclass(frozen=True)
class AcceptanceResult:
    site: str
    url: str
    status: str
    item_count: int
    sample_title: str | None = None
    error: str | None = None


def _parse_target(raw: str) -> tuple[str, str]:
    site, separator, url = raw.partition("=")
    site = site.strip().lower()
    url = url.strip()
    if not separator or site not in SUPPORTED_SITES or not url:
        supported = ", ".join(SUPPORTED_SITES)
        raise argparse.ArgumentTypeError(
            f"target must be SITE=URL; SITE must be one of: {supported}"
        )
    return site, url


def run_target(site: str, url: str, *, limit: int) -> AcceptanceResult:
    try:
        url_kind, detected_site = classify_target_url(url)
        if url_kind != "search":
            raise ValueError("URL is not classified as a search/listing page")
        if detected_site != site:
            raise ValueError(
                f"URL was classified as {detected_site!r}, expected {site!r}"
            )

        items = SEARCH_SCRAPERS[site](
            search_url=url,
            max_items=limit,
            max_scroll=get_search_depth(site, limit),
            headless=True,
        )
        if not items:
            raise RuntimeError(
                "no products were returned; treat bot blocking and parser drift as failures"
            )

        usable_items = []
        rejected_items = 0
        for item in items:
            if not is_usable_detail_result(item):
                rejected_items += 1
                continue
            try:
                validate_marketplace_url(
                    str(item.get("url") or ""),
                    site,
                    kind="detail",
                )
            except Exception:
                rejected_items += 1
                continue
            usable_items.append(item)
        if rejected_items:
            raise RuntimeError(
                f"{rejected_items} returned product(s) failed status or detail-URL validation"
            )
        if not usable_items:
            raise RuntimeError(
                "products were returned without a usable status, title, and allowlisted detail URL"
            )

        return AcceptanceResult(
            site=site,
            url=url,
            status="passed",
            item_count=len(usable_items),
            sample_title=str(usable_items[0]["title"])[:120],
        )
    except Exception as exc:  # The report must include every requested site.
        return AcceptanceResult(
            site=site,
            url=url,
            status="failed",
            item_count=0,
            error=f"{type(exc).__name__}: {exc}",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run live search-URL acceptance checks. Supply one SITE=URL target "
            "for each marketplace being accepted."
        )
    )
    parser.add_argument(
        "--target",
        action="append",
        required=True,
        type=_parse_target,
        metavar="SITE=URL",
    )
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument(
        "--allow-subset",
        action="store_true",
        help="Allow fewer than all seven supported sites.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    limit = max(1, min(int(args.limit), 10))
    targets = dict(args.target)

    if len(targets) != len(args.target):
        raise SystemExit("each site may be supplied only once")

    missing = [site for site in SUPPORTED_SITES if site not in targets]
    if missing and not args.allow_subset:
        raise SystemExit(
            "all seven sites are required unless --allow-subset is used; missing: "
            + ", ".join(missing)
        )

    results = [
        run_target(site, targets[site], limit=limit)
        for site in SUPPORTED_SITES
        if site in targets
    ]
    payload = {
        "status": "passed" if all(row.status == "passed" for row in results) else "failed",
        "requested_limit": limit,
        "results": [asdict(row) for row in results],
    }
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
