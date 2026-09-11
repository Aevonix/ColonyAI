"""Server-attested, hot-reloaded exact contact grants for API principals.

The credential keyring remains static and secret-bearing. This separate
mode-private projection contains only exact Colony person IDs learned at the
ParticipantResolver chokepoint. A request body can use an existing grant; it
can never create one.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
from typing import Any, Mapping


logger = logging.getLogger(__name__)

_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_PERSON_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,255}$")
_RESERVED_PERSON_IDS = frozenset({"*", "owner", "shared", "global", "viewer", "system"})


class ContactGrantError(ValueError):
    """A grant projection is unsafe or malformed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _exact_principal(value: Any) -> str:
    principal = str(value or "").strip()
    if not _PRINCIPAL_RE.fullmatch(principal):
        raise ContactGrantError("invalid principal ID in contact grant projection")
    return principal


def _exact_person(value: Any) -> str:
    person_id = str(value or "").strip()
    if (
        not _PERSON_RE.fullmatch(person_id)
        or person_id.lower() in _RESERVED_PERSON_IDS
        or "*" in person_id
    ):
        raise ContactGrantError("contact grants require one exact non-reserved person ID")
    return person_id


def _private_regular_file(path: Path) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ContactGrantError("contact grant path must be a regular file")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ContactGrantError("contact grant file must be mode 0600")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise ContactGrantError("contact grant file must be owned by the service user")
    return info


def _parse_document(raw: Any) -> dict[str, frozenset[str]]:
    if not isinstance(raw, Mapping) or raw.get("version") != 1:
        raise ContactGrantError("contact grant projection version must be 1")
    if set(raw) - {"version", "principals"}:
        raise ContactGrantError("contact grant projection contains unsupported fields")
    records = raw.get("principals")
    if not isinstance(records, Mapping):
        raise ContactGrantError("contact grant principals must be an object")
    result: dict[str, frozenset[str]] = {}
    for raw_principal, record in records.items():
        principal = _exact_principal(raw_principal)
        if not isinstance(record, Mapping):
            raise ContactGrantError(f"contact grant record for {principal!r} must be an object")
        raw_ids = record.get("person_ids")
        if not isinstance(raw_ids, list):
            raise ContactGrantError(f"contact grant person_ids for {principal!r} must be an array")
        exact_ids = frozenset(_exact_person(item) for item in raw_ids)
        if len(exact_ids) != len(raw_ids):
            raise ContactGrantError(f"contact grant person_ids for {principal!r} contain duplicates")
        # Projection records deliberately cannot add audiences/lanes or scopes.
        unknown = set(record) - {"person_ids", "updated_at"}
        if unknown:
            raise ContactGrantError(
                f"contact grant record for {principal!r} contains unsupported fields"
            )
        updated_at = str(record.get("updated_at") or "").strip()
        if updated_at:
            candidate = updated_at[:-1] + "+00:00" if updated_at.endswith("Z") else updated_at
            try:
                parsed = datetime.fromisoformat(candidate)
            except ValueError as exc:
                raise ContactGrantError(
                    f"contact grant updated_at for {principal!r} is invalid"
                ) from exc
            if parsed.tzinfo is None:
                raise ContactGrantError(
                    f"contact grant updated_at for {principal!r} needs a timezone"
                )
        result[principal] = exact_ids
    return result


class ContactGrantRegistry:
    """Mode-private exact-ID projection with atomic write and hot reload."""

    def __init__(self, path: str | os.PathLike[str] | None) -> None:
        self.path = Path(path).expanduser() if path else None
        self._signature: tuple[int, int, int, int, int] | tuple[str] | None = None
        self._grants: dict[str, frozenset[str]] = {}
        self._updated_at: dict[str, str] = {}
        self._error: str | None = None
        self._document_available = False
        self._last_loaded_at: str | None = None
        self._last_written_at: str | None = None
        self._policy_error: str | None = None
        self._lock = threading.RLock()

    @property
    def configured(self) -> bool:
        return self.path is not None

    def _current_signature(self) -> tuple[int, int, int, int, int] | tuple[str]:
        assert self.path is not None
        try:
            info = self.path.lstat()
        except OSError:
            return ("missing",)
        return (
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            stat.S_IMODE(info.st_mode),
        )

    def _reload_if_needed(self) -> None:
        if self.path is None:
            return
        signature = self._current_signature()
        with self._lock:
            if signature == self._signature:
                return
            self._signature = signature
            self._policy_error = None
            if signature == ("missing",):
                self._grants = {}
                self._updated_at = {}
                self._document_available = False
                self._error = None
                self._last_loaded_at = _now()
                return
            try:
                _private_regular_file(self.path)
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                grants = _parse_document(raw)
                records = raw.get("principals") or {}
                self._grants = grants
                self._document_available = True
                self._updated_at = {
                    principal: str((records.get(principal) or {}).get("updated_at") or "")
                    for principal in grants
                }
                self._error = None
                self._last_loaded_at = _now()
                logger.info(
                    "Loaded %d exact contact grant set(s) from %s",
                    len(grants), self.path,
                )
            except (OSError, UnicodeError, ValueError, ContactGrantError) as exc:
                # Fail closed on malformed replacement; never retain stale grants.
                self._grants = {}
                self._updated_at = {}
                self._document_available = False
                self._error = str(exc)
                self._last_loaded_at = _now()
                logger.error("Exact contact grants unavailable: %s", exc)

    def person_ids(self, principal: str, *, max_person_ids: int) -> frozenset[str]:
        """Return exact IDs only when the projection respects the policy cap."""

        self._reload_if_needed()
        with self._lock:
            ids = self._grants.get(principal, frozenset())
            if len(ids) > max_person_ids:
                self._policy_error = (
                    f"principal {principal!r} has {len(ids)} contact grants, "
                    f"above its cap of {max_person_ids}"
                )
                return frozenset()
            return ids

    def principal_projection(
        self,
        principal: str,
        *,
        max_person_ids: int,
    ) -> dict[str, Any]:
        """Return one caller's exact, server-attested contact-grant posture.

        This is a read model for an already authenticated principal.  It never
        enumerates another principal and never adds a person, audience, or API
        scope.  Callers must still enforce their own authentication boundary;
        this method only applies the same configured cap as ``person_ids``.
        """

        principal = _exact_principal(principal)
        if not isinstance(max_person_ids, int) or not 1 <= max_person_ids <= 4096:
            raise ContactGrantError("contact grant cap must be 1..4096")
        self._reload_if_needed()
        with self._lock:
            ids = self._grants.get(principal, frozenset())
            if len(ids) > max_person_ids:
                self._policy_error = (
                    f"principal {principal!r} has {len(ids)} contact grants, "
                    f"above its cap of {max_person_ids}"
                )
                return {
                    "available": False,
                    "reason": "principal_contact_grant_cap_exceeded",
                    "person_ids": [],
                    "updated_at": self._updated_at.get(principal) or None,
                }
            available = (
                self.configured
                and self._document_available
                and self._error is None
            )
            return {
                "available": available,
                "reason": None if available else (
                    "contact_grant_projection_unconfigured"
                    if not self.configured
                    else "contact_grant_projection_missing"
                    if not self._document_available and self._error is None
                    else "contact_grant_projection_invalid"
                ),
                "person_ids": sorted(ids) if available else [],
                "updated_at": self._updated_at.get(principal) or None,
            }

    def grant(self, principal: str, person_id: str, *, max_person_ids: int) -> bool:
        """Atomically add one server-attested exact ID.

        Returns ``False`` when no projection is configured, it is invalid, or
        the bounded principal cap would be exceeded. Existing grants are
        idempotent and return ``True``.
        """

        if self.path is None:
            return False
        principal = _exact_principal(principal)
        person_id = _exact_person(person_id)
        if not isinstance(max_person_ids, int) or not 1 <= max_person_ids <= 4096:
            raise ContactGrantError("contact grant cap must be 1..4096")
        with self._lock:
            self._reload_if_needed()
            if self._error:
                return False
            current = set(self._grants.get(principal, frozenset()))
            if person_id in current:
                return True
            if len(current) >= max_person_ids:
                self._policy_error = (
                    f"principal {principal!r} reached its contact grant cap "
                    f"of {max_person_ids}"
                )
                logger.warning("%s", self._policy_error)
                return False
            current.add(person_id)
            all_grants = dict(self._grants)
            all_grants[principal] = frozenset(current)
            updated = dict(self._updated_at)
            updated[principal] = _now()
            document = {
                "version": 1,
                "principals": {
                    key: {
                        "person_ids": sorted(values),
                        "updated_at": updated.get(key) or _now(),
                    }
                    for key, values in sorted(all_grants.items())
                },
            }
            self._atomic_write(document)
            self._signature = None
            self._reload_if_needed()
            self._last_written_at = updated[principal]
            return self._error is None and person_id in self._grants.get(
                principal, frozenset()
            )

    def _atomic_write(self, document: dict[str, Any]) -> None:
        assert self.path is not None
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, raw_tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(parent))
        tmp = Path(raw_tmp)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
            try:
                directory = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError:
                pass
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def status(self) -> dict[str, Any]:
        """Return privacy-safe counts and loader state, never exact IDs."""

        self._reload_if_needed()
        with self._lock:
            return {
                "configured": self.configured,
                "available": (
                    self.configured
                    and self._document_available
                    and self._error is None
                ),
                "error": self._error or self._policy_error,
                "last_loaded_at": self._last_loaded_at,
                "last_written_at": self._last_written_at,
                "principal_counts": {
                    principal: len(values)
                    for principal, values in sorted(self._grants.items())
                },
                "total_exact_person_ids": sum(len(values) for values in self._grants.values()),
            }
