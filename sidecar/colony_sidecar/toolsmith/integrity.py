"""Canonical digests for Toolsmith artifacts and comparison captures.

The helpers in this module deliberately accept JSON-shaped values only.  A
Toolsmith artifact or shadow input must have one stable byte representation so
that audit records cannot be rebound to different code or data later.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


MAX_CAPTURE_BYTES = 32 * 1024
PURE_CAPABILITY_MANIFEST = {
    "version": "toolsmith.capability-manifest.v1",
    "entrypoint": "run",
    "effects": "none",
    "filesystem": "none",
    "network": "none",
    "subprocess": False,
    "environment": False,
    "deterministic": True,
}


class IntegrityError(ValueError):
    """A value cannot be represented by the bounded canonical contract."""


def canonical_json(value: Any, *, max_bytes: int | None = None) -> str:
    """Return deterministic JSON, rejecting non-JSON and oversized values."""

    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise IntegrityError("value must be finite JSON") from exc
    size = len(rendered.encode("utf-8"))
    if max_bytes is not None and size > max_bytes:
        raise IntegrityError(f"canonical value exceeds {max_bytes} bytes")
    return rendered


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def digest_json(value: Any, *, max_bytes: int | None = None) -> str:
    return digest_text(canonical_json(value, max_bytes=max_bytes))


def candidate_digest(candidate: Any) -> str:
    """Bind a draft to the exact mined/requested evidence it came from."""

    payload = {
        "signature": str(getattr(candidate, "signature", "") or ""),
        "domain": str(getattr(candidate, "domain", "") or ""),
        "description": str(getattr(candidate, "description", "") or ""),
        "occurrences": int(getattr(candidate, "occurrences", 0) or 0),
        "evidence": list(getattr(candidate, "evidence", []) or []),
        "sample_descriptions": list(
            getattr(candidate, "sample_descriptions", []) or []
        ),
    }
    return digest_json(payload)


def artifact_digest(
    *,
    name: str,
    description: str,
    source_code: str,
    input_schema: Mapping[str, Any],
    test_source: str,
    origin_kind: str,
    evidence: list[str],
    candidate_digest_value: str,
    capability_manifest: Mapping[str, Any] | None = None,
) -> str:
    """Bind all executable and provenance-bearing fields of one tool."""

    return digest_json({
        "name": name,
        "description": description,
        "source_code": source_code,
        "input_schema": dict(input_schema),
        "test_source": test_source,
        "origin_kind": origin_kind,
        "evidence": list(evidence),
        "candidate_digest": candidate_digest_value,
        "capability_manifest": dict(
            capability_manifest or PURE_CAPABILITY_MANIFEST),
    })
