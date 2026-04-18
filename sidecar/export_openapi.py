#!/usr/bin/env python3
"""Export the sidecar's OpenAPI spec to JSON for TypeScript type generation."""

from __future__ import annotations

import json
import sys
import os

# Ensure the sidecar package is importable
sys.path.insert(0, os.path.dirname(__file__))

from colony_sidecar.server import create_app


def main() -> None:
    out_path = os.path.join(os.path.dirname(__file__), "..", "openapi.json")
    app = create_app()
    spec = app.openapi()

    with open(out_path, "w") as f:
        json.dump(spec, f, indent=2)

    n_schemas = len(spec.get("components", {}).get("schemas", {}))
    n_paths = len(spec.get("paths", {}))
    print(f"✅ OpenAPI spec written to {out_path} ({n_schemas} schemas, {n_paths} paths)")


if __name__ == "__main__":
    main()
