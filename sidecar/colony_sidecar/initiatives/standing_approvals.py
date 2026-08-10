"""Deprecated compatibility facade for historical standing approvals.

New queue approvals use ``approval_authority.db`` and immutable
ApprovalRequests.  A few older, non-queue call sites still use this module; to
avoid leaving a permanent bypass during migration, every entry now has both a
short expiry and a use cap, and :func:`is_approved` atomically consumes a use.

Old unbounded JSON entries are accepted once as a one-use/24-hour migration
grant. They are rewritten in bounded form on their first check. No new caller
can create permanent authority through this API.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from colony_sidecar import get_state_dir


logger = logging.getLogger(__name__)

_FILENAME = "standing_approvals.json"
_LOCK_FILENAME = "standing_approvals.lock"
_DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60
_DEFAULT_MAX_USES = 5
_MIGRATION_TTL_SECONDS = 24 * 60 * 60
_MAX_TTL_SECONDS = 30 * 24 * 60 * 60
_MAX_USES = 100


def _path() -> Path:
    return get_state_dir() / _FILENAME


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(text)
    except ValueError:
        return None
    if result.tzinfo is None:
        return None
    return result.astimezone(timezone.utc)


@contextmanager
def _locked() -> Iterator[None]:
    lock_path = get_state_dir() / _LOCK_FILENAME
    with lock_path.open("a+", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_raw() -> Dict[str, Dict[str, Any]]:
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
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _bounded_entry(
    action_name: str,
    entry: Dict[str, Any],
    *,
    observed: datetime,
) -> Dict[str, Any]:
    """Normalize an entry; legacy unbounded records get one migration use."""

    granted_at = _parse_time(entry.get("granted_at")) or observed
    expires_at = _parse_time(entry.get("expires_at"))
    max_uses = entry.get("max_uses")
    uses = entry.get("uses", 0)
    migrated = expires_at is None or not isinstance(max_uses, int)
    if migrated:
        expires_at = observed + timedelta(seconds=_MIGRATION_TTL_SECONDS)
        max_uses = 1
        uses = 0
    max_uses = max(1, min(int(max_uses), _MAX_USES))
    uses = max(0, int(uses))
    status = str(entry.get("status") or "active")
    if observed >= expires_at:
        status = "expired"
    elif uses >= max_uses:
        status = "exhausted"
    return {
        "action_name": action_name,
        # Historical text is retained as audit metadata only. It is never
        # interpreted as authenticated authority.
        "approved_by": str(entry.get("approved_by") or "legacy-migrated"),
        "granted_at": granted_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "max_uses": max_uses,
        "uses": uses,
        "status": status,
        "migrated_from_unbounded": bool(
            entry.get("migrated_from_unbounded") or migrated
        ),
    }


def load() -> Dict[str, Dict[str, Any]]:
    """Read and normalize all compatibility grants."""

    observed = _now()
    return {
        name: _bounded_entry(name, entry, observed=observed)
        for name, entry in _read_raw().items()
    }


def _save(data: Dict[str, Dict[str, Any]]) -> None:
    path = _path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def is_approved(action_name: Optional[str]) -> bool:
    """Consume one bounded compatibility use for this exact action name."""

    if not action_name:
        return False
    with _locked():
        observed = _now()
        raw = _read_raw()
        original = raw.get(action_name)
        if original is None:
            return False
        entry = _bounded_entry(action_name, original, observed=observed)
        if entry["status"] != "active":
            raw[action_name] = entry
            _save(raw)
            return False
        entry["uses"] += 1
        if entry["uses"] >= entry["max_uses"]:
            entry["status"] = "exhausted"
        raw[action_name] = entry
        _save(raw)
        return True


def grant(
    action_name: str,
    approved_by: str = "trusted-internal",
    *,
    expires_in_seconds: int = _DEFAULT_TTL_SECONDS,
    max_uses: int = _DEFAULT_MAX_USES,
) -> Dict[str, Any]:
    """Create a bounded legacy grant for an exact non-queue action.

    Queue/API code must use :mod:`approval_authority` instead. This helper is
    retained for embedded directed-action integrations during migration.
    """

    if not action_name:
        raise ValueError("action_name is required")
    if not 60 <= int(expires_in_seconds) <= _MAX_TTL_SECONDS:
        raise ValueError("expires_in_seconds is out of bounds")
    if not 1 <= int(max_uses) <= _MAX_USES:
        raise ValueError("max_uses is out of bounds")
    observed = _now()
    entry = {
        "action_name": action_name,
        "approved_by": str(approved_by or "trusted-internal"),
        "granted_at": observed.isoformat(),
        "expires_at": (
            observed + timedelta(seconds=int(expires_in_seconds))
        ).isoformat(),
        "max_uses": int(max_uses),
        "uses": 0,
        "status": "active",
        "migrated_from_unbounded": False,
    }
    with _locked():
        data = _read_raw()
        data[action_name] = entry
        _save(data)
    logger.info(
        "Bounded compatibility approval granted for %s (%s uses)",
        action_name,
        max_uses,
    )
    return entry


def revoke(action_name: str) -> bool:
    """Revoke a compatibility grant without deleting its audit metadata."""

    with _locked():
        data = _read_raw()
        if action_name not in data:
            return False
        entry = _bounded_entry(action_name, data[action_name], observed=_now())
        if entry["status"] == "revoked":
            return False
        entry["status"] = "revoked"
        data[action_name] = entry
        _save(data)
    logger.info("Bounded compatibility approval revoked for %s", action_name)
    return True


def list() -> List[Dict[str, Any]]:  # noqa: A001 — legacy API name
    """All compatibility grants, including expired/exhausted audit records."""

    return [entry for _, entry in sorted(load().items())]
