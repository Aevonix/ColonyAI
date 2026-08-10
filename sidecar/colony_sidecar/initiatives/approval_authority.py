"""Durable, transport-neutral authority for Colony action approvals.

Colony owns approval *state* but deliberately knows nothing about WhatsApp,
SMS, an operator deck, or any other decision transport.  A host authenticates a
principal at the API boundary and supplies that server-derived principal to
this store.  Caller prose such as ``approved_by`` is never authority.

The store provides two related primitives:

* An ``ApprovalRequest`` binds one queued job to an immutable action digest.
  The first valid decision wins; expired or superseded requests cannot be
  revived.
* A bounded grant is an exact action scope with both an expiry and a use cap.
  It replaces the historical permanent, action-name-only standing approval.

SQLite transactions make decision and grant consumption atomic across Colony
workers.  The database lives in ``COLONY_STATE_DIR`` and is safe to copy with
the rest of Colony state for rollback.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, Iterator, Mapping, Optional
import uuid

from colony_sidecar import get_state_dir


DB_FILENAME = "approval_authority.db"
DEFAULT_REQUEST_TTL_SECONDS = 24 * 60 * 60
DEFAULT_GRANT_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_GRANT_MAX_USES = 5
MAX_GRANT_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_GRANT_USES = 100
PRESENTATION_SCHEMA = "ColonyApprovalPresentationV1"
AUTHORIZATION_PROJECTION_SCHEMA = "ColonyApprovalAuthorizationProjectionV1"
TYPED_APPROVAL_SUBJECT_SCHEMA = "ColonyApprovalSubjectBindingV1"

_DECISION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{7,191}$")
_ACTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,191}$")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|bearer|credential|password|secret|token)"
    r"\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_PEM_PRIVATE_KEY_BEGIN = re.compile(
    r"-----BEGIN (?P<label>(?:[A-Z0-9]+ )*PRIVATE KEY)-----",
    re.IGNORECASE,
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SUBJECT_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ApprovalAuthorityError(ValueError):
    """A stable, API-safe approval error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ActionBinding:
    """Server-derived immutable identity and reusable exact scope."""

    action_digest: str
    scope: Dict[str, Any]
    scope_digest: str


@dataclass(frozen=True)
class ApprovalSubjectBinding:
    """Server-derived identity for a non-queue approval subject.

    The authority store remains domain-neutral. Domain services opt in by
    attaching an immutable kind/id/revision/action tuple when the request is
    born; an existing request can never be relabelled as a typed subject.
    """

    kind: str
    subject_id: str
    revision: str
    action: str

    def payload(self) -> Dict[str, str]:
        return _validated_subject({
            "kind": self.kind,
            "subject_id": self.subject_id,
            "revision": self.revision,
            "action": self.action,
        })


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> datetime:
    result = value or _utcnow()
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _canonical(value: Any) -> str:
    """Canonical JSON used only for digests and bounded public metadata."""

    def default(item: Any) -> str:
        if isinstance(item, datetime):
            return _iso(item)
        return str(item)

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=default,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _validated_subject(value: Mapping[str, Any]) -> Dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "kind", "subject_id", "revision", "action",
    }:
        raise ApprovalAuthorityError(
            "invalid_subject_binding",
            "approval subject binding fields are invalid",
        )
    kind = str(value.get("kind") or "").strip()
    subject_id = str(value.get("subject_id") or "").strip()
    revision = str(value.get("revision") or "").strip()
    action = str(value.get("action") or "").strip().lower()
    if not _SUBJECT_KIND_RE.fullmatch(kind):
        raise ApprovalAuthorityError(
            "invalid_subject_binding", "approval subject kind is invalid",
        )
    for field, text in (
        ("subject_id", subject_id), ("revision", revision),
    ):
        if (
            not text or len(text) > 256 or _CONTROL.search(text)
            or not _ACTION_RE.fullmatch(text)
        ):
            raise ApprovalAuthorityError(
                "invalid_subject_binding",
                f"approval subject {field} is invalid",
            )
    if not action or not _ACTION_RE.fullmatch(action):
        raise ApprovalAuthorityError(
            "invalid_subject_binding", "approval subject action is invalid",
        )
    return {
        "kind": kind,
        "subject_id": subject_id,
        "revision": revision,
        "action": action,
    }


def approval_subject_digest(
    subject: Mapping[str, Any],
    *,
    action_digest: str,
    scope_digest: str,
    presentation_digest: str,
) -> str:
    """Bind a typed subject to the exact action and owner presentation."""

    return _digest({
        "version": 1,
        "schema": TYPED_APPROVAL_SUBJECT_SCHEMA,
        "subject": _validated_subject(subject),
        "action_digest": str(action_digest),
        "scope_digest": str(scope_digest),
        "presentation_digest": str(presentation_digest),
    })


def _nested_value(payload: Mapping[str, Any], key: str) -> Any:
    if key in payload:
        return payload.get(key)
    context = payload.get("context")
    if isinstance(context, Mapping):
        if key in context:
            return context.get(key)
        upper = key.upper()
        if upper in context:
            return context.get(upper)
    return None


def _redact_pem_private_keys(value: str) -> str:
    """Remove bounded PEM private-key blocks without scanning them greedily."""

    text = value
    cursor = 0
    for _ in range(32):
        match = _PEM_PRIVATE_KEY_BEGIN.search(text, cursor)
        if match is None:
            return text
        end_pattern = re.compile(
            re.escape(f"-----END {match.group('label')}-----"),
            re.IGNORECASE,
        )
        end = end_pattern.search(text, match.end())
        end_offset = end.end() if end is not None else len(text)
        text = (
            text[:match.start()]
            + "[REDACTED PRIVATE KEY]"
            + text[end_offset:]
        )
        cursor = match.start() + len("[REDACTED PRIVATE KEY]")
        if end is None:
            return text
    # An adversarial description with more than 32 PEM blocks is not useful
    # owner-facing content. Preserve its safe prefix and redact the remainder.
    overflow = _PEM_PRIVATE_KEY_BEGIN.search(text, cursor)
    if overflow is not None:
        text = text[:overflow.start()] + "[REDACTED PRIVATE KEY MATERIAL]"
    return text


def _safe_display_text(value: Any, *, maximum: int, fallback: str = "") -> str:
    """Return bounded single-line owner display text with common secrets removed."""

    text = _redact_pem_private_keys(str(value or ""))
    text = _CONTROL.sub(" ", text)
    text = _SECRET_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}=[REDACTED]", text,
    )
    text = _BEARER_VALUE.sub("Bearer [REDACTED]", text)
    text = " ".join(text.split())
    if not text:
        text = fallback
    return text[:maximum]


def approval_binding_digest(
    *,
    job_id: str,
    job_type: str,
    action_digest: str,
    scope_digest: str,
) -> str:
    return _digest({
        "version": 1,
        "job_id": str(job_id),
        "job_type": str(job_type),
        "action_digest": str(action_digest),
        "scope_digest": str(scope_digest),
    })


def approval_presentation_digest(presentation: Mapping[str, Any]) -> str:
    return _digest(_validated_presentation(presentation))


def _validated_presentation(value: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ApprovalAuthorityError(
            "invalid_presentation", "approval presentation must be an object"
        )
    expected = {
        "schema", "version", "summary", "action_name", "risk", "effect",
        "target", "capabilities", "deadline", "reversibility", "constraints",
    }
    if set(value) != expected:
        raise ApprovalAuthorityError(
            "invalid_presentation", "approval presentation fields are invalid"
        )
    if value.get("schema") != PRESENTATION_SCHEMA or value.get("version") != 1:
        raise ApprovalAuthorityError(
            "invalid_presentation", "approval presentation schema is invalid"
        )
    capabilities = value.get("capabilities")
    constraints = value.get("constraints")
    if (
        not isinstance(capabilities, list)
        or len(capabilities) > 32
        or not all(
            isinstance(item, str) and 0 < len(item) <= 128
            for item in capabilities
        )
        or capabilities != sorted(set(capabilities))
        or not isinstance(constraints, Mapping)
        or len(constraints) > 32
    ):
        raise ApprovalAuthorityError(
            "invalid_presentation", "approval presentation bounds are invalid"
        )
    normalized_constraints: Dict[str, Optional[str]] = {}
    for key, digest in constraints.items():
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 128
            or (
                digest is not None
                and (
                    not isinstance(digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)
                )
            )
        ):
            raise ApprovalAuthorityError(
                "invalid_presentation", "approval presentation constraint is invalid"
            )
        normalized_constraints[key] = digest
    result = {
        "schema": PRESENTATION_SCHEMA,
        "version": 1,
        "summary": _safe_display_text(value.get("summary"), maximum=1200),
        "action_name": _safe_display_text(
            value.get("action_name"), maximum=192,
        ),
        "risk": _safe_display_text(value.get("risk"), maximum=64),
        "effect": _safe_display_text(value.get("effect"), maximum=32),
        "target": _safe_display_text(value.get("target"), maximum=512),
        "capabilities": list(capabilities),
        "deadline": _safe_display_text(value.get("deadline"), maximum=64),
        "reversibility": _safe_display_text(
            value.get("reversibility"), maximum=256, fallback="unspecified",
        ),
        "constraints": dict(sorted(normalized_constraints.items())),
    }
    if not result["summary"] or not result["action_name"] or not result["risk"]:
        raise ApprovalAuthorityError(
            "invalid_presentation", "approval presentation is incomplete"
        )
    if len(_canonical(result).encode("utf-8")) > 16 * 1024:
        raise ApprovalAuthorityError(
            "invalid_presentation", "approval presentation is too large"
        )
    return result


def build_approval_presentation(
    *,
    job_id: str,
    job_type: str,
    payload: Mapping[str, Any],
    deadline: Any = None,
) -> Dict[str, Any]:
    """Build the transport-neutral, redacted view an owner may approve.

    Raw payloads and context references never enter this projection. Exact
    action constraints are represented only by their server-derived digests.
    """

    binding = build_action_binding(
        job_id=job_id,
        job_type=job_type,
        payload=payload,
    )
    action_name = binding.scope["action_name"]
    if payload.get("schema") == "ApprovalRelayCanaryV1":
        from colony_sidecar.task_queue.approval_relay_canary import (
            ACTION_HINT as CANARY_ACTION_HINT,
            job_id_for_digest,
            payload_for_digest,
        )

        digest = payload.get("idempotency_digest")
        exact_canary = bool(
            isinstance(digest, str)
            and action_name == CANARY_ACTION_HINT
            and str(job_id) == job_id_for_digest(digest)
            and dict(payload) == payload_for_digest(digest)
        )
        if not exact_canary:
            raise ApprovalAuthorityError(
                "invalid_action_scope",
                "approval relay canary presentation identity drifted",
            )
        return _validated_presentation({
            "schema": PRESENTATION_SCHEMA,
            "version": 1,
            "summary": (
                "Calibrate the owner approval relay. Either choice records "
                "the decision and permanently cancels this no-effect job."
            ),
            "action_name": action_name,
            "risk": "calibration",
            "effect": "none",
            "target": "internal approval relay",
            "capabilities": [],
            "deadline": _safe_display_text(
                deadline, maximum=64, fallback="unspecified",
            ),
            "reversibility": "no execution; terminal cancellation only",
            "constraints": binding.scope.get("constraints") or {},
        })
    risk = str(binding.scope.get("risk") or "unknown")
    effect = "unknown"
    if risk in {"mutation", "mutating", "destructive"}:
        effect = "mutation"
    elif risk in {"disclosure", "outbound"}:
        effect = "disclosure"
    elif risk in {"read_only", "internal"}:
        effect = "none"

    summary = (
        payload.get("objective")
        or payload.get("description")
        or payload.get("request")
        or action_name
    )
    target = payload.get("recipient_scope")
    if target in (None, ""):
        try:
            from colony_sidecar.initiatives.action_registry import get_action

            spec = get_action(action_name)
            target = _nested_value(payload, spec.target_param) if spec and spec.target_param else None
        except Exception:
            target = None
    if target in (None, ""):
        for name in ("recipient", "target", "to"):
            target = _nested_value(payload, name)
            if target not in (None, ""):
                break

    raw_capabilities = payload.get("capability_allowlist") or ()
    capabilities = sorted({
        _safe_display_text(item, maximum=128)
        for item in raw_capabilities
        if _safe_display_text(item, maximum=128)
    })[:32]
    reversibility = (
        payload.get("reversibility")
        or ("reversible" if payload.get("reversible") is True else None)
        or ("irreversible" if payload.get("reversible") is False else None)
        or "unspecified"
    )
    return _validated_presentation({
        "schema": PRESENTATION_SCHEMA,
        "version": 1,
        "summary": _safe_display_text(summary, maximum=1200, fallback=action_name),
        "action_name": action_name,
        "risk": risk,
        "effect": effect,
        "target": _safe_display_text(target, maximum=512, fallback="unspecified"),
        "capabilities": capabilities,
        "deadline": _safe_display_text(
            deadline or payload.get("deadline"), maximum=64, fallback="unspecified",
        ),
        "reversibility": reversibility,
        "constraints": binding.scope.get("constraints") or {},
    })


def _request_ttl_for_deadline(
    deadline: Any,
    *,
    now: Optional[datetime] = None,
) -> int:
    """Return a request lifetime that never outlives the queue deadline."""

    observed = _as_utc(now)
    if deadline in (None, ""):
        return DEFAULT_REQUEST_TTL_SECONDS
    if isinstance(deadline, datetime):
        parsed = _as_utc(deadline)
    else:
        try:
            parsed = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return DEFAULT_REQUEST_TTL_SECONDS
        parsed = _as_utc(parsed)
    remaining = int((parsed - observed).total_seconds())
    if remaining < 60:
        return 0
    return min(DEFAULT_REQUEST_TTL_SECONDS, remaining)


def canonical_approval_timeout_seconds() -> int:
    """One timeout used by request creation and blocked-job governance."""

    raw = os.environ.get("COLONY_APPROVAL_TIMEOUT_HOURS")
    if raw in (None, ""):
        return DEFAULT_REQUEST_TTL_SECONDS
    try:
        seconds = int(float(raw) * 60 * 60)
    except (TypeError, ValueError):
        return DEFAULT_REQUEST_TTL_SECONDS
    return max(60, min(seconds, MAX_GRANT_TTL_SECONDS))


def prepare_action_approval(
    store: "ApprovalAuthorityStore",
    *,
    job_id: str,
    job_type: str,
    payload: Mapping[str, Any],
    deadline: Any = None,
    approval_started_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Resolve one server-owned effect gate without any transport coupling.

    The caller must keep the queue row unclaimable until it durably applies the
    returned state/tags. Repeating this function reuses the exact request or
    grant-use record, which closes restart and partial-transition gaps.
    """

    binding = build_action_binding(
        job_id=job_id,
        job_type=job_type,
        payload=payload,
    )
    presentation = build_approval_presentation(
        job_id=job_id,
        job_type=job_type,
        payload=payload,
        deadline=deadline,
    )
    presentation_digest = _digest(presentation)
    common = {
        "action_digest": binding.action_digest,
        "approval_scope_digest": binding.scope_digest,
        "approval_presentation_digest": presentation_digest,
    }

    def request_result(request: Mapping[str, Any]) -> Dict[str, Any]:
        request_tags = {
            **common,
            "approval_request_id": request["request_id"],
            "approval_binding_digest": request["binding_digest"],
            "approval_request_digest": request["request_digest"],
            "approval_expires_at": request["expires_at"],
        }
        if request["status"] == "approved" and request.get("decision") == "approve":
            state = "authorized_direct"
            request_tags.update({
                "approval_provenance": "server_direct_decision",
                "approval_decision_id": request["decision_id"],
                "approved_by": request["decided_by"],
                "approved_at": request["decided_at"],
            })
        elif request["status"] == "rejected":
            state = "rejected"
            request_tags.update({
                "approval_provenance": "server_direct_decision",
                "approval_decision_id": request["decision_id"],
                "rejected_by": request["decided_by"],
                "rejected_at": request["decided_at"],
            })
        elif request["status"] in {"expired", "superseded"}:
            state = request["status"]
        else:
            state = "pending"
        return {
            "state": state,
            "binding": binding,
            "presentation": presentation,
            "request": dict(request),
            "grant_use": None,
            "tags": request_tags,
        }

    observed = _as_utc(now)
    effective_deadline = deadline
    if approval_started_at is not None:
        approval_deadline = _as_utc(approval_started_at) + timedelta(
            seconds=canonical_approval_timeout_seconds()
        )
        if effective_deadline in (None, ""):
            effective_deadline = approval_deadline
        else:
            try:
                parsed_deadline = (
                    _as_utc(effective_deadline)
                    if isinstance(effective_deadline, datetime)
                    else _as_utc(datetime.fromisoformat(
                        str(effective_deadline).replace("Z", "+00:00")
                    ))
                )
            except (TypeError, ValueError):
                parsed_deadline = approval_deadline
            effective_deadline = min(parsed_deadline, approval_deadline)
    ttl_seconds = _request_ttl_for_deadline(effective_deadline, now=observed)
    resolved = store.resolve_action_gate(
        job_id=job_id,
        binding=binding,
        operation_id=job_id,
        ttl_seconds=ttl_seconds,
        presentation=presentation,
        now=observed,
    )
    if resolved["kind"] == "request":
        # Reconciliation preserves the original exact request and expiry.
        # A durable request (including a rejection or projected expiry) always
        # wins over a reusable grant for the same operation.
        return request_result(resolved["request"])
    if resolved["kind"] == "deadline_expired":
        return {
            "state": "expired",
            "binding": binding,
            "presentation": presentation,
            "request": None,
            "grant_use": None,
            "tags": {
                **common,
                "approval_provenance": "server_deadline_expired",
            },
        }
    if resolved["kind"] != "grant":
        raise ApprovalAuthorityError(
            "approval_resolution_invalid",
            "approval gate returned an invalid authority state",
        )
    use = resolved["grant_use"]
    if use is None:
        raise ApprovalAuthorityError(
            "grant_provenance_missing",
            "bounded grant consumption has no durable use record",
        )
    return {
        "state": "authorized_grant",
        "binding": binding,
        "presentation": presentation,
        "request": None,
        "grant_use": use,
        "tags": {
            **common,
            "auto_approved_by_policy": "bounded_grant",
            "approval_provenance": "server_bounded_grant",
            "bounded_grant_id": use["grant_id"],
            "approval_source_request_id": use["source_request_id"],
            "approval_decision_id": use["decision_id"],
            "approved_by": use["granted_by"],
            "approved_at": use["consumed_at"],
            "bounded_grant_expires_at": use["grant_expires_at"],
        },
    }


def build_action_binding(
    *,
    job_id: str,
    job_type: str,
    payload: Mapping[str, Any],
) -> ActionBinding:
    """Derive an immutable job digest and an exact reusable grant scope.

    Scope is based solely on the registered action contract and hashes of its
    required values.  Raw recipient, message, path, and repository values are
    therefore not copied into the authority database.  Missing constraints are
    represented explicitly, so a later request cannot broaden an approval by
    dropping a field.
    """

    action_name = str(payload.get("action_hint") or "").strip()
    if not action_name or not _ACTION_RE.fullmatch(action_name):
        raise ApprovalAuthorityError(
            "invalid_action_scope", "job has no valid registered action name"
        )
    normalized_job_type = str(job_type or "").strip()
    if not normalized_job_type:
        raise ApprovalAuthorityError("invalid_action_scope", "job_type is required")

    required: list[str] = []
    risk = str(payload.get("risk") or payload.get("risk_class") or "unknown").strip()
    try:
        from colony_sidecar.initiatives.action_registry import get_action

        spec = get_action(action_name)
        if spec is not None:
            required = list(spec.required_params)
            if spec.target_param and spec.target_param not in required:
                required.append(spec.target_param)
            risk = spec.risk.value
    except Exception:
        # WorkOrder and extension actions can be registered by the host rather
        # than Colony's built-in registry. Their exact action/risk/type tuple is
        # still bounded; the complete payload remains bound by action_digest.
        pass

    if payload.get("schema") == "WorkOrderV1" and payload.get("version") == 1:
        # WorkOrder actions are host extension actions rather than built-in
        # registry entries. Their reusable scope must still bind the project,
        # recipient, source and exact capability ceiling. Step/job identity is
        # intentionally excluded so a bounded multi-use grant can authorize a
        # later step only inside that same scope.
        required = [
            "source", "project_id", "recipient_scope", "capability_allowlist",
        ]
        risk = str(payload.get("risk_class") or risk or "unknown").strip()

    constraints: Dict[str, Optional[str]] = {}
    for name in sorted(set(required)):
        raw = _nested_value(payload, name)
        constraints[name] = _digest(raw) if raw is not None else None

    scope = {
        "version": 1,
        "job_type": normalized_job_type,
        "action_name": action_name,
        "risk": risk or "unknown",
        "constraints": constraints,
    }
    snapshot = {
        "version": 1,
        "job_id": str(job_id),
        "job_type": normalized_job_type,
        "payload": dict(payload),
    }
    return ActionBinding(
        action_digest=_digest(snapshot),
        scope=scope,
        scope_digest=_digest(scope),
    )


def legacy_action_binding(
    action_name: str,
    *,
    operation_id: str,
    job_type: str = "legacy_action",
    risk: str = "mutating",
) -> ActionBinding:
    """Build a bounded scope for older non-queue action gates."""

    payload = {"action_hint": action_name, "risk": risk}
    return build_action_binding(
        job_id=operation_id,
        job_type=job_type,
        payload=payload,
    )


class ApprovalAuthorityStore:
    """Transactional approval and bounded-grant ledger."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(db_path) if db_path is not None else get_state_dir() / DB_FILENAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch(mode=0o600)
        else:
            os.chmod(self.path, 0o600)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=30.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            # sqlite3.Connection.__exit__ commits or rolls back but does not
            # close. Preserve its transaction semantics and own the resource
            # lifetime explicitly so repeated approval projections cannot
            # exhaust the process descriptor limit.
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS approval_requests (
                    request_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    action_digest TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    scope_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    superseded_by TEXT,
                    decision TEXT,
                    decision_id TEXT UNIQUE,
                    decided_at TEXT,
                    decided_by TEXT,
                    authority_evidence TEXT,
                    grant_id TEXT,
                    presentation_json TEXT,
                    presentation_digest TEXT,
                    subject_kind TEXT,
                    subject_id TEXT,
                    subject_revision TEXT,
                    subject_action TEXT,
                    subject_digest TEXT
                );
                CREATE INDEX IF NOT EXISTS approval_requests_job_idx
                    ON approval_requests(job_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS approval_requests_status_idx
                    ON approval_requests(status, expires_at);

                CREATE TABLE IF NOT EXISTS bounded_grants (
                    grant_id TEXT PRIMARY KEY,
                    source_request_id TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    scope_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    max_uses INTEGER NOT NULL,
                    uses INTEGER NOT NULL DEFAULT 0,
                    granted_by TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    revoked_at TEXT,
                    FOREIGN KEY(source_request_id)
                        REFERENCES approval_requests(request_id)
                );
                CREATE INDEX IF NOT EXISTS bounded_grants_scope_idx
                    ON bounded_grants(scope_digest, status, expires_at);

                CREATE TABLE IF NOT EXISTS bounded_grant_uses (
                    action_digest TEXT PRIMARY KEY,
                    grant_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    consumed_at TEXT NOT NULL,
                    FOREIGN KEY(grant_id) REFERENCES bounded_grants(grant_id)
                );
                """
            )
            columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(approval_requests)"
                ).fetchall()
            }
            if "presentation_json" not in columns:
                conn.execute(
                    "ALTER TABLE approval_requests ADD COLUMN presentation_json TEXT"
                )
            if "presentation_digest" not in columns:
                conn.execute(
                    "ALTER TABLE approval_requests ADD COLUMN presentation_digest TEXT"
                )
            subject_columns = {
                "subject_kind": "TEXT",
                "subject_id": "TEXT",
                "subject_revision": "TEXT",
                "subject_action": "TEXT",
                "subject_digest": "TEXT",
            }
            for name, column_type in subject_columns.items():
                if name not in columns:
                    conn.execute(
                        f"ALTER TABLE approval_requests ADD COLUMN {name} "
                        f"{column_type}"
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS approval_requests_subject_idx "
                "ON approval_requests(subject_kind, status, created_at DESC)"
            )
        os.chmod(self.path, 0o600)

    @staticmethod
    def _request_dict(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["scope"] = json.loads(result.pop("scope_json"))
        raw_presentation = result.pop("presentation_json", None)
        if raw_presentation:
            try:
                presentation = _validated_presentation(json.loads(raw_presentation))
            except (ApprovalAuthorityError, TypeError, ValueError) as exc:
                raise ApprovalAuthorityError(
                    "presentation_integrity_failed",
                    "stored approval presentation is invalid",
                ) from exc
        else:
            presentation = None
        if presentation is None:
            presentation = _validated_presentation({
                "schema": PRESENTATION_SCHEMA,
                "version": 1,
                "summary": result["scope"].get("action_name") or "legacy action",
                "action_name": result["scope"].get("action_name") or "legacy_action",
                "risk": result["scope"].get("risk") or "unknown",
                "effect": "unknown",
                "target": "unspecified",
                "capabilities": [],
                "deadline": "unspecified",
                "reversibility": "unspecified",
                "constraints": result["scope"].get("constraints") or {},
            })
        computed_presentation_digest = _digest(presentation)
        stored_presentation_digest = str(result.get("presentation_digest") or "")
        if (
            stored_presentation_digest
            and stored_presentation_digest != computed_presentation_digest
        ):
            raise ApprovalAuthorityError(
                "presentation_integrity_failed",
                "stored approval presentation does not match its digest",
            )
        result["presentation"] = presentation
        result["presentation_digest"] = computed_presentation_digest
        raw_subject = {
            "kind": result.pop("subject_kind", None),
            "subject_id": result.pop("subject_id", None),
            "revision": result.pop("subject_revision", None),
            "action": result.pop("subject_action", None),
        }
        stored_subject_digest = str(result.pop("subject_digest", None) or "")
        populated_subject_fields = [
            value not in (None, "") for value in raw_subject.values()
        ]
        if any(populated_subject_fields) and not all(populated_subject_fields):
            raise ApprovalAuthorityError(
                "subject_integrity_failed",
                "stored approval subject binding is incomplete",
            )
        subject = None
        computed_subject_digest = None
        if all(populated_subject_fields):
            subject = _validated_subject(raw_subject)
            computed_subject_digest = approval_subject_digest(
                subject,
                action_digest=result["action_digest"],
                scope_digest=result["scope_digest"],
                presentation_digest=computed_presentation_digest,
            )
            if stored_subject_digest != computed_subject_digest:
                raise ApprovalAuthorityError(
                    "subject_integrity_failed",
                    "stored approval subject binding does not match its digest",
                )
        elif stored_subject_digest:
            raise ApprovalAuthorityError(
                "subject_integrity_failed",
                "stored approval subject digest has no subject binding",
            )
        result["subject"] = subject
        result["subject_digest"] = computed_subject_digest
        job_type = str(result["scope"].get("job_type") or "")
        result["binding_digest"] = approval_binding_digest(
            job_id=result["job_id"],
            job_type=job_type,
            action_digest=result["action_digest"],
            scope_digest=result["scope_digest"],
        )
        request_digest_version = 2 if subject is not None else 1
        request_digest_payload = {
            "version": request_digest_version,
            "request_id": result["request_id"],
            "job_id": result["job_id"],
            "action_digest": result["action_digest"],
            "scope_digest": result["scope_digest"],
            "binding_digest": result["binding_digest"],
            "presentation_digest": result["presentation_digest"],
            "created_at": result["created_at"],
            "expires_at": result["expires_at"],
        }
        if subject is not None:
            request_digest_payload["subject_digest"] = computed_subject_digest
        result["request_digest"] = _digest(request_digest_payload)
        result["request_digest_version"] = request_digest_version
        return result

    @staticmethod
    def _grant_dict(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["scope"] = json.loads(result.pop("scope_json"))
        return result

    @classmethod
    def _request_view(
        cls,
        row: sqlite3.Row,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        result = cls._request_dict(row)
        if (
            result["status"] == "pending"
            and datetime.fromisoformat(result["expires_at"]) <= _as_utc(now)
        ):
            # Read endpoints project expiry without mutating authority state.
            # Decision and scheduler transactions materialize it durably.
            result["status"] = "expired"
        return result

    def _expire(self, conn: sqlite3.Connection, now: datetime) -> None:
        stamp = _iso(now)
        conn.execute(
            "UPDATE approval_requests SET status='expired' "
            "WHERE status='pending' AND expires_at<=?",
            (stamp,),
        )
        conn.execute(
            "UPDATE bounded_grants SET status='expired' "
            "WHERE status='active' AND expires_at<=?",
            (stamp,),
        )
        conn.execute(
            "UPDATE bounded_grants SET status='exhausted' "
            "WHERE status='active' AND uses>=max_uses",
        )

    def resolve_action_gate(
        self,
        *,
        job_id: str,
        binding: ActionBinding,
        operation_id: str,
        ttl_seconds: int,
        presentation: Mapping[str, Any],
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Atomically choose an existing request, a grant, or a new request.

        Approval state and reusable grants share this SQLite database, so one
        ``BEGIN IMMEDIATE`` transaction can impose the authority ordering that
        the separate queue database cannot: any durable request for this job
        wins before a grant is considered.  If no request exists, exactly one
        concurrent resolver may consume a grant or create the pending request.
        The queue transition remains a separately reconciled operation.
        """

        if ttl_seconds < 0 or ttl_seconds > MAX_GRANT_TTL_SECONDS:
            raise ApprovalAuthorityError(
                "invalid_expiry", "request TTL is out of bounds"
            )
        operation = str(operation_id or "").strip()
        if not operation:
            raise ApprovalAuthorityError(
                "operation_id_required", "operation_id is required"
            )
        normalized_presentation = _validated_presentation(presentation)
        presentation_json = _canonical(normalized_presentation)
        presentation_digest = _digest(normalized_presentation)
        observed = _as_utc(now)
        request_id = "apr_" + uuid.uuid4().hex

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire(conn, observed)
            current = conn.execute(
                "SELECT * FROM approval_requests WHERE job_id=? "
                "ORDER BY created_at DESC, request_id DESC LIMIT 1",
                (str(job_id),),
            ).fetchone()
            if current is not None:
                if (
                    current["action_digest"] != binding.action_digest
                    or current["scope_digest"] != binding.scope_digest
                    or _canonical(json.loads(current["scope_json"]))
                    != _canonical(binding.scope)
                ):
                    conn.rollback()
                    raise ApprovalAuthorityError(
                        "job_binding_conflict",
                        "an approval request already binds this immutable job to another action",
                    )
                stored_presentation_digest = str(
                    current["presentation_digest"] or ""
                )
                if (
                    stored_presentation_digest
                    and stored_presentation_digest != presentation_digest
                ):
                    conn.rollback()
                    raise ApprovalAuthorityError(
                        "presentation_mismatch",
                        "approval presentation changed for an immutable action",
                    )
                if not stored_presentation_digest:
                    conn.execute(
                        "UPDATE approval_requests SET presentation_json=?, "
                        "presentation_digest=? WHERE request_id=? "
                        "AND presentation_digest IS NULL",
                        (
                            presentation_json,
                            presentation_digest,
                            current["request_id"],
                        ),
                    )
                    current = conn.execute(
                        "SELECT * FROM approval_requests WHERE request_id=?",
                        (current["request_id"],),
                    ).fetchone()
                conn.commit()
                assert current is not None
                return {
                    "kind": "request",
                    "request": self._request_dict(current),
                    "grant": None,
                    "grant_use": None,
                }

            # A job whose approval window closed cannot spend a reusable grant.
            # Returning no durable row is safe: the queue writes a terminal
            # state, and a crash simply retries this same deterministic result.
            if ttl_seconds == 0:
                conn.commit()
                return {
                    "kind": "deadline_expired",
                    "request": None,
                    "grant": None,
                    "grant_use": None,
                }

            use_row = conn.execute(
                """SELECT u.action_digest, u.grant_id, u.operation_id,
                          u.consumed_at, g.scope_digest, g.source_request_id,
                          g.granted_by, g.decision_id, g.status AS grant_status,
                          g.created_at AS grant_created_at,
                          g.expires_at AS grant_expires_at,
                          g.max_uses, g.uses
                   FROM bounded_grant_uses AS u
                   JOIN bounded_grants AS g ON g.grant_id = u.grant_id
                   WHERE u.action_digest = ?""",
                (binding.action_digest,),
            ).fetchone()
            if use_row is not None:
                if use_row["operation_id"] != operation:
                    conn.rollback()
                    raise ApprovalAuthorityError(
                        "grant_operation_conflict",
                        "bounded grant use belongs to another operation",
                    )
                conn.commit()
                return {
                    "kind": "grant",
                    "request": None,
                    "grant": None,
                    "grant_use": dict(use_row),
                }

            grant_row = conn.execute(
                "SELECT * FROM bounded_grants WHERE scope_digest=? "
                "AND status='active' AND expires_at>? AND uses<max_uses "
                "ORDER BY expires_at ASC, created_at ASC LIMIT 1",
                (binding.scope_digest, _iso(observed)),
            ).fetchone()
            if grant_row is not None:
                if _canonical(json.loads(grant_row["scope_json"])) != _canonical(
                    binding.scope
                ):
                    conn.rollback()
                    raise ApprovalAuthorityError(
                        "scope_digest_collision", "grant scope mismatch"
                    )
                conn.execute(
                    "INSERT INTO bounded_grant_uses "
                    "(action_digest, grant_id, operation_id, consumed_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        binding.action_digest,
                        grant_row["grant_id"],
                        operation,
                        _iso(observed),
                    ),
                )
                conn.execute(
                    "UPDATE bounded_grants SET uses=uses+1, "
                    "status=CASE WHEN uses+1>=max_uses "
                    "THEN 'exhausted' ELSE status END WHERE grant_id=?",
                    (grant_row["grant_id"],),
                )
                use_row = conn.execute(
                    """SELECT u.action_digest, u.grant_id, u.operation_id,
                              u.consumed_at, g.scope_digest,
                              g.source_request_id, g.granted_by, g.decision_id,
                              g.status AS grant_status,
                              g.created_at AS grant_created_at,
                              g.expires_at AS grant_expires_at,
                              g.max_uses, g.uses
                       FROM bounded_grant_uses AS u
                       JOIN bounded_grants AS g ON g.grant_id = u.grant_id
                       WHERE u.action_digest = ?""",
                    (binding.action_digest,),
                ).fetchone()
                conn.commit()
                assert use_row is not None
                return {
                    "kind": "grant",
                    "request": None,
                    "grant": self._grant_dict(grant_row),
                    "grant_use": dict(use_row),
                }

            conn.execute(
                "INSERT INTO approval_requests "
                "(request_id, job_id, action_digest, scope_json, scope_digest, "
                "status, created_at, expires_at, presentation_json, "
                "presentation_digest) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (
                    request_id,
                    str(job_id),
                    binding.action_digest,
                    _canonical(binding.scope),
                    binding.scope_digest,
                    _iso(observed),
                    _iso(observed + timedelta(seconds=ttl_seconds)),
                    presentation_json,
                    presentation_digest,
                ),
            )
            request_row = conn.execute(
                "SELECT * FROM approval_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            conn.commit()
        assert request_row is not None
        return {
            "kind": "request",
            "request": self._request_dict(request_row),
            "grant": None,
            "grant_use": None,
        }

    def ensure_request(
        self,
        *,
        job_id: str,
        binding: ActionBinding,
        ttl_seconds: int = DEFAULT_REQUEST_TTL_SECONDS,
        presentation: Optional[Mapping[str, Any]] = None,
        subject: Optional[
            ApprovalSubjectBinding | Mapping[str, Any]
        ] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Return the current request, superseding any changed pending one.

        A typed non-queue subject must be supplied when its request is first
        created. It is deliberately not backfilled onto an existing row: that
        would let an orphan or unrelated action be relabelled for a public
        decision surface after the fact.
        """

        if ttl_seconds < 60 or ttl_seconds > MAX_GRANT_TTL_SECONDS:
            raise ApprovalAuthorityError("invalid_expiry", "request TTL is out of bounds")
        observed = _as_utc(now)
        presentation_supplied = presentation is not None
        fallback_presentation = _validated_presentation({
                "schema": PRESENTATION_SCHEMA,
                "version": 1,
                "summary": binding.scope.get("action_name") or "approval required",
                "action_name": binding.scope.get("action_name") or "legacy_action",
                "risk": binding.scope.get("risk") or "unknown",
                "effect": "unknown",
                "target": "unspecified",
                "capabilities": [],
                "deadline": "unspecified",
                "reversibility": "unspecified",
                "constraints": binding.scope.get("constraints") or {},
            })
        if presentation is None:
            normalized_presentation = fallback_presentation
        else:
            normalized_presentation = _validated_presentation(presentation)
        presentation_json = _canonical(normalized_presentation)
        presentation_digest = _digest(normalized_presentation)
        normalized_subject: Optional[Dict[str, str]] = None
        subject_digest: Optional[str] = None
        if subject is not None:
            normalized_subject = (
                subject.payload()
                if isinstance(subject, ApprovalSubjectBinding)
                else _validated_subject(subject)
            )
            subject_digest = approval_subject_digest(
                normalized_subject,
                action_digest=binding.action_digest,
                scope_digest=binding.scope_digest,
                presentation_digest=presentation_digest,
            )
        request_id = "apr_" + uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire(conn, observed)
            current = conn.execute(
                "SELECT * FROM approval_requests WHERE job_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (str(job_id),),
            ).fetchone()
            if (
                current is not None
                and current["action_digest"] == binding.action_digest
                and current["scope_digest"] == binding.scope_digest
            ):
                current_presentation_digest = str(
                    current["presentation_digest"] or ""
                )
                if current_presentation_digest and presentation_supplied and (
                    current_presentation_digest != presentation_digest
                ):
                    conn.rollback()
                    raise ApprovalAuthorityError(
                        "presentation_mismatch",
                        "approval presentation changed for an immutable action",
                    )
                if not current_presentation_digest and presentation_supplied:
                    # Migration-only backfill. The action digest already binds
                    # the source payload from which this redacted view derives.
                    conn.execute(
                        "UPDATE approval_requests SET presentation_json=?, "
                        "presentation_digest=? WHERE request_id=? "
                        "AND presentation_digest IS NULL",
                        (
                            presentation_json,
                            presentation_digest,
                            current["request_id"],
                        ),
                    )
                    current = conn.execute(
                        "SELECT * FROM approval_requests WHERE request_id=?",
                        (current["request_id"],),
                    ).fetchone()
                current_subject_values = (
                    current["subject_kind"], current["subject_id"],
                    current["subject_revision"], current["subject_action"],
                )
                current_has_subject = any(
                    value not in (None, "") for value in current_subject_values
                )
                if normalized_subject is not None:
                    if not current_has_subject:
                        conn.rollback()
                        raise ApprovalAuthorityError(
                            "subject_binding_conflict",
                            "an existing approval request cannot be relabelled as a typed subject",
                        )
                    expected_values = (
                        normalized_subject["kind"],
                        normalized_subject["subject_id"],
                        normalized_subject["revision"],
                        normalized_subject["action"],
                    )
                    if (
                        current_subject_values != expected_values
                        or current["subject_digest"] != subject_digest
                    ):
                        conn.rollback()
                        raise ApprovalAuthorityError(
                            "subject_binding_conflict",
                            "approval request subject binding is immutable",
                        )
                conn.commit()
                return self._request_dict(current)

            if current is not None and (
                normalized_subject is not None
                or current["subject_kind"] not in (None, "")
            ):
                conn.rollback()
                raise ApprovalAuthorityError(
                    "subject_binding_conflict",
                    "a typed approval subject cannot change its immutable action binding",
                )

            if current is not None and current["status"] == "pending":
                conn.execute(
                    "UPDATE approval_requests SET status='superseded', "
                    "superseded_by=? WHERE request_id=? AND status='pending'",
                    (request_id, current["request_id"]),
                )

            conn.execute(
                "INSERT INTO approval_requests "
                "(request_id, job_id, action_digest, scope_json, scope_digest, "
                "status, created_at, expires_at, presentation_json, "
                "presentation_digest, subject_kind, subject_id, "
                "subject_revision, subject_action, subject_digest) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    str(job_id),
                    binding.action_digest,
                    _canonical(binding.scope),
                    binding.scope_digest,
                    _iso(observed),
                    _iso(observed + timedelta(seconds=ttl_seconds)),
                    presentation_json,
                    presentation_digest,
                    normalized_subject["kind"] if normalized_subject else None,
                    normalized_subject["subject_id"] if normalized_subject else None,
                    normalized_subject["revision"] if normalized_subject else None,
                    normalized_subject["action"] if normalized_subject else None,
                    subject_digest,
                ),
            )
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            conn.commit()
        assert row is not None
        return self._request_dict(row)

    def get_request(self, request_id: str, *, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE request_id=?", (request_id,)
            ).fetchone()
        return self._request_view(row, now=now) if row is not None else None

    def get_request_for_job(
        self,
        job_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return the latest canonical request for one immutable queue job."""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE job_id=? "
                "ORDER BY created_at DESC, request_id DESC LIMIT 1",
                (str(job_id),),
            ).fetchone()
        return self._request_view(row, now=now) if row is not None else None

    def list_requests(
        self,
        *,
        status: Optional[str] = None,
        limit: int = 100,
        now: Optional[datetime] = None,
    ) -> list[Dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 500))
        observed = _as_utc(now)
        with self._connect() as conn:
            if status == "pending":
                rows = conn.execute(
                    "SELECT * FROM approval_requests WHERE status='pending' "
                    "AND expires_at>? ORDER BY created_at DESC LIMIT ?",
                    (_iso(observed), bounded_limit),
                ).fetchall()
            elif status == "expired":
                rows = conn.execute(
                    "SELECT * FROM approval_requests WHERE status='expired' "
                    "OR (status='pending' AND expires_at<=?) "
                    "ORDER BY created_at DESC LIMIT ?",
                    (_iso(observed), bounded_limit),
                ).fetchall()
            elif status:
                rows = conn.execute(
                    "SELECT * FROM approval_requests WHERE status=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (status, bounded_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM approval_requests ORDER BY created_at DESC LIMIT ?",
                    (bounded_limit,),
                ).fetchall()
        return [self._request_view(row, now=observed) for row in rows]

    def list_subject_requests(
        self,
        *,
        subject_kind: str,
        status: Optional[str] = None,
        limit: int = 100,
        now: Optional[datetime] = None,
    ) -> list[Dict[str, Any]]:
        """Return typed candidates for a domain service to validate.

        This is intentionally not itself a public authorization boundary. The
        owning domain must still prove the subject exists and recompute its
        domain binding before projecting or deciding a candidate.
        """

        inspection = self.inspect_subject_requests(
            subject_kind=subject_kind,
            status=status,
            limit=limit,
            now=now,
        )
        return inspection["requests"]

    def inspect_subject_requests(
        self,
        *,
        subject_kind: str,
        status: Optional[str] = None,
        limit: int = 100,
        job_prefix: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Inspect typed candidates while counting undisclosed invalid rows."""

        kind = str(subject_kind or "").strip()
        if not _SUBJECT_KIND_RE.fullmatch(kind):
            raise ApprovalAuthorityError(
                "invalid_subject_binding", "approval subject kind is invalid",
            )
        bounded_limit = max(1, min(int(limit), 500))
        observed = _as_utc(now)
        prefix = str(job_prefix or "").strip()
        if prefix and (
            len(prefix) > 128 or _CONTROL.search(prefix)
            or not _ACTION_RE.fullmatch(prefix.rstrip(":"))
        ):
            raise ApprovalAuthorityError(
                "invalid_subject_binding", "approval job prefix is invalid",
            )
        predicates = [
            "(subject_kind=? OR job_id LIKE ? ESCAPE '\\')"
            if prefix else "subject_kind=?",
        ]
        values: list[Any] = [kind]
        if prefix:
            values.append(prefix.replace("%", "\\%").replace("_", "\\_") + "%")
        if status == "pending":
            predicates.extend(["status='pending'", "expires_at>?"])
            values.append(_iso(observed))
        elif status == "expired":
            predicates.append(
                "(status='expired' OR (status='pending' AND expires_at<=?))"
            )
            values.append(_iso(observed))
        elif status:
            predicates.append("status=?")
            values.append(str(status))
        values.append(bounded_limit)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM approval_requests WHERE "
                + " AND ".join(predicates)
                + " ORDER BY created_at DESC, request_id DESC LIMIT ?",
                values,
            ).fetchall()
        requests: list[Dict[str, Any]] = []
        invalid_count = 0
        for row in rows:
            try:
                request = self._request_view(row, now=observed)
            except ApprovalAuthorityError:
                invalid_count += 1
                continue
            subject = request.get("subject")
            if not isinstance(subject, Mapping) or subject.get("kind") != kind:
                invalid_count += 1
                continue
            requests.append(request)
        return {
            "requests": requests,
            "invalid_hidden_count": invalid_count,
            "candidate_count": len(rows),
            "complete": len(rows) < bounded_limit,
        }

    def decide(
        self,
        request_id: str,
        *,
        decision: str,
        decision_id: str,
        expected_action_digest: str,
        decided_by: str,
        authority_evidence: str,
        grant_scope: Optional[Mapping[str, Any]] = None,
        grant_ttl_seconds: int = DEFAULT_GRANT_TTL_SECONDS,
        grant_max_uses: int = DEFAULT_GRANT_MAX_USES,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Record the first valid decision and optionally mint one grant.

        Repeating the exact same decision id is idempotent.  A different
        replay, an opposite decision, or a decision against changed content is
        rejected and cannot alter the winner.
        """

        normalized = str(decision).strip().lower()
        if normalized not in {"approve", "reject"}:
            raise ApprovalAuthorityError("invalid_decision", "decision must be approve or reject")
        if not _DECISION_ID_RE.fullmatch(str(decision_id or "")):
            raise ApprovalAuthorityError("invalid_decision_id", "decision_id is malformed")
        actor = str(decided_by or "").strip()
        evidence = str(authority_evidence or "").strip()
        if not actor or not evidence:
            raise ApprovalAuthorityError(
                "authority_required", "server-derived decision authority is required"
            )
        if len(evidence) > 512 or _CONTROL.search(evidence):
            raise ApprovalAuthorityError(
                "invalid_authority_evidence",
                "decision authority evidence is invalid or exceeds 512 characters",
            )
        if grant_scope is not None:
            if normalized != "approve":
                raise ApprovalAuthorityError("invalid_grant", "a rejection cannot create a grant")
            if grant_ttl_seconds < 60 or grant_ttl_seconds > MAX_GRANT_TTL_SECONDS:
                raise ApprovalAuthorityError("invalid_grant_expiry", "grant expiry is out of bounds")
            if grant_max_uses < 1 or grant_max_uses > MAX_GRANT_USES:
                raise ApprovalAuthorityError("invalid_grant_uses", "grant use bound is out of bounds")

        observed = _as_utc(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire(conn, observed)
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise ApprovalAuthorityError("request_not_found", "approval request was not found")
            if row["action_digest"] != expected_action_digest:
                conn.rollback()
                raise ApprovalAuthorityError(
                    "stale_action_digest", "decision does not match the current immutable action"
                )

            replayed_elsewhere = conn.execute(
                "SELECT request_id FROM approval_requests WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
            if (
                replayed_elsewhere is not None
                and replayed_elsewhere["request_id"] != request_id
            ):
                conn.rollback()
                raise ApprovalAuthorityError(
                    "decision_replay", "decision_id was already used for another request"
                )

            status = row["status"]
            if status in {"approved", "rejected"}:
                if row["decision_id"] == decision_id and row["decision"] == normalized:
                    grant = None
                    if row["grant_id"]:
                        grant_row = conn.execute(
                            "SELECT * FROM bounded_grants WHERE grant_id=?", (row["grant_id"],)
                        ).fetchone()
                        grant = self._grant_dict(grant_row) if grant_row is not None else None
                    conn.commit()
                    return {
                        "request": self._request_dict(row),
                        "grant": grant,
                        "replayed": True,
                    }
                conn.rollback()
                raise ApprovalAuthorityError(
                    "decision_already_final", "the first valid decision is already final"
                )
            if status == "expired":
                conn.rollback()
                raise ApprovalAuthorityError("request_expired", "approval request has expired")
            if status == "superseded":
                conn.rollback()
                raise ApprovalAuthorityError("request_superseded", "approval request was superseded")
            if status != "pending":
                conn.rollback()
                raise ApprovalAuthorityError("request_not_pending", "approval request is not pending")

            grant_id: Optional[str] = None
            grant: Optional[Dict[str, Any]] = None
            if grant_scope is not None:
                stored_scope = json.loads(row["scope_json"])
                if _canonical(dict(grant_scope)) != _canonical(stored_scope):
                    conn.rollback()
                    raise ApprovalAuthorityError(
                        "scope_broadening",
                        "bounded grant scope must exactly match the approved action scope",
                    )
                grant_id = "grt_" + hashlib.sha256(
                    f"{request_id}:{decision_id}".encode("utf-8")
                ).hexdigest()[:32]
                conn.execute(
                    "INSERT INTO bounded_grants "
                    "(grant_id, source_request_id, scope_json, scope_digest, status, "
                    "created_at, expires_at, max_uses, uses, granted_by, decision_id) "
                    "VALUES (?, ?, ?, ?, 'active', ?, ?, ?, 0, ?, ?)",
                    (
                        grant_id,
                        request_id,
                        row["scope_json"],
                        row["scope_digest"],
                        _iso(observed),
                        _iso(observed + timedelta(seconds=grant_ttl_seconds)),
                        int(grant_max_uses),
                        actor,
                        decision_id,
                    ),
                )

            final_status = "approved" if normalized == "approve" else "rejected"
            conn.execute(
                "UPDATE approval_requests SET status=?, decision=?, decision_id=?, "
                "decided_at=?, decided_by=?, authority_evidence=?, grant_id=? "
                "WHERE request_id=? AND status='pending'",
                (
                    final_status,
                    normalized,
                    decision_id,
                    _iso(observed),
                    actor,
                    evidence,
                    grant_id,
                    request_id,
                ),
            )
            final_row = conn.execute(
                "SELECT * FROM approval_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if grant_id:
                grant_row = conn.execute(
                    "SELECT * FROM bounded_grants WHERE grant_id=?", (grant_id,)
                ).fetchone()
                grant = self._grant_dict(grant_row) if grant_row is not None else None
            conn.commit()
        assert final_row is not None
        return {
            "request": self._request_dict(final_row),
            "grant": grant,
            "replayed": False,
        }

    def supersede(
        self,
        request_id: str,
        *,
        replaced_by: str,
    ) -> Dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise ApprovalAuthorityError("request_not_found", "approval request was not found")
            if row["status"] != "pending":
                conn.rollback()
                raise ApprovalAuthorityError(
                    "request_not_pending", "only a pending request can be superseded"
                )
            conn.execute(
                "UPDATE approval_requests SET status='superseded', superseded_by=? "
                "WHERE request_id=? AND status='pending'",
                (str(replaced_by), request_id),
            )
            final_row = conn.execute(
                "SELECT * FROM approval_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            conn.commit()
        assert final_row is not None
        return self._request_dict(final_row)

    def consume_grant(
        self,
        *,
        binding: ActionBinding,
        operation_id: str,
        now: Optional[datetime] = None,
    ) -> Optional[Dict[str, Any]]:
        """Atomically consume one matching use; retries are idempotent."""

        observed = _as_utc(now)
        operation = str(operation_id or "").strip()
        if not operation:
            raise ApprovalAuthorityError("operation_id_required", "operation_id is required")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire(conn, observed)
            existing = conn.execute(
                "SELECT g.* FROM bounded_grant_uses u "
                "JOIN bounded_grants g ON g.grant_id=u.grant_id "
                "WHERE u.action_digest=?",
                (binding.action_digest,),
            ).fetchone()
            if existing is not None:
                conn.commit()
                result = self._grant_dict(existing)
                result["idempotent_reuse"] = True
                return result

            row = conn.execute(
                "SELECT * FROM bounded_grants WHERE scope_digest=? "
                "AND status='active' AND expires_at>? AND uses<max_uses "
                "ORDER BY expires_at ASC, created_at ASC LIMIT 1",
                (binding.scope_digest, _iso(observed)),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            if _canonical(json.loads(row["scope_json"])) != _canonical(binding.scope):
                # Hash collisions are fantastically unlikely, but authority
                # code compares canonical scope too instead of relying on one.
                conn.rollback()
                raise ApprovalAuthorityError("scope_digest_collision", "grant scope mismatch")

            conn.execute(
                "INSERT INTO bounded_grant_uses "
                "(action_digest, grant_id, operation_id, consumed_at) VALUES (?, ?, ?, ?)",
                (binding.action_digest, row["grant_id"], operation, _iso(observed)),
            )
            conn.execute(
                "UPDATE bounded_grants SET uses=uses+1, "
                "status=CASE WHEN uses+1>=max_uses THEN 'exhausted' ELSE status END "
                "WHERE grant_id=?",
                (row["grant_id"],),
            )
            final_row = conn.execute(
                "SELECT * FROM bounded_grants WHERE grant_id=?", (row["grant_id"],)
            ).fetchone()
            conn.commit()
        assert final_row is not None
        result = self._grant_dict(final_row)
        result["idempotent_reuse"] = False
        return result

    def list_grants(
        self,
        *,
        status: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> list[Dict[str, Any]]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire(conn, _as_utc(now))
            if status:
                rows = conn.execute(
                    "SELECT * FROM bounded_grants WHERE status=? ORDER BY created_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM bounded_grants ORDER BY created_at DESC"
                ).fetchall()
            conn.commit()
        return [self._grant_dict(row) for row in rows]

    def get_grant_use(self, action_digest: str) -> Optional[Dict[str, Any]]:
        """Return durable bounded-grant consumption evidence for one action."""

        digest = str(action_digest or "").strip()
        if not digest:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """SELECT u.action_digest, u.grant_id, u.operation_id,
                          u.consumed_at, g.scope_digest, g.source_request_id,
                          g.granted_by, g.decision_id, g.status AS grant_status,
                          g.created_at AS grant_created_at,
                          g.expires_at AS grant_expires_at,
                          g.max_uses, g.uses
                   FROM bounded_grant_uses AS u
                   JOIN bounded_grants AS g ON g.grant_id = u.grant_id
                   WHERE u.action_digest = ?""",
                (digest,),
            ).fetchone()
        return dict(row) if row is not None else None

    def revoke_grant(self, grant_id: str, *, now: Optional[datetime] = None) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE bounded_grants SET status='revoked', revoked_at=? "
                "WHERE grant_id=? AND status IN ('active', 'exhausted')",
                (_iso(_as_utc(now)), grant_id),
            )
            changed = cursor.rowcount > 0
            conn.commit()
        return changed


def authority_mode() -> str:
    """Return strict approval authority mode, preserving invalid config."""

    value = os.environ.get("COLONY_APPROVAL_AUTHORITY_MODE", "shadow").strip().lower()
    return value if value in {"shadow", "enforce"} else "invalid"
