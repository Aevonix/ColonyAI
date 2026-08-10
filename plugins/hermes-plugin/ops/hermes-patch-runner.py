#!/usr/bin/env python3
"""Read-only inventory for a legacy Hermes patch registry.

The governed Colony integration targets zero Hermes core patches.  This tool
retains the old filename so deployment checks can inventory and hash any
leftover patch files, but it never executes them and has no apply mode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os


DEFAULT_DIR = os.environ.get(
    "HERMES_PATCH_DIR", os.path.expanduser("~/.hermes/patches")
)


def discover(directory: str) -> list[str]:
    if not os.path.isdir(directory):
        return []
    paths = []
    for name in sorted(os.listdir(directory)):
        if name.startswith(".") or name.endswith(".disabled"):
            continue
        if not (name.endswith("_patch.py") or name.endswith("_patch")):
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            paths.append(path)
    return paths


def inspect(path: str) -> dict[str, object]:
    try:
        with open(path, "rb") as handle:
            content = handle.read(4 * 1024 * 1024 + 1)
    except OSError as error:
        return {"name": os.path.basename(path), "status": "unreadable", "error": str(error)}
    if len(content) > 4 * 1024 * 1024:
        return {"name": os.path.basename(path), "status": "too_large"}
    return {
        "name": os.path.basename(path),
        "path": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "status": "legacy_patch_present",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only inventory of legacy Hermes patch files."
    )
    parser.add_argument("command", choices=["list", "status"])
    parser.add_argument("--dir", default=DEFAULT_DIR)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    rows = [inspect(path) for path in discover(args.dir)]
    value = {
        "schema": "ColonyLegacyHermesPatchInventoryV1",
        "directory": args.dir,
        "zero_patch_ready": not rows,
        "patches": rows,
    }
    if args.json:
        print(json.dumps(value, sort_keys=True, indent=2))
    elif not rows:
        print("No legacy Hermes core patches found.")
    else:
        for row in rows:
            print(f"LEGACY PATCH: {row['name']} {row.get('sha256', row['status'])}")
    return 0 if not rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
