"""Versioned deployment contract for the P3 cognition spine.

The feature flags used by the older cognition slices are independent.  That
is useful for canaries, but it also made unsafe combinations representable --
for example a live P3 consuming concerns manufactured by a shadow reducer.
This module turns the flags into one small, inspectable lattice.  It does not
enable any producer and it never widens a requested mode.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Mapping, Optional


_MODES = {"off", "shadow", "live"}


def _mode(value: Any) -> str:
    normalized = str(value or "off").strip().lower()
    return normalized if normalized in _MODES else "off"


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CognitionRuntimeContractV1:
    """Attached/effective mode composition and its immutable revision."""

    requested_mode: str
    effective_mode: str
    workspace_mode: str
    event_concern_mode: str
    drive_governance_mode: str
    charter_revision_id: Optional[str]
    blockers: tuple[str, ...]
    revision: str
    schema: str = "CognitionRuntimeContractV1"
    version: int = 1

    @classmethod
    def compose(
        cls,
        *,
        requested_mode: str,
        workspace_mode: str,
        event_concern_mode: str,
        drive_governance_mode: str,
        charter_revision_id: Optional[str] = None,
        charter_store_attached: bool = False,
        attachment_blockers: tuple[str, ...] = (),
    ) -> "CognitionRuntimeContractV1":
        requested = _mode(requested_mode)
        workspace = _mode(workspace_mode)
        events = _mode(event_concern_mode)
        drives = _mode(drive_governance_mode)
        blockers = list(dict.fromkeys(str(item) for item in attachment_blockers))

        if requested == "live":
            if workspace != "live":
                blockers.append(f"workspace_mode_{workspace}_cannot_feed_live_p3")
            # Event provenance is checked on the exact Concern. Holding the
            # whole runtime because an optional shadow reducer is attached
            # would also stop separately-produced live concerns. Likewise P7
            # shadow is an observer and must not change P3 admission.
            if drives == "live":
                if not charter_store_attached:
                    blockers.append("live_charter_store_not_attached")
                elif not charter_revision_id:
                    blockers.append("active_owner_ratified_charter_required")

        effective = requested
        if requested != "off" and blockers:
            effective = "held"
        authority = {
            "schema": "CognitionRuntimeContractV1",
            "version": 1,
            "requested_mode": requested,
            "effective_mode": effective,
            "workspace_mode": workspace,
            "event_concern_mode": events,
            "drive_governance_mode": drives,
            "charter_revision_id": charter_revision_id,
            "blockers": sorted(set(blockers)),
        }
        return cls(
            requested_mode=requested,
            effective_mode=effective,
            workspace_mode=workspace,
            event_concern_mode=events,
            drive_governance_mode=drives,
            charter_revision_id=charter_revision_id,
            blockers=tuple(authority["blockers"]),
            revision=f"cognition-runtime:{_digest(authority)[:24]}",
        )

    def payload(self) -> dict[str, Any]:
        return asdict(self)


__all__ = ["CognitionRuntimeContractV1"]
