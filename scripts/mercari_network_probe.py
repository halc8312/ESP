import argparse
import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from services.mercari_network_probe import probe_mercari_page


def _default_output_path(page_kind: str) -> Path:
    return ROOT / "output" / "playwright" / f"mercari_{page_kind}_probe.json"


async def _run(args) -> dict:
    return await probe_mercari_page(
        args.url,
        page_kind=args.page_kind,
        max_responses=args.max_responses,
        include_raw_payloads=args.include_raw,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe Mercari live search/detail pages and summarize browser-visible JSON payloads."
    )
    parser.add_argument("--url", required=True)
    parser.add_argument("--page-kind", choices=("detail", "search"), default="detail")
    parser.add_argument("--max-responses", type=int, default=25)
    parser.add_argument("--include-raw", action="store_true")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    result = asyncio.run(_run(args))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)

    output_path = Path(args.output) if args.output else _default_output_path(args.page_kind)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
