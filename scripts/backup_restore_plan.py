#!/usr/bin/env python3
"""Inspect local backup artifacts only; never connect, extract, or restore.

The manifest proves file identity and a restricted media archive structure, not
database restorability or cross-artifact consistency. It contains no filenames,
database URLs, environment values, archive member names, or customer data.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import tarfile
import zlib


class InvalidBackup(ValueError):
    """The message is an allowlisted code, never an input path or payload."""


def _digest(path: Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return {"bytes": size, "sha256": digest.hexdigest()}


def _media_summary(path: Path) -> dict:
    files = total_bytes = members = 0
    seen = {}
    required_dirs = set()
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            members += 1
            if members > 100_000:
                raise InvalidBackup("media_member_limit")
            name = member.name.rstrip("/")
            parts = name.split("/")
            if (
                not name
                or parts[0] != "images"
                or any(part in {"", ".", ".."} for part in parts)
                or "\\" in name
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or name in seen
                or not (member.isdir() or member.isfile())
            ):
                raise InvalidBackup("unsafe_media_archive")
            if any(seen.get("/".join(parts[:index])) == "file" for index in range(1, len(parts))):
                raise InvalidBackup("unsafe_media_archive")
            if member.isfile() and name in required_dirs:
                raise InvalidBackup("unsafe_media_archive")
            seen[name] = "directory" if member.isdir() else "file"
            required_dirs.update("/".join(parts[:index]) for index in range(1, len(parts)))
            if member.isfile():
                # Files must live below images/, not replace the root directory.
                if len(parts) < 2 or member.size < 0:
                    raise InvalidBackup("unsafe_media_archive")
                files += 1
                total_bytes += member.size
                if total_bytes > 100 * 1024**3:
                    raise InvalidBackup("media_size_limit")
                source = archive.extractfile(member)
                if source is None:
                    raise InvalidBackup("invalid_media_archive")
                with source:
                    remaining = member.size
                    while remaining:
                        block = source.read(min(1024 * 1024, remaining))
                        if not block:
                            raise InvalidBackup("invalid_media_archive")
                        remaining -= len(block)
    if members == 0:
        raise InvalidBackup("empty_media_archive")
    return {"files": files, "uncompressed_bytes": total_bytes}


def inspect_backup(dump: Path, media: Path, revision: str) -> dict:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
        raise InvalidBackup("invalid_revision")
    with dump.open("rb") as source:
        if source.read(5) != b"PGDMP":
            raise InvalidBackup("not_postgresql_custom_dump")
    media_summary = _media_summary(media)
    return {
        "manifest_version": 1,
        "mode": "offline_inspection",
        "inspected_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_revision": revision.lower(),
        "expected_postgresql_major": 18,
        "database": {**_digest(dump), "format_check": "custom_header_only"},
        "media": {**_digest(media), **media_summary, "structure_check": "relative_regular_files_only"},
        "database_restore_test": "not_performed",
        "cross_artifact_consistency": "not_verified",
        "network_access": False,
        "restore_executed": False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", type=Path, required=True)
    parser.add_argument("--media", type=Path, required=True)
    parser.add_argument("--revision", required=True, help="Full source Git commit SHA")
    args = parser.parse_args(argv)
    try:
        manifest = inspect_backup(args.dump, args.media, args.revision)
    except InvalidBackup as exc:
        print(json.dumps({"status": "error", "code": str(exc)}))
        return 2
    except (OSError, tarfile.TarError, EOFError, zlib.error):
        # OS/archive errors can include private paths or member contents.
        print(json.dumps({"status": "error", "code": "artifact_read_failed"}))
        return 2
    print(json.dumps(manifest, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
