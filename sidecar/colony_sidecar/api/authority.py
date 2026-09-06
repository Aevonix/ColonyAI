"""Authenticated API principals and server-derived person authority.

The legacy ``COLONY_API_KEY`` remains a migration credential. New scoped
credentials are loaded from a private JSON keyring and bind a service
principal to exact API scopes, a viewer/person, and explicit audience lanes.
Request bodies may narrow to a granted lane; they cannot broaden authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Mapping

from fastapi import HTTPException
from starlette.requests import Request


logger = logging.getLogger(__name__)

_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_CREDENTIAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,191}$")
_PERSON_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,127}$")
_SCOPE_RE = re.compile(r"^(?:\*|[a-z][a-z0-9_.-]*:[a-z][a-z0-9_.-]*)$")
_PLATFORM_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_WORKER_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_RESERVED_PRINCIPALS = frozenset({"legacy", "anonymous-dev", "unauthenticated", "public"})
_AUDIENCES = frozenset({"viewer", "owner", "shared", "global"})
_ACCEPTING_STATUSES = frozenset({"active", "retiring"})
_KNOWN_STATUSES = frozenset({"active", "retiring", "disabled", "revoked"})
_GOVERNED_ACTION_PRINCIPAL = "host-action-worker"
_GOVERNED_ACTION_SCOPES = frozenset({"actions:execute", "actions:verify"})

# Canonical read-only work surfaces used to assemble an operator's current
# working set.  Keep this exact: collection reads belong to ``work:read``;
# neighbouring item reads and every mutation retain their existing authority.
WORK_READ_SURFACE_V1 = frozenset({
    ("GET", "/v1/host/goals"),
    ("GET", "/v1/host/projects"),
    ("GET", "/v1/host/queue/jobs/pending"),
    ("GET", "/v1/host/queue/jobs/neutral"),
})
_API_ACCESS_COMPATIBILITY = frozenset({"api:access"})
_NO_COMPATIBILITY_SCOPES: frozenset[str] = frozenset()


class KeyringError(ValueError):
    """The configured keyring is unsafe or malformed."""


@dataclass(frozen=True)
class Credential:
    credential_id: str
    secret: str
    status: str
    accept_until: datetime | None

    def accepts(self, now: datetime) -> bool:
        return _accepts(self.status, self.accept_until, now)


@dataclass(frozen=True)
class WorkerGrant:
    """Server-owned ceiling for one worker node authenticated by a principal."""

    node_id: str
    capabilities: frozenset[str]
    capacity: tuple[tuple[str, float], ...]
    max_concurrent: int
    job_types: frozenset[str]

    def capacity_map(self) -> dict[str, float]:
        return dict(self.capacity)


@dataclass(frozen=True)
class Principal:
    principal_id: str
    status: str
    accept_until: datetime | None
    scopes: frozenset[str]
    allow_unscoped_api: bool
    viewer_person_id: str | None
    person_ids: frozenset[str]
    audiences: frozenset[str]
    turn_ingress_platforms: frozenset[str]
    attested_contact_platforms: frozenset[str]
    attested_contact_limit: int
    worker_grants: tuple[WorkerGrant, ...]
    credentials: tuple[Credential, ...]

    def accepts(self, now: datetime) -> bool:
        return _accepts(self.status, self.accept_until, now)


@dataclass(frozen=True)
class AuthenticatedCredential:
    principal: Principal
    credential: Credential

    def accepts(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        return self.principal.accepts(current) and self.credential.accepts(current)


@dataclass(frozen=True)
class RequestAuthority:
    """Authority attached to ``request.state`` by the auth middleware."""

    principal_id: str
    credential_id: str | None
    scopes: frozenset[str]
    viewer_person_id: str | None
    person_ids: frozenset[str]
    audiences: frozenset[str]
    authenticated: bool
    static_person_ids: frozenset[str] = frozenset()
    turn_ingress_platforms: frozenset[str] = frozenset()
    allow_unscoped_api: bool = True
    legacy: bool = False
    anonymous: bool = False
    attested_contact_platforms: frozenset[str] = frozenset()
    attested_contact_limit: int = 0
    worker_grants: tuple[WorkerGrant, ...] = ()

    def has_scope(self, required: str) -> bool:
        return self.legacy or "*" in self.scopes or required in self.scopes


def _clean_string(value: Any, *, field: str, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise KeyringError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise KeyringError(f"{field} must be a string")
    cleaned = value.strip()
    if not cleaned:
        if required:
            raise KeyringError(f"{field} must not be empty")
        return None
    return cleaned


def _parse_time(value: Any, *, field: str) -> datetime | None:
    cleaned = _clean_string(value, field=field)
    if cleaned is None:
        return None
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise KeyringError(f"{field} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise KeyringError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _parse_status(value: Any, *, field: str) -> str:
    status = _clean_string(value if value is not None else "active", field=field, required=True)
    assert status is not None
    status = status.lower()
    if status not in _KNOWN_STATUSES:
        raise KeyringError(
            f"{field} must be one of {', '.join(sorted(_KNOWN_STATUSES))}"
        )
    return status


def _accepts(status: str, accept_until: datetime | None, now: datetime) -> bool:
    if status not in _ACCEPTING_STATUSES:
        return False
    if accept_until is not None and now >= accept_until:
        return False
    return True


def _string_set(value: Any, *, field: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise KeyringError(f"{field} must be a JSON array")
    result: set[str] = set()
    for index, item in enumerate(value):
        cleaned = _clean_string(item, field=f"{field}[{index}]", required=True)
        assert cleaned is not None
        result.add(cleaned)
    return frozenset(result)


def _person_id(value: Any, *, field: str) -> str:
    cleaned = _clean_string(value, field=field, required=True)
    assert cleaned is not None
    if "*" in cleaned:
        raise KeyringError(f"{field} cannot contain a wildcard")
    if value != cleaned or not _PERSON_ID_RE.fullmatch(cleaned):
        raise KeyringError(
            f"{field} must be a canonical person ID of at most 128 characters"
        )
    return cleaned


def _person_id_set(value: Any, *, field: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise KeyringError(f"{field} must be a JSON array")
    return frozenset(
        _person_id(item, field=f"{field}[{index}]")
        for index, item in enumerate(value)
    )


def _parse_worker_grants(
    value: Any,
    *,
    field: str,
    scopes: frozenset[str],
) -> tuple[WorkerGrant, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise KeyringError(f"{field} must be a JSON array")
    if len(value) > 64:
        raise KeyringError(f"{field} supports at most 64 exact nodes")
    grants: list[WorkerGrant] = []
    seen_nodes: set[str] = set()
    for index, raw in enumerate(value):
        prefix = f"{field}[{index}]"
        if not isinstance(raw, Mapping):
            raise KeyringError(f"{prefix} must be an object")
        unknown = set(raw) - {
            "node_id", "capabilities", "capacity", "max_concurrent", "job_types",
        }
        if unknown:
            raise KeyringError(f"{prefix} contains unsupported fields")
        node_id = _clean_string(
            raw.get("node_id"), field=f"{prefix}.node_id", required=True,
        )
        assert node_id is not None
        if not _WORKER_TOKEN_RE.fullmatch(node_id) or "*" in node_id:
            raise KeyringError(f"{prefix}.node_id is invalid")
        if node_id in seen_nodes:
            raise KeyringError(f"{field} contains duplicate node_id {node_id!r}")
        seen_nodes.add(node_id)

        capabilities = _string_set(
            raw.get("capabilities"), field=f"{prefix}.capabilities",
        )
        if any(
            not _WORKER_TOKEN_RE.fullmatch(item) or "*" in item
            for item in capabilities
        ):
            raise KeyringError(f"{prefix}.capabilities contains an invalid name")
        job_types = _string_set(
            raw.get("job_types"), field=f"{prefix}.job_types",
        )
        if not job_types:
            raise KeyringError(f"{prefix}.job_types must not be empty")
        if any(
            not _WORKER_TOKEN_RE.fullmatch(item) or "*" in item
            for item in job_types
        ):
            raise KeyringError(f"{prefix}.job_types contains an invalid name")
        from colony_sidecar.task_queue.models import JobType
        known_job_types = {item.value for item in JobType}
        unknown_job_types = job_types - known_job_types
        if unknown_job_types:
            raise KeyringError(
                f"{prefix}.job_types contains unsupported values: "
                f"{', '.join(sorted(unknown_job_types))}"
            )

        raw_capacity = raw.get("capacity", {})
        if not isinstance(raw_capacity, Mapping):
            raise KeyringError(f"{prefix}.capacity must be an object")
        capacity: list[tuple[str, float]] = []
        for key, raw_amount in raw_capacity.items():
            if (
                not isinstance(key, str)
                or not _WORKER_TOKEN_RE.fullmatch(key)
                or "*" in key
            ):
                raise KeyringError(f"{prefix}.capacity contains an invalid name")
            if isinstance(raw_amount, bool) or not isinstance(raw_amount, (int, float)):
                raise KeyringError(f"{prefix}.capacity.{key} must be numeric")
            amount = float(raw_amount)
            if not math.isfinite(amount) or amount < 0:
                raise KeyringError(
                    f"{prefix}.capacity.{key} must be finite and non-negative"
                )
            capacity.append((key, amount))

        max_concurrent = raw.get("max_concurrent", 1)
        if (
            isinstance(max_concurrent, bool)
            or not isinstance(max_concurrent, int)
            or not 1 <= max_concurrent <= 1024
        ):
            raise KeyringError(f"{prefix}.max_concurrent must be 1..1024")
        grants.append(WorkerGrant(
            node_id=node_id,
            capabilities=capabilities,
            capacity=tuple(sorted(capacity)),
            max_concurrent=max_concurrent,
            job_types=job_types,
        ))

    worker_scopes = {
        "workers:register", "workers:claim", "workers:lifecycle",
    }
    if grants and "*" not in scopes and not (worker_scopes & set(scopes)):
        raise KeyringError(f"{field} requires at least one workers:* scope")
    if (
        grants
        and "*" not in scopes
        and "workers:claim" in scopes
        and "workers:lifecycle" not in scopes
    ):
        raise KeyringError(
            f"{field} with workers:claim also requires workers:lifecycle"
        )
    return tuple(grants)


def _parse_principal(raw: Any, *, index: int) -> Principal:
    if not isinstance(raw, Mapping):
        raise KeyringError(f"principals[{index}] must be an object")
    prefix = f"principals[{index}]"
    raw_principal_id = raw.get("principal")
    principal_id = _clean_string(
        raw_principal_id, field=f"{prefix}.principal", required=True
    )
    assert principal_id is not None
    if raw_principal_id != principal_id or not _PRINCIPAL_RE.fullmatch(principal_id):
        raise KeyringError(
            f"{prefix}.principal must be a canonical principal ID of at most "
            "128 characters"
        )
    if principal_id in _RESERVED_PRINCIPALS:
        raise KeyringError(f"{prefix}.principal uses a reserved telemetry identity")

    scopes = _string_set(raw.get("scopes"), field=f"{prefix}.scopes")
    for scope in scopes:
        if not _SCOPE_RE.fullmatch(scope):
            raise KeyringError(f"{prefix}.scopes contains invalid scope {scope!r}")
    allow_unscoped_api = raw.get("allow_unscoped_api", True)
    if not isinstance(allow_unscoped_api, bool):
        raise KeyringError(f"{prefix}.allow_unscoped_api must be a boolean")

    raw_viewer = raw.get("viewer_person_id")
    viewer = (
        _person_id(raw_viewer, field=f"{prefix}.viewer_person_id")
        if raw_viewer is not None
        else None
    )
    person_ids = _person_id_set(
        raw.get("person_ids"), field=f"{prefix}.person_ids",
    )
    audiences = _string_set(raw.get("audiences"), field=f"{prefix}.audiences")
    unknown_audiences = audiences - _AUDIENCES
    if unknown_audiences:
        raise KeyringError(
            f"{prefix}.audiences contains unknown lanes: "
            f"{', '.join(sorted(unknown_audiences))}"
        )
    if "viewer" in audiences and not viewer:
        raise KeyringError(f"{prefix}.audiences grants viewer without viewer_person_id")
    if principal_id == _GOVERNED_ACTION_PRINCIPAL and (
        scopes != _GOVERNED_ACTION_SCOPES
        or allow_unscoped_api is not False
        or not viewer
        or viewer not in person_ids
        or audiences != frozenset({"owner"})
    ):
        raise KeyringError(
            f"{prefix} must be the exact owner-bound governed-action role"
        )

    worker_grants = _parse_worker_grants(
        raw.get("worker_grants"), field=f"{prefix}.worker_grants", scopes=scopes,
    )
    if "workers:attest" in scopes:
        execution_scopes = scopes & {
            "workers:register", "workers:claim", "workers:lifecycle", "*",
        }
        if worker_grants or execution_scopes:
            raise KeyringError(
                f"{prefix}.workers:attest requires a verifier-only principal "
                "with no worker grants or execution scopes"
            )

    turn_ingress_platforms_explicit = "turn_ingress_platforms" in raw
    turn_ingress_platforms = _string_set(
        raw.get("turn_ingress_platforms"),
        field=f"{prefix}.turn_ingress_platforms",
    )
    for platform in turn_ingress_platforms:
        if not _PLATFORM_RE.fullmatch(platform) or "*" in platform:
            raise KeyringError(
                f"{prefix}.turn_ingress_platforms contains invalid platform"
            )
    if (
        turn_ingress_platforms
        and "turns:write" not in scopes
        and "*" not in scopes
    ):
        raise KeyringError(
            f"{prefix}.turn_ingress_platforms requires turns:write"
        )

    attested_policy = raw.get("attested_contact_grants")
    attested_platforms: frozenset[str] = frozenset()
    attested_limit = 0
    if attested_policy is not None:
        if not isinstance(attested_policy, Mapping):
            raise KeyringError(f"{prefix}.attested_contact_grants must be an object")
        unknown = set(attested_policy) - {"platforms", "max_person_ids"}
        if unknown:
            raise KeyringError(
                f"{prefix}.attested_contact_grants contains unsupported fields"
            )
        attested_platforms = _string_set(
            attested_policy.get("platforms"),
            field=f"{prefix}.attested_contact_grants.platforms",
        )
        if not attested_platforms:
            raise KeyringError(
                f"{prefix}.attested_contact_grants.platforms must not be empty"
            )
        for platform in attested_platforms:
            if not _PLATFORM_RE.fullmatch(platform) or "*" in platform:
                raise KeyringError(
                    f"{prefix}.attested_contact_grants contains invalid platform"
                )
        raw_limit = attested_policy.get("max_person_ids", 512)
        if (
            isinstance(raw_limit, bool)
            or not isinstance(raw_limit, int)
            or not 1 <= raw_limit <= 4096
        ):
            raise KeyringError(
                f"{prefix}.attested_contact_grants.max_person_ids must be 1..4096"
            )
        attested_limit = raw_limit
        if "turns:resolve-sender" not in scopes and "*" not in scopes:
            raise KeyringError(
                f"{prefix}.attested_contact_grants requires turns:resolve-sender"
            )
        # Compatibility is deliberately server-owned and default-off.  An
        # existing turns writer that omitted the new role derives it only
        # from the already validated static contact-attestation policy.  An
        # explicit role (including an explicit empty role) is authoritative.
        if (
            not turn_ingress_platforms_explicit
            and ("turns:write" in scopes or "*" in scopes)
        ):
            turn_ingress_platforms = attested_platforms
        if (
            turn_ingress_platforms_explicit
            and not attested_platforms.issubset(turn_ingress_platforms)
        ):
            raise KeyringError(
                f"{prefix}.attested_contact_grants.platforms must be a subset "
                "of turn_ingress_platforms"
            )

    raw_credentials = raw.get("credentials")
    if not isinstance(raw_credentials, list) or not raw_credentials:
        raise KeyringError(f"{prefix}.credentials must be a non-empty JSON array")
    credentials: list[Credential] = []
    credential_ids: set[str] = set()
    for cred_index, raw_credential in enumerate(raw_credentials):
        cp = f"{prefix}.credentials[{cred_index}]"
        if not isinstance(raw_credential, Mapping):
            raise KeyringError(f"{cp} must be an object")
        raw_credential_id = raw_credential.get("id")
        credential_id = _clean_string(
            raw_credential_id, field=f"{cp}.id", required=True
        )
        secret = _clean_string(
            raw_credential.get("secret"), field=f"{cp}.secret", required=True
        )
        assert credential_id is not None and secret is not None
        if (
            raw_credential_id != credential_id
            or not _CREDENTIAL_ID_RE.fullmatch(credential_id)
        ):
            raise KeyringError(
                f"{cp}.id must be a canonical credential ID of at most 192 characters"
            )
        if credential_id in credential_ids:
            raise KeyringError(f"duplicate credential id {credential_id!r} in {principal_id}")
        credential_ids.add(credential_id)
        credential_status = _parse_status(
            raw_credential.get("status"), field=f"{cp}.status"
        )
        credential_accept_until = _parse_time(
            raw_credential.get("accept_until"), field=f"{cp}.accept_until"
        )
        if credential_status == "retiring" and credential_accept_until is None:
            raise KeyringError(f"{cp}.retiring credential requires accept_until")
        credentials.append(Credential(
            credential_id=credential_id,
            secret=secret,
            status=credential_status,
            accept_until=credential_accept_until,
        ))

    principal_status = _parse_status(raw.get("status"), field=f"{prefix}.status")
    principal_accept_until = _parse_time(
        raw.get("accept_until"), field=f"{prefix}.accept_until"
    )
    if principal_status == "retiring" and principal_accept_until is None:
        raise KeyringError(f"{prefix}.retiring principal requires accept_until")
    return Principal(
        principal_id=principal_id,
        status=principal_status,
        accept_until=principal_accept_until,
        scopes=scopes,
        allow_unscoped_api=allow_unscoped_api,
        viewer_person_id=viewer,
        person_ids=person_ids,
        audiences=audiences,
        turn_ingress_platforms=turn_ingress_platforms,
        attested_contact_platforms=attested_platforms,
        attested_contact_limit=attested_limit,
        worker_grants=worker_grants,
        credentials=tuple(credentials),
    )


def load_keyring(path: Path) -> tuple[Principal, ...]:
    """Load one private keyring, rejecting group/world-readable files."""

    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise KeyringError("keyring must be a regular file")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise KeyringError(
            f"keyring permissions must be private (chmod 600); found {mode:04o}"
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise KeyringError("keyring is not valid JSON") from exc
    if not isinstance(raw, Mapping) or raw.get("version") != 1:
        raise KeyringError("keyring version must be 1")
    raw_principals = raw.get("principals")
    if not isinstance(raw_principals, list):
        raise KeyringError("keyring principals must be a JSON array")

    principals = tuple(
        _parse_principal(value, index=index)
        for index, value in enumerate(raw_principals)
    )
    principal_ids: set[str] = set()
    secrets: set[str] = set()
    worker_nodes: set[str] = set()
    for principal in principals:
        if principal.principal_id in principal_ids:
            raise KeyringError(f"duplicate principal {principal.principal_id!r}")
        principal_ids.add(principal.principal_id)
        for grant in principal.worker_grants:
            if grant.node_id in worker_nodes:
                raise KeyringError(
                    f"worker node {grant.node_id!r} is granted to multiple principals"
                )
            worker_nodes.add(grant.node_id)
        for credential in principal.credentials:
            if credential.secret in secrets:
                raise KeyringError("credential secrets must be unique across the keyring")
            secrets.add(credential.secret)
    return principals


class KeyringLoader:
    """Reload a keyring when its identity, contents, timestamps, or mode change.

    An invalid replacement disables scoped credentials until a valid private
    file is installed. The separately configured legacy key remains usable,
    which makes migration failures recoverable without accepting stale revoked
    scoped keys.
    """

    def __init__(self, path: str | os.PathLike[str] | None) -> None:
        self.path = Path(path).expanduser() if path else None
        self._signature: tuple[int, int, int, int, int] | tuple[str] | None = None
        self._principals: tuple[Principal, ...] = ()
        self._error: str | None = None
        self._lock = threading.RLock()

    @property
    def configured(self) -> bool:
        return self.path is not None

    @property
    def error(self) -> str | None:
        self._reload_if_needed()
        return self._error

    def _current_signature(self) -> tuple[int, int, int, int, int] | tuple[str]:
        assert self.path is not None
        try:
            info = self.path.stat()
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
            try:
                self._principals = load_keyring(self.path)
                self._error = None
                logger.info(
                    "Loaded %d scoped API principals from %s",
                    len(self._principals), self.path,
                )
            except (OSError, KeyringError) as exc:
                self._principals = ()
                self._error = str(exc)
                logger.error("Scoped API keyring unavailable: %s", exc)

    def authenticate(self, token: str) -> AuthenticatedCredential | None:
        self._reload_if_needed()
        match: AuthenticatedCredential | None = None
        # Always inspect every configured secret rather than returning on the
        # first comparison. The keyring is deliberately small and this avoids
        # making credential order an obvious timing oracle.
        for principal in self._principals:
            for credential in principal.credentials:
                if hmac.compare_digest(
                    token.encode("utf-8"), credential.secret.encode("utf-8")
                ):
                    match = AuthenticatedCredential(principal, credential)
        return match

    def status(self) -> dict[str, Any]:
        """Return loader health and counts without principals or key material."""

        self._reload_if_needed()
        with self._lock:
            return {
                "configured": self.configured,
                "available": self.configured and self._error is None,
                "error": self._error,
                "principal_count": len(self._principals),
                "credential_count": sum(
                    len(principal.credentials) for principal in self._principals
                ),
            }


def _audience_person_ids() -> dict[str, str]:
    owner = (
        os.environ.get("COLONY_OWNER_PERSON_ID", "").strip()
        or os.environ.get("COLONY_OWNER_CONTACT_ID", "").strip()
        or "owner"
    )
    return {
        "owner": owner,
        "shared": os.environ.get("COLONY_SHARED_PERSON_ID", "shared").strip() or "shared",
        "global": os.environ.get("COLONY_GLOBAL_PERSON_ID", "global").strip() or "global",
    }


def scoped_authority(
    match: AuthenticatedCredential,
    granted_person_ids: frozenset[str] = frozenset(),
) -> RequestAuthority:
    principal = match.principal
    mapped = _audience_person_ids()
    static_person_ids = set(principal.person_ids)
    if principal.viewer_person_id:
        static_person_ids.add(principal.viewer_person_id)
    for audience in principal.audiences:
        if audience in mapped:
            static_person_ids.add(mapped[audience])
    person_ids = set(static_person_ids)
    if principal.attested_contact_platforms:
        person_ids.update(granted_person_ids)
    return RequestAuthority(
        principal_id=principal.principal_id,
        credential_id=match.credential.credential_id,
        scopes=principal.scopes,
        allow_unscoped_api=principal.allow_unscoped_api,
        viewer_person_id=principal.viewer_person_id,
        person_ids=frozenset(person_ids),
        audiences=principal.audiences,
        authenticated=True,
        static_person_ids=frozenset(static_person_ids),
        turn_ingress_platforms=principal.turn_ingress_platforms,
        attested_contact_platforms=principal.attested_contact_platforms,
        attested_contact_limit=principal.attested_contact_limit,
        worker_grants=principal.worker_grants,
    )


def legacy_authority() -> RequestAuthority:
    return RequestAuthority(
        principal_id="legacy",
        credential_id="COLONY_API_KEY",
        scopes=frozenset({"*"}),
        viewer_person_id=None,
        person_ids=frozenset(),
        audiences=_AUDIENCES,
        authenticated=True,
        legacy=True,
    )


def anonymous_authority() -> RequestAuthority:
    return RequestAuthority(
        principal_id="anonymous-dev",
        credential_id=None,
        scopes=frozenset(),
        viewer_person_id=None,
        person_ids=frozenset(),
        audiences=frozenset(),
        authenticated=False,
        anonymous=True,
    )


def record_attested_contact_grant(
    request: Request | None,
    *,
    platform: str,
    person_id: str,
) -> bool:
    """Project one ParticipantResolver result into exact principal authority.

    This function is called only after the server-side resolver has returned a
    concrete contact. It never reads a body-asserted contact ID and never adds
    an audience lane or scope.
    """

    if request is None:
        return False
    authority = request_authority(request)
    platform = (platform or "").strip().lower()
    person_id = (person_id or "").strip()
    if (
        authority.legacy
        or authority.anonymous
        or not authority.authenticated
        or not authority.has_scope("turns:resolve-sender")
        or platform not in authority.attested_contact_platforms
        or authority.attested_contact_limit <= 0
        or not person_id
    ):
        return False
    registry = getattr(request.state, "colony_contact_grants", None)
    if registry is None:
        return False
    try:
        return bool(registry.grant(
            authority.principal_id,
            person_id,
            max_person_ids=authority.attested_contact_limit,
        ))
    except Exception:
        logger.warning(
            "Failed to persist server-attested contact grant for principal %s",
            authority.principal_id,
            exc_info=True,
        )
        return False


def _authority_error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=403, detail={"code": code, "message": message})


def request_authority(request: Request | None) -> RequestAuthority:
    if request is None:
        # Direct in-process router calls predate HTTP authority middleware and
        # are trusted like other internal method calls. Actual HTTP dev-mode
        # requests always carry the explicit anonymous authority below.
        return legacy_authority()
    value = getattr(request.state, "colony_authority", None)
    if isinstance(value, RequestAuthority):
        return value
    # Routers are mounted without middleware in a number of focused unit tests.
    # Treat that path exactly like loopback dev mode, never like owner authority.
    return anonymous_authority()


def resolve_request_person(
    request: Request | None,
    *,
    claimed_person_id: str | None = None,
    context_person_id: str | None = None,
    audience: str | None = None,
) -> str | None:
    """Resolve one exact person/audience lane from authenticated authority."""

    authority = request_authority(request)
    claimed = (claimed_person_id or "").strip() or None
    context = (context_person_id or "").strip() or None
    if claimed and context and claimed != context:
        raise _authority_error(
            "person_scope_conflict",
            "person_id and context.contact_id must identify the same person",
        )

    audience = (audience or "").strip().lower() or None
    if audience and audience not in _AUDIENCES:
        raise _authority_error("invalid_audience", "unknown audience lane")

    mapped = _audience_person_ids()
    lane_person: str | None = None
    if audience:
        if authority.anonymous:
            raise _authority_error(
                "audience_not_granted",
                "anonymous dev mode cannot select authenticated audience lanes",
            )
        if not authority.legacy and audience not in authority.audiences:
            raise _authority_error(
                "audience_not_granted",
                f"principal {authority.principal_id!r} is not granted audience {audience!r}",
            )
        lane_person = (
            authority.viewer_person_id if audience == "viewer" else mapped[audience]
        )
        if not lane_person:
            raise _authority_error(
                "audience_unbound", "the selected audience has no person binding"
            )
        body_person = claimed or context
        if body_person and body_person != lane_person:
            raise _authority_error(
                "person_scope_conflict",
                "body person does not match the selected audience lane",
            )

    target = lane_person or claimed or context
    if authority.legacy:
        # Compatibility carve-out: the old global bearer keeps its historical
        # body-selected/no-filter behavior until all consumers are migrated.
        return target

    if authority.anonymous:
        target = target or (
            os.environ.get("COLONY_DEV_PERSON_ID", "dev-anonymous").strip()
            or "dev-anonymous"
        )
        reserved = set(mapped.values())
        if target in reserved:
            raise _authority_error(
                "reserved_authority_required",
                "owner, shared, and global memory require an authenticated principal",
            )
        return target

    target = target or authority.viewer_person_id
    if not target:
        raise _authority_error(
            "person_binding_required",
            "this scoped principal has no default viewer/person binding",
        )
    if target not in authority.person_ids:
        raise _authority_error(
            "person_scope_not_granted",
            f"principal {authority.principal_id!r} is not granted person {target!r}",
        )
    return target


def resolve_turn_person(
    request: Request | None,
    *,
    context_person_id: str | None,
    has_sender: bool,
) -> str | None:
    """Authorize a turn's initial contact before idempotency reservation.

    Most principals must submit their exact bound viewer/person. A transport
    adapter may additionally receive ``turns:resolve-sender``; for a structured
    sender envelope Colony then discards the body contact claim, starts from the
    principal's viewer binding, and lets ``ParticipantResolver`` establish the
    human contact server-side inside turn processing.
    """

    authority = request_authority(request)
    if (
        has_sender
        and not authority.legacy
        and not authority.anonymous
        and authority.has_scope("turns:resolve-sender")
    ):
        if not authority.viewer_person_id:
            raise _authority_error(
                "person_binding_required",
                "sender-resolving principals need a default viewer/person binding",
            )
        return authority.viewer_person_id
    return resolve_request_person(
        request,
        context_person_id=context_person_id,
    )


def query_person_is_granted(authority: RequestAuthority, person_id: str) -> bool:
    """Validate legacy query-string person selectors without trusting them."""

    target = (person_id or "").strip()
    if not target or authority.legacy:
        return True
    if authority.anonymous:
        return target not in set(_audience_person_ids().values())
    return target in authority.person_ids


def worker_authority_mode() -> str:
    """Return the worker HTTP authority migration posture.

    ``shadow`` preserves legacy/global-bearer consumers while recording the
    exact posture that enforcement would require.  ``enforce`` accepts only a
    scoped principal with an exact worker grant.  Unknown values are exposed
    as ``invalid`` so handlers return an operator-fixable 503 rather than
    silently choosing a weaker policy.
    """

    value = os.environ.get("COLONY_WORKER_AUTHORITY_MODE", "shadow").strip().lower()
    return value if value in {"shadow", "enforce"} else "invalid"


def compatible_scopes(method: str, path: str) -> frozenset[str]:
    """Return deliberate legacy scope aliases for one exact route.

    Compatibility never makes ``api:access`` a global parent of focused
    scopes.  Middleware also applies ``allow_unscoped_api`` before accepting
    this route-local alias.
    """

    if (method.upper(), path) in WORK_READ_SURFACE_V1:
        return _API_ACCESS_COMPATIBILITY
    return _NO_COMPATIBILITY_SCOPES


def required_scope(method: str, path: str) -> str:
    """Return the exact scope required for a scoped credential."""

    key = (method.upper(), path)
    if key in WORK_READ_SURFACE_V1:
        return "work:read"
    if method.upper() == "POST" and path in {
        "/v1/host/contact-policy/standing",
        "/v1/host/contact-policy/provision",
    }:
        return "contacts:policy-write"
    if path.startswith("/v1/host/actions/"):
        if method.upper() == "PUT":
            return "actions:execute"
        if method.upper() == "GET":
            return "actions:verify"
    worker_mode = worker_authority_mode()
    if (
        method.upper() == "POST"
        and path == "/v1/host/queue/work/reconciliations"
    ):
        return "workers:attest"
    if (
        path == "/v1/host/queue/work"
        or path.startswith("/v1/host/queue/work/")
    ):
        return "work:read" if method.upper() == "GET" else "work:control"
    if (
        path.startswith("/v1/host/queue/workers/")
        and "/controls" in path
    ):
        return "workers:lifecycle"
    if (
        method.upper() == "POST"
        and path.startswith("/v1/host/queue/attestations/jobs/")
    ):
        return "workers:attest"
    if worker_mode in {"enforce", "invalid"} and method.upper() == "POST":
        if path == "/v1/host/queue/workers/register" or (
            path.startswith("/v1/host/queue/workers/")
            and path.endswith("/deregister")
        ):
            return "workers:register"
        if path == "/v1/host/queue/jobs/claim":
            return "workers:claim"
        if (
            path.startswith("/v1/host/queue/workers/")
            and path.endswith("/heartbeat")
        ) or (
            path.startswith("/v1/host/queue/jobs/")
            and path.endswith((
                "/start", "/complete", "/fail", "/heartbeat", "/release",
            ))
        ):
            return "workers:lifecycle"
    if (
        method.upper() == "GET"
        and path.startswith("/v1/host/queue/inspection/jobs/")
    ):
        return "workers:inspect"
    # Approval routes always have stable exact scopes so restricted scoped
    # principals can canary them in shadow without gaining generic api:access
    # fallback. Shadow may still accept the legacy bearer; enforce additionally
    # requires an exact scoped principal in middleware.
    if path.startswith("/v1/host/queue/approvals/"):
        if method.upper() == "GET":
            return "approvals:read"
        if method.upper() == "DELETE":
            return "approvals:manage"
        return "approvals:decide"
    if (
        method.upper() == "GET"
        and path == "/v1/host/queue/jobs/blocked"
    ):
        return "approvals:read"
    if (
        path.startswith("/v1/host/queue/jobs/")
        and path.endswith(("/approve", "/reject"))
    ):
        return "approvals:decide"
    if (
        method.upper() == "POST"
        and path.startswith("/v1/host/self/tools/")
        and path.endswith("/graduate")
    ):
        return "toolsmith:graduate"
    if (
        method.upper() == "POST"
        and path.startswith("/v1/host/self/tools/")
        and path.endswith("/shadow-compare")
    ):
        return "toolsmith:evaluate"
    if method.upper() == "POST" and path == "/v1/host/sandbox/run":
        return "sandbox:execute"
    if (
        method.upper() == "POST"
        and path.startswith("/v1/host/cognition/charters/")
        and path.endswith(("/request-activation", "/request-revocation"))
    ):
        return "charter:request"
    if (
        method.upper() == "POST"
        and path.startswith("/v1/host/cognition/charters/")
        and path.endswith("/ratify")
    ):
        return "charter:ratify"
    if path.startswith(
        "/v1/host/cognition/charter-transition-approvals"
    ):
        if method.upper() == "GET":
            return "charter:approval-read"
        if method.upper() == "POST" and path.endswith("/decision"):
            return "charter:approval-decide"
        return "api:access"
    exact = {
        ("GET", "/v1/host/admin/auth/status"): "auth:admin",
        ("GET", "/v1/host/contact-policy"): "contacts:policy-read",
        ("GET", "/v1/host/queue/contract"): "workers:contract",
        ("POST", "/v1/host/memory/read"): "memory:read",
        ("POST", "/v1/host/memory/search"): "memory:search",
        ("POST", "/v1/host/memory/write"): "memory:write",
        ("POST", "/v1/host/memory/sources/forget"): "memory:write",
        ("GET", "/v1/host/memory/sources/erasures"): "turns:write",
        ("POST", "/v1/host/context/assemble"): "context:read",
        ("GET", "/v1/host/executions"): "context:read",
        ("POST", "/v1/host/executions/observe"): "turns:write",
        ("GET", "/v1/host/context/projection-readiness"): "context:read",
        ("POST", "/v1/host/context/enriched"): "context:read",
        ("GET", "/v1/host/context/temporal"): "context:read",
        ("POST", "/v1/host/turns/sync"): "turns:write",
        ("GET", "/v1/host/events/replay"): "events:read",
        ("GET", "/v1/host/self/workspace"): "cognition:read",
        ("GET", "/v1/host/self/benchmark"): "cognition:benchmark-read",
        ("POST", "/v1/host/self/benchmark/samples"):
            "cognition:benchmark-manage",
        ("POST", "/v1/host/self/benchmark/recall-probe"):
            "cognition:benchmark-manage",
        ("POST", "/v1/host/self/benchmark/compute"):
            "cognition:benchmark-manage",
        ("GET", "/v1/host/self/experiments"):
            "cognition:experiment-read",
        ("POST", "/v1/host/self/experiments"):
            "cognition:experiment-manage",
        ("GET", "/v1/host/self/params"): "cognition:experiment-read",
        ("GET", "/v1/host/cognition/cpi"): "cognition:benchmark-read",
        ("POST", "/v1/host/cognition/cycle"):
            "cognition:benchmark-manage",
        ("GET", "/v1/host/self/situation"): "cognition:read",
        ("GET", "/v1/host/autonomy/posture"): "cognition:read",
        ("GET", "/v1/host/autonomy/schedule"): "cognition:read",
        ("GET", "/v1/host/autonomy/status"): "cognition:read",
        ("GET", "/v1/host/cognition/evidence"): "cognition:read",
        ("GET", "/v1/host/cognition/drives"): "drives:read",
        ("GET", "/v1/host/cognition/charters"): "charter:read",
        ("GET", "/v1/host/cognition/rankings"): "drives:read",
        ("GET", "/v1/host/cognition/spine"): "cognition:read",
        ("POST", "/v1/host/cognition/events"): "cognition:events-ingest",
        ("GET", "/v1/host/proposals"): "cognition:read",
        ("GET", "/v1/host/tom/p8/status"): "tom:read",
        ("GET", "/v1/host/tom/p8/deck"): "tom:read",
        ("GET", "/v1/host/tom2/report"): "tom:read",
        ("POST", "/v1/host/cognition/drives"): "drives:propose",
        ("POST", "/v1/host/cognition/drive-signals"): "drives:signal",
        ("POST", "/v1/host/cognition/charters"): "charter:propose",
    }
    if key in exact:
        return exact[key]
    if method.upper() == "PUT" and path.startswith("/v2/host/turns/"):
        return "turns:write"
    if (
        method.upper() == "POST"
        and path.startswith("/v1/host/cognition/concerns/")
        and path.endswith("/promote")
    ):
        return "cognition:manage"
    if (
        method.upper() == "POST"
        and path.startswith("/v1/host/cognition/goals/")
        and path.endswith("/promote")
    ):
        return "cognition:manage"
    if (
        method.upper() == "GET"
        and path.startswith("/v1/host/self/workspace/")
        and path.endswith("/resolution")
    ):
        return "cognition:read"
    if (
        method.upper() == "POST"
        and path.startswith("/v1/host/self/workspace/")
        and path.endswith("/resolve")
    ):
        return "cognition:manage"
    if path.startswith("/v1/host/self/experiments/"):
        if method.upper() == "GET":
            return "cognition:experiment-read"
        return "cognition:experiment-manage"
    return "api:access"
