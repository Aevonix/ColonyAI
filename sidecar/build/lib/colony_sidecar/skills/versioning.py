"""Colony Skills — semantic versioning and schema compatibility checking."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(-[a-zA-Z0-9.]+)?$")


@dataclass
class CompatibilityResult:
    compatible: bool
    breaking: bool
    changes: List[str]
    recommended_bump: str   # "major" | "minor" | "patch"


def check_schema_compatibility(
    old_schema: Dict[str, Any],
    new_schema: Dict[str, Any],
) -> CompatibilityResult:
    """Compare JSON Schema dicts to determine version bump requirement.

    Breaking changes (require MAJOR bump):
      - Required property removed
      - Required property type changed
      - additionalProperties changed from True to False

    Non-breaking additions (require MINOR bump):
      - New optional property added

    Patch-only:
      - description or examples updated only
    """
    old_props = old_schema.get("properties", {})
    new_props = new_schema.get("properties", {})
    old_req = set(old_schema.get("required", []))
    new_req = set(new_schema.get("required", []))

    changes: List[str] = []
    breaking = False

    # Removed required properties
    removed_required = old_req - new_req
    for name in removed_required:
        if name not in new_props:
            changes.append(f"Required property '{name}' removed.")
            breaking = True

    # Type changes in existing required properties
    for name in old_req & new_req:
        old_type = old_props.get(name, {}).get("type")
        new_type = new_props.get(name, {}).get("type")
        if old_type != new_type:
            changes.append(f"Type of '{name}' changed from {old_type} to {new_type}.")
            breaking = True

    # additionalProperties strictness change
    if old_schema.get("additionalProperties", True) is True and \
       new_schema.get("additionalProperties", True) is False:
        changes.append("additionalProperties changed from True to False.")
        breaking = True

    # New optional properties
    new_optional = set(new_props.keys()) - set(old_props.keys()) - new_req
    if new_optional:
        changes.append(f"New optional properties: {sorted(new_optional)}")

    if breaking:
        bump = "major"
    elif new_optional or (set(new_props.keys()) - set(old_props.keys())):
        bump = "minor"
    else:
        bump = "patch"

    return CompatibilityResult(
        compatible=not breaking,
        breaking=breaking,
        changes=changes,
        recommended_bump=bump,
    )


def bump_version(current: str, bump: str) -> str:
    """Increment a semantic version string.

    Args:
        current: Semantic version string like "1.2.3".
        bump:    One of "major", "minor", "patch".

    Returns:
        New version string.

    Raises:
        ValueError: If current is not a valid semver.
    """
    m = _VERSION_RE.match(current)
    if not m:
        raise ValueError(f"Invalid version string: {current!r}")
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if bump == "major":
        return f"{major + 1}.0.0"
    elif bump == "minor":
        return f"{major}.{minor + 1}.0"
    elif bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    else:
        raise ValueError(f"Unknown bump type: {bump!r}")


def is_valid_version(version: str) -> bool:
    """Return True if the version string matches MAJOR.MINOR.PATCH."""
    return bool(_VERSION_RE.match(version))
