"""Dispatch admission: an operator kill-switch checked before every mutation.

THE PROTOCOL
============
A :class:`DispatchAdmission` answers exactly one question at exactly one
moment: "may this process attempt a mutation RIGHT NOW?"  The worker calls
``assert_live()`` immediately before every owner-authorized dispatch (the
transition that consumes the approval gate and precedes the one PUT).
``assert_live()`` MUST:

* re-read its authority from durable or ambient state at call time — never
  cache a yes;
* raise :class:`DispatchAdmissionError` to refuse, in which case the worker
  defers the leased action WITHOUT consuming the gate or counting an
  attempt; and
* return ``None`` only when a mutation is admitted for immediate dispatch.

Admission is deliberately NOT authorization.  The owner approval gate binds
one action; admission binds the deployment (this binary, this store, this
endpoint, these tools, this time window).  Deleting or expiring the
admission halts all mutations without touching any durable action state.

THE REFERENCE IMPLEMENTATION
============================
:class:`FileDispatchAdmission` reads an owner-only mode-0600 canonical-JSON
file on every check.  The private deployment this generalizes pinned its
SQLite store by device/inode and its release by commit SHA inside the
admission document; those are host-deployment details, so here they become
one optional ``identity_probe`` hook: a host callable returning any
JSON-serializable identity document (a device/inode pair, a release SHA, a
container digest, ...).  The probe result is captured at construction,
required to match the admission file's ``binding_identity`` field, and
re-probed on EVERY ``assert_live()`` — so the resource the admission was
issued for cannot be swapped underneath a live worker.  A host that
configures no probe gets a file whose ``binding_identity`` must be ``null``;
the probe is optional, the field is not.
"""

from __future__ import annotations

import math
import os
import re
import stat
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, runtime_checkable

from ._private_io import loopback_origin, read_private_json, safe_ancestry
from .catalog import ACTION_TOOL_NAMES
from .contract import GATE_CLOCK_SKEW_SECONDS, canonical_json_utf8


class DispatchAdmissionError(RuntimeError):
    """The deployment is not admitted to attempt a mutation right now."""


@runtime_checkable
class DispatchAdmission(Protocol):
    """See the module docstring for the full ``assert_live`` contract."""

    def assert_live(self) -> None:
        """Raise :class:`DispatchAdmissionError` unless a mutation may be
        attempted immediately; never cache a previous answer."""
        ...


ADMISSION_SCHEMA = "ColonyHostWorkerAdmissionV1"
ADMISSION_FIELDS = frozenset(
    {
        "schema",
        "version",
        "authorized",
        "authorization_id",
        "colony_origin",
        "enabled_tools",
        "binding_identity",
        "created_at",
        "expires_at",
    }
)
ADMISSION_MAX_LIFETIME_SECONDS = 30 * 24 * 60 * 60

AUTHORIZATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def sqlite_database_identity(path: str | Path) -> dict[str, Any]:
    """Reference ``identity_probe`` for a host whose store is local SQLite.

    Returns the database file's device/inode identity after asserting that
    the file, its mutable WAL/journal siblings, and its whole directory
    ancestry are private to the calling user and free of symlinks.  This is
    the generalized form of the private deployment's store pinning; hosts
    with a different store supply their own probe (or none).
    """

    target = Path(os.path.abspath(os.path.expanduser(str(path))))
    safe_ancestry(target.parent, label="action store", error=DispatchAdmissionError)
    try:
        info = target.lstat()
    except OSError as error:
        raise DispatchAdmissionError("action store file is unavailable") from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) & 0o077
        or (hasattr(os, "geteuid") and info.st_uid != os.geteuid())
    ):
        raise DispatchAdmissionError("action store file is unsafe")
    for suffix in ("-wal", "-shm", "-journal"):
        sibling = Path(str(target) + suffix)
        if not os.path.lexists(sibling):
            continue
        try:
            sibling_info = sibling.lstat()
        except OSError as error:
            raise DispatchAdmissionError(
                "action store journal is unavailable"
            ) from error
        if (
            stat.S_ISLNK(sibling_info.st_mode)
            or not stat.S_ISREG(sibling_info.st_mode)
            or stat.S_IMODE(sibling_info.st_mode) & 0o077
            or (
                hasattr(os, "geteuid")
                and sibling_info.st_uid != os.geteuid()
            )
        ):
            raise DispatchAdmissionError("action store journal is unsafe")
    return {"path": str(target), "device": info.st_dev, "inode": info.st_ino}


class FileDispatchAdmission:
    """File-based reference :class:`DispatchAdmission`.

    The admission file must be the exact canonical UTF-8 JSON encoding of an
    :data:`ADMISSION_SCHEMA` document plus one trailing newline, mode 0600,
    owned by the calling user.  Every field the file carries is pinned
    against this process's configuration; a mismatch anywhere fails closed.
    """

    def __init__(
        self,
        path: str,
        *,
        colony_origin: str,
        enabled_tools: Iterable[str],
        clock: Callable[[], float],
        identity_probe: Callable[[], Any] | None = None,
    ) -> None:
        configured = str(path or "")
        if not configured or not os.path.isabs(configured):
            raise DispatchAdmissionError("admission path must be absolute")
        try:
            tools = frozenset(enabled_tools)
        except TypeError as error:
            raise DispatchAdmissionError("admitted tools are invalid") from error
        if (
            not tools
            or any(not isinstance(tool, str) for tool in tools)
            or tools - ACTION_TOOL_NAMES
        ):
            raise DispatchAdmissionError("admitted tools are invalid")
        if not callable(clock):
            raise DispatchAdmissionError("admission clock is invalid")
        if identity_probe is not None and not callable(identity_probe):
            raise DispatchAdmissionError("admission identity probe is invalid")
        self.path = configured
        self.colony_origin = loopback_origin(
            colony_origin, error=DispatchAdmissionError
        )
        self.enabled_tools = tools
        self.clock = clock
        self.identity_probe = identity_probe
        # Capture the identity ONCE at construction; assert_live() then
        # requires probe-now == probe-at-construction == file value, so the
        # bound resource cannot be swapped underneath a live worker.
        self.pinned_identity = self._probe_identity()

    def _probe_identity(self) -> str | None:
        if self.identity_probe is None:
            return None
        try:
            observed = self.identity_probe()
        except DispatchAdmissionError:
            raise
        except Exception as error:
            raise DispatchAdmissionError("admission identity probe failed") from error
        try:
            return canonical_json_utf8(observed)
        except Exception as error:
            raise DispatchAdmissionError(
                "admission identity is not canonical JSON"
            ) from error

    def fence(self) -> dict[str, Any]:
        """Secret-free deployment identity for observability."""

        return {
            "schema": "ColonyHostWorkerAdmissionFenceV1",
            "admission_file": self.path,
            "colony_origin": self.colony_origin,
            "enabled_tools": sorted(self.enabled_tools),
            "binding_identity": self.pinned_identity,
        }

    def assert_live(self) -> None:
        _target, raw, document = read_private_json(
            self.path,
            label="host worker admission",
            error=DispatchAdmissionError,
        )
        if not isinstance(document, Mapping) or set(document) != ADMISSION_FIELDS:
            raise DispatchAdmissionError("admission fields are invalid")
        try:
            canonical = (canonical_json_utf8(dict(document)) + "\n").encode("utf-8")
        except (
            TypeError,
            ValueError,
            UnicodeError,
            OverflowError,
            RecursionError,
        ) as error:
            raise DispatchAdmissionError("admission is not canonical") from error
        if raw != canonical:
            raise DispatchAdmissionError("admission must be canonical JSON")
        version = document.get("version")
        authorized = document.get("authorized")
        created_at = document.get("created_at")
        expires_at = document.get("expires_at")
        admitted_tools = document.get("enabled_tools")
        binding_identity = document.get("binding_identity")
        if (
            document.get("schema") != ADMISSION_SCHEMA
            or isinstance(version, bool)
            or version != 1
            or authorized is not True
            or not isinstance(document.get("authorization_id"), str)
            or not AUTHORIZATION_ID_RE.fullmatch(document["authorization_id"])
            or document.get("colony_origin") != self.colony_origin
            or not isinstance(admitted_tools, list)
            or admitted_tools != sorted(self.enabled_tools)
            or len(admitted_tools) != len(self.enabled_tools)
            or isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
        ):
            raise DispatchAdmissionError("admission does not bind this process")
        if self.identity_probe is None:
            if binding_identity is not None:
                raise DispatchAdmissionError(
                    "admission does not bind this process"
                )
        else:
            try:
                observed_file = canonical_json_utf8(binding_identity)
            except Exception as error:
                raise DispatchAdmissionError(
                    "admission does not bind this process"
                ) from error
            observed_now = self._probe_identity()
            if (
                binding_identity is None
                or observed_file != self.pinned_identity
                or observed_now != self.pinned_identity
            ):
                raise DispatchAdmissionError("admission binding identity changed")
        created = float(created_at)
        expires = float(expires_at)
        try:
            now = float(self.clock())
        except (TypeError, ValueError, OverflowError) as error:
            raise DispatchAdmissionError("admission clock failed") from error
        if (
            not math.isfinite(now)
            or not math.isfinite(created)
            or not math.isfinite(expires)
            or now <= 0
            or created <= 0
            or created > now + GATE_CLOCK_SKEW_SECONDS
            or expires <= created
            or expires - created > ADMISSION_MAX_LIFETIME_SECONDS
            or expires <= now
        ):
            raise DispatchAdmissionError("admission is not live")


__all__ = (
    "ADMISSION_FIELDS",
    "ADMISSION_MAX_LIFETIME_SECONDS",
    "ADMISSION_SCHEMA",
    "AUTHORIZATION_ID_RE",
    "DispatchAdmission",
    "DispatchAdmissionError",
    "FileDispatchAdmission",
    "sqlite_database_identity",
)
