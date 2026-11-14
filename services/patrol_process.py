"""Minimal executable child entrypoint; does not import worker.py/app boot."""
from __future__ import annotations

import argparse
import logging


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--generation")
    parser.add_argument("--directory")
    parser.add_argument("--clear-generation")
    parser.add_argument("--quarantine-generation")
    parser.add_argument("--sites")
    parser.add_argument("--deadline", type=float)
    parser.add_argument("--result")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if arguments.clear_generation or arguments.quarantine_generation:
        from services.marketplace_access import clear_marketplace_generation_quarantines, get_access_store
        from services.patrol_supervisor import _write_atomic_message
        if arguments.quarantine_generation:
            import time
            for site in arguments.sites.split(","):
                if time.monotonic() >= arguments.deadline:
                    raise TimeoutError("Retirement fence deadline exceeded")
                get_access_store().quarantine(site, arguments.quarantine_generation + ":supervisor")
            result = {"quarantined": True}
        else:
            clear_marketplace_generation_quarantines(
                arguments.clear_generation, sites=arguments.sites.split(","), deadline=arguments.deadline,
            )
            # Chromium cache/profile deletion can itself take time. It stays
            # inside this killable helper, rather than extending parent cleanup.
            from pathlib import Path
            import shutil
            profile = Path(arguments.result).parent / "chrome"
            if profile.exists():
                shutil.rmtree(profile)
            result = {"cleared": True}
        _write_atomic_message(arguments.result, result)
        return 0
    from services.patrol_supervisor import _patrol_batch_child_main
    _patrol_batch_child_main(arguments.parent_pid, arguments.generation, arguments.directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
