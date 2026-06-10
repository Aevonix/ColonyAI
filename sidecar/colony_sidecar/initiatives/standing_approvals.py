"""Standing approvals (v0.18.0) — owner-granted "always allow this action".

When the owner approves a blocked job with ``{"always": true}``, the
job's action name is recorded here and ``classify_agent_action`` skips
the gate for that exact name from then on — in BOTH policy modes. The
grant is per registered capability name, never per command string, so it
inherits the registry's allow-list guarantees.

State lives in ``$COLONY_STATE_DIR/standing_approvals.json``. The file
is read on every check (it is tiny) so grants/revokes take effect
immediately and survive process restarts. A corrupt file is treated as
empty — the gate fails closed.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from colony_sidecar import get_state_dir

logger = logging.getLogger(__name__)

_FILENAME = "standing_approvals.json"


def _path() -> Path:
    return get_state_dir() / _FILENAME


def load() -> Dict[str, Dict[str, Any]]:
    """Read all standing approvals, keyed by action name.

    Missing or unreadable file → empty dict (gate stays closed).
    """
    path = _path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s: %s — treating as empty", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("%s is not a JSON object — treating as empty", path)
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _save(data: Dict[str, Dict[str, Any]]) -> None:
    """Atomic write — never leave a half-written approvals file."""
    path = _path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def is_approved(action_name: Optional[str]) -> bool:
    """True when the owner has granted a standing approval for the action."""
    if not action_name:
        return False
    return action_name in load()


def grant(action_name: str, approved_by: str = "owner") -> Dict[str, Any]:
    """Record a standing approval for an exact action name."""
    if not action_name:
        raise ValueError("action_name is required")
    data = load()
    entry = {
        "action_name": action_name,
        "approved_by": approved_by,
        "granted_at": datetime.now(timezone.utc).isoformat(),
    }
    data[action_name] = entry
    _save(data)
    logger.info("Standing approval granted for %s by %s", action_name, approved_by)
    return entry


def revoke(action_name: str) -> bool:
    """Remove a standing approval. False when none existed."""
    data = load()
    if action_name not in data:
        return False
    del data[action_name]
    _save(data)
    logger.info("Standing approval revoked for %s", action_name)
    return True


def list() -> List[Dict[str, Any]]:  # noqa: A001 — spec'd API name
    """All standing approvals, sorted by action name."""
    return [entry for _, entry in sorted(load().items())]
