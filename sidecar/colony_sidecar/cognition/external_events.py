"""Strict Phase C intake for external text and system cognition evidence.

External producers may report bounded observations. They cannot assert person,
audience, boundary, or transport authority, and this module never accepts a
realtime audio/voice surface. The server-attested envelope is stored before it
is projected into Colony's existing durable host event journal.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, Mapping, Optional

from colony_sidecar.scope_bounds import (
    SUBJECT_PERSON_ID_MAX_CHARS,
    VIEWER_SCOPE_MAX_CHARS,
)


_KINDS = frozenset({
    "action_outcome", "delivery_outcome", "service_state", "approval_state",
    "operator_reaction", "text_turn_observation",
})
EXTERNAL_JOURNAL_EVENT_ID_MAX_CHARS = 128
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{7,191}$")
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,191}$")
_SAFE_JOURNAL_EVENT_ID = re.compile(
    rf"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]"
    rf"{{0,{EXTERNAL_JOURNAL_EVENT_ID_MAX_CHARS - 1}}}$"
)
_SAFE_ATTRIBUTE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SECRET_KEY = re.compile(
    r"(?i)(?:^|[_.-])(api[_-]?key|authorization|bearer|cookie|credential|"
    r"password|private[_-]?key|secret|session|token)(?:$|[_.-])"
)
_SECRET_VALUE = re.compile(
    r"(?ix)(?:"
    r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"\bsk-[A-Za-z0-9_-]{8,}|"
    r"\b(?:api[\s_-]?(?:key|token)|authorization|cookie|credential|password|"
    r"private[\s_-]?key|secret|session|token)\s*[:=]\s*"
    r"(?:\"[^\"\r\n]{8,}\"|'[^'\r\n]{8,}'|[A-Za-z0-9._~+/=-]{8,})"
    r")"
)
_RESERVED_ATTRIBUTES = frozenset({
    "approval", "audience", "audiences", "authority", "boundary_attested",
    "credential_id", "evidence_status", "grant", "outcome_contract",
    "person_id", "principal_id", "producer_revision", "receipt_ref", "scope",
    "scope_digest", "shareability", "subject_person_id", "verified",
    "viewer_person_id", "viewer_scope",
})
_AUTHORITY_ATTRIBUTE_ALIAS = re.compile(
    r"(?:^|[_.-])(?:approved?|approval|authority|authorized|evidence[_-]?status|"
    r"granted?|outcome[_-]?contract|receipt[_-]?ref|verification|verified)"
    r"(?:$|[_.-])"
)
_KIND_FIELDS = {
    "action_outcome": {
        "required": {"action_id", "outcome"},
        "allowed": {
            "action_id", "outcome", "action_digest", "error_code",
            "duration_ms",
        },
        "enum": {
            "outcome": {
                "succeeded", "failed", "blocked", "cancelled", "partial",
            },
        },
    },
    "delivery_outcome": {
        "required": {"outcome"},
        "allowed": {
            "delivery_ref", "message_ref", "outcome", "channel", "error_code",
        },
        "enum": {
            "outcome": {
                "delivered", "failed", "bounced", "read", "acknowledged",
            },
            "channel": {
                "text", "sms", "rcs", "whatsapp", "chat", "email",
                "webhook", "system",
            },
        },
    },
    "service_state": {
        "required": {"service", "state"},
        "allowed": {
            "service", "state", "detail", "latency_ms", "observed_samples",
        },
        "enum": {
            "state": {"healthy", "degraded", "offline", "recovering"},
        },
    },
    "approval_state": {
        "required": {"request_id", "state"},
        "allowed": {"request_id", "state", "decision_ref", "reason_code"},
        "enum": {
            "state": {"pending", "approved", "rejected", "expired", "cancelled"},
        },
    },
    "operator_reaction": {
        "required": {"target_ref", "reaction"},
        "allowed": {"target_ref", "reaction", "intensity", "signal"},
        "enum": {
            "reaction": {
                "positive", "negative", "neutral", "correction", "dismissed",
            },
        },
    },
    "text_turn_observation": {
        "required": {"turn_id", "channel", "observation"},
        "allowed": {"turn_id", "channel", "observation"},
        "enum": {
            "channel": {"text", "sms", "rcs", "whatsapp", "chat", "email"},
        },
    },
}

_JOURNAL_PROJECTION_FIELDS = frozenset({
    "schema", "version", "external_event_id", "external_event_digest",
    "external_occurred_at", "kind", "summary", "attributes", "producer_principal_id",
    "producer_revision", "subject_person_id", "viewer_person_id",
    "viewer_scope", "shareability", "audience_scope", "scope_digest",
    "boundary_attested", "evidence_status",
})


class ExternalEventValidationError(ValueError):
    """The external event is outside the strict Phase C evidence schema."""


class ExternalEventConflict(ValueError):
    """An event ID was replayed with changed immutable content."""


class ExternalEventProjectionError(RuntimeError):
    """The receipt is durable but journal projection is currently unavailable."""


def validate_external_journal_event_id(value: Any) -> str:
    """Return one exact, bounded host-journal identity for a V2 projection."""

    if type(value) is not str or not _SAFE_JOURNAL_EVENT_ID.fullmatch(value):
        raise ExternalEventValidationError(
            "external cognition journal ID is not canonical"
        )
    return value


def _exact_projection_text(
    value: Any,
    *,
    field: str,
    maximum: int,
) -> str:
    """Accept only the exact JSON string form emitted by the server."""

    if type(value) is not str:
        raise ExternalEventValidationError(
            f"external cognition journal {field} must be an exact string"
        )
    if not value or value != value.strip() or len(value) > maximum:
        raise ExternalEventValidationError(
            f"external cognition journal {field} is not canonical"
        )
    return value


def validate_external_journal_projection(
    event_type: Any,
    value: Any,
) -> Dict[str, Any]:
    """Validate one server-produced host-journal projection exactly.

    This is the shared trust boundary for downstream durable consumers.  It
    deliberately revalidates the typed attributes and recomputes the scope
    digest instead of trusting fields copied from the journal payload.
    """

    if not isinstance(value, Mapping):
        raise ExternalEventValidationError(
            "external cognition journal projection must be one object"
        )
    if set(value) != _JOURNAL_PROJECTION_FIELDS:
        raise ExternalEventValidationError(
            "external cognition journal projection fields are not exact"
        )
    if (
        type(value.get("schema")) is not str
        or value.get("schema") != "ExternalCognitionJournalProjectionV2"
        or (
            type(value.get("version")) is not int
            or value.get("version") != 2
        )
    ):
        raise ExternalEventValidationError(
            "external cognition journal projection schema is invalid"
        )
    kind = _exact_projection_text(
        value.get("kind"), field="kind", maximum=64,
    )
    if kind not in _KINDS:
        raise ExternalEventValidationError(
            "external cognition journal projection kind is unsupported"
        )
    if (
        type(event_type) is not str
        or event_type != f"cognition.external.{kind}"
    ):
        raise ExternalEventValidationError(
            "external cognition journal event type differs from its kind"
        )
    external_id = _exact_projection_text(
        value.get("external_event_id"), field="event ID", maximum=192,
    )
    if not _SAFE_ID.fullmatch(external_id):
        raise ExternalEventValidationError(
            "external cognition journal event ID is invalid"
        )
    external_digest = _exact_projection_text(
        value.get("external_event_digest"), field="event digest", maximum=64,
    )
    scope_digest = _exact_projection_text(
        value.get("scope_digest"), field="scope digest", maximum=64,
    )
    if not re.fullmatch(r"[a-f0-9]{64}", external_digest):
        raise ExternalEventValidationError(
            "external cognition journal event digest is invalid"
        )
    if not re.fullmatch(r"[a-f0-9]{64}", scope_digest):
        raise ExternalEventValidationError(
            "external cognition journal scope digest is invalid"
        )
    external_occurred_at = _exact_projection_text(
        value.get("external_occurred_at"),
        field="occurred time", maximum=64,
    )
    if _iso(_parse_time(
        external_occurred_at, field="external_occurred_at",
    )) != external_occurred_at:
        raise ExternalEventValidationError(
            "external cognition journal occurred time is not canonical"
        )
    producer = _exact_projection_text(
        value.get("producer_principal_id"),
        field="producer principal ID", maximum=128,
    )
    revision = _exact_projection_text(
        value.get("producer_revision"), field="producer revision", maximum=64,
    )
    subject = _exact_projection_text(
        value.get("subject_person_id"), field="subject person ID",
        maximum=SUBJECT_PERSON_ID_MAX_CHARS,
    )
    viewer = _exact_projection_text(
        value.get("viewer_person_id"), field="viewer person ID",
        maximum=SUBJECT_PERSON_ID_MAX_CHARS,
    )
    if not _SAFE_REF.fullmatch(producer) or not re.fullmatch(
        r"external-principal:[a-f0-9]{24}", revision,
    ):
        raise ExternalEventValidationError(
            "external cognition journal producer identity is invalid"
        )
    if (
        not _SAFE_REF.fullmatch(subject)
        or not _SAFE_REF.fullmatch(viewer)
        or subject != viewer
    ):
        raise ExternalEventValidationError(
            "external cognition journal subject binding is invalid"
        )
    sharing = _exact_projection_text(
        value.get("shareability"), field="shareability", maximum=32,
    )
    viewer_scope = _exact_projection_text(
        value.get("viewer_scope"), field="viewer scope",
        maximum=VIEWER_SCOPE_MAX_CHARS,
    )
    audience = value.get("audience_scope")
    if (
        type(audience) is not list
        or any(
            type(item) is not str or not item or item != item.strip()
            for item in audience
        )
    ):
        raise ExternalEventValidationError(
            "external cognition journal audience scope is not canonical"
        )
    configured_owner = (
        os.environ.get("COLONY_OWNER_PERSON_ID", "")
        or os.environ.get("COLONY_OWNER_CONTACT_ID", "")
        or "owner"
    )
    owner = _authority_id(
        configured_owner, field="configured owner_person_id", maximum=128,
    )
    if sharing == "owner_private":
        if subject != owner:
            raise ExternalEventValidationError(
                "external cognition owner lane subject is not the owner"
            )
        expected_viewer_scope = "owner"
        expected_audience = ["owner"]
    elif sharing == "subject_private":
        if subject == owner:
            raise ExternalEventValidationError(
                "external cognition owner cannot assert a subject-private lane"
            )
        expected_viewer_scope = f"person:{subject}"
        expected_audience = []
    else:
        raise ExternalEventValidationError(
            "external cognition journal sharing lane is invalid"
        )
    if viewer_scope != expected_viewer_scope or audience != expected_audience:
        raise ExternalEventValidationError(
            "external cognition journal viewer or audience scope is invalid"
        )
    scope = {
        "schema": "ExternalCognitionScopeV1",
        "version": 1,
        "subject_person_id": subject,
        "viewer_person_id": viewer,
        "viewer_scope": viewer_scope,
        "shareability": sharing,
        "audience_scope": expected_audience,
    }
    if scope_digest != _digest(scope):
        raise ExternalEventValidationError(
            "external cognition journal scope digest mismatch"
        )
    evidence_status = _exact_projection_text(
        value.get("evidence_status"), field="evidence status", maximum=32,
    )
    if value.get("boundary_attested") is not False or (
        evidence_status != "reported/unverified"
    ):
        raise ExternalEventValidationError(
            "external cognition journal projection asserted authority"
        )
    if type(value.get("summary")) is not str:
        raise ExternalEventValidationError(
            "external cognition journal summary must be an exact string"
        )
    summary = _text(value.get("summary"), field="summary", maximum=1000)
    attributes = _typed_attributes(kind, value.get("attributes"))
    if summary != value.get("summary") or _canonical(attributes) != _canonical(
        value.get("attributes")
    ):
        raise ExternalEventValidationError(
            "external cognition journal content is not canonical"
        )
    return dict(value)


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _parse_time(value: Any, *, field: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ExternalEventValidationError(
            f"{field} must be an RFC3339 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise ExternalEventValidationError(f"{field} requires a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ExternalEventValidationError(f"{field} must be text")
    result = " ".join(value.split()).strip()
    if not result or len(result) > maximum:
        raise ExternalEventValidationError(
            f"{field} must contain 1-{maximum} characters"
        )
    if _SECRET_VALUE.search(result):
        raise ExternalEventValidationError(f"{field} contains secret-like data")
    return result


def _authority_id(value: Any, *, field: str, maximum: int) -> str:
    """Accept one exact server authority identifier without collision-prone slicing."""

    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > maximum
        or not _SAFE_REF.fullmatch(value)
    ):
        raise ExternalEventValidationError(
            f"{field} is outside the exact authority identifier boundary"
        )
    return value


def _attribute_scalar(value: Any, *, field: str) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExternalEventValidationError(f"{field} must be finite")
        return value
    if isinstance(value, str):
        result = " ".join(value.split()).strip()
        if len(result) > 500:
            raise ExternalEventValidationError(f"{field} exceeds 500 characters")
        if _SECRET_VALUE.search(result):
            raise ExternalEventValidationError(f"{field} contains secret-like data")
        return result
    raise ExternalEventValidationError(
        f"{field} supports only JSON scalars or scalar arrays"
    )


def _attributes(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ExternalEventValidationError("attributes must be one object")
    if len(value) > 32:
        raise ExternalEventValidationError("attributes exceeds 32 fields")
    result: Dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip().lower()
        if not _SAFE_ATTRIBUTE.fullmatch(key):
            raise ExternalEventValidationError("attributes contains an invalid key")
        if key in _RESERVED_ATTRIBUTES or _AUTHORITY_ATTRIBUTE_ALIAS.search(key):
            raise ExternalEventValidationError(
                f"attributes.{key} attempts to assert server authority"
            )
        if _SECRET_KEY.search(key):
            raise ExternalEventValidationError(
                f"attributes.{key} is secret-bearing"
            )
        if isinstance(raw_value, list):
            if len(raw_value) > 16:
                raise ExternalEventValidationError(
                    f"attributes.{key} exceeds 16 list items"
                )
            result[key] = [
                _attribute_scalar(item, field=f"attributes.{key}")
                for item in raw_value
            ]
        else:
            result[key] = _attribute_scalar(
                raw_value, field=f"attributes.{key}",
            )
    if len(_canonical(result).encode("utf-8")) > 8192:
        raise ExternalEventValidationError("attributes exceeds 8192 bytes")
    return result


def _typed_attributes(kind: str, value: Any) -> Dict[str, Any]:
    result = _attributes(value)
    contract = _KIND_FIELDS[kind]
    missing = set(contract["required"]) - set(result)
    unknown = set(result) - set(contract["allowed"])
    if kind == "delivery_outcome" and not (
        result.get("delivery_ref") or result.get("message_ref")
    ):
        missing.add("delivery_ref_or_message_ref")
    if missing:
        raise ExternalEventValidationError(
            f"{kind} attributes missing: {','.join(sorted(missing))}"
        )
    if unknown:
        raise ExternalEventValidationError(
            f"{kind} attributes unsupported: {','.join(sorted(unknown))}"
        )
    identifier_fields = {
        "action_id", "action_digest", "delivery_ref", "message_ref", "service",
        "request_id", "decision_ref", "target_ref", "turn_id",
    }
    for field in identifier_fields.intersection(result):
        if not isinstance(result[field], str) or not _SAFE_REF.fullmatch(
            result[field]
        ):
            raise ExternalEventValidationError(
                f"{kind} attributes.{field} is invalid"
            )
    if "action_digest" in result and not (
        isinstance(result["action_digest"], str)
        and re.fullmatch(r"[a-f0-9]{64}", result["action_digest"])
    ):
        raise ExternalEventValidationError(
            "action_outcome attributes.action_digest must be lowercase 64-hex"
        )
    for field in {
        "channel", "detail", "error_code", "observation", "reason_code", "signal",
    }.intersection(result):
        if not isinstance(result[field], str):
            raise ExternalEventValidationError(
                f"{kind} attributes.{field} must be text"
            )
    if kind == "text_turn_observation":
        result["observation"] = _text(
            result["observation"],
            field="text_turn_observation attributes.observation",
            maximum=500,
        )
    numeric_bounds = {
        "duration_ms": (0.0, 86_400_000.0, False),
        "latency_ms": (0.0, 3_600_000.0, False),
        "observed_samples": (1.0, 1_000_000.0, True),
        "intensity": (0.0, 1.0, False),
    }
    for field, (minimum, maximum, integer_only) in numeric_bounds.items():
        if field not in result:
            continue
        numeric = result[field]
        if (
            isinstance(numeric, bool)
            or not isinstance(numeric, (int, float))
            or not math.isfinite(float(numeric))
            or not minimum <= float(numeric) <= maximum
            or (integer_only and not isinstance(numeric, int))
        ):
            raise ExternalEventValidationError(
                f"{kind} attributes.{field} is outside bounds"
            )
    for field, choices in contract["enum"].items():
        if field not in result:
            continue
        if not isinstance(result.get(field), str):
            raise ExternalEventValidationError(
                f"{kind} attributes.{field} must be text"
            )
        normalized = result[field].strip().lower()
        if normalized not in choices:
            raise ExternalEventValidationError(
                f"{kind} attributes.{field} is unsupported"
            )
        result[field] = normalized
    return result


@dataclass(frozen=True)
class ExternalCognitionEventV1:
    event_id: str
    kind: str
    occurred_at: str
    summary: str
    attributes: Mapping[str, Any]
    producer_principal_id: str
    producer_credential_id: str
    producer_revision: str
    subject_person_id: str
    viewer_person_id: str
    viewer_scope: str
    shareability: str
    audience_scope: tuple[str, ...]
    scope_digest: str
    boundary_attested: bool
    event_digest: str
    schema: str = "ExternalCognitionEventV1"
    version: int = 1

    @classmethod
    def from_authority(
        cls,
        body: Mapping[str, Any],
        *,
        authority: Any,
        now: Optional[datetime] = None,
    ) -> "ExternalCognitionEventV1":
        if not isinstance(body, Mapping):
            raise ExternalEventValidationError("event body must be one object")
        allowed = {"event_id", "kind", "occurred_at", "summary", "attributes"}
        unknown = set(body) - allowed
        if unknown:
            raise ExternalEventValidationError(
                "event body contains unsupported fields: "
                + ",".join(sorted(str(item) for item in unknown))
            )
        if not (
            getattr(authority, "authenticated", False)
            and not getattr(authority, "legacy", False)
            and not getattr(authority, "anonymous", False)
            and authority.has_scope("cognition:events-ingest")
            and getattr(authority, "principal_id", "")
            and getattr(authority, "credential_id", "")
            and getattr(authority, "viewer_person_id", "")
        ):
            raise ExternalEventValidationError(
                "scoped principal with an exact viewer binding is required"
            )
        event_id = str(body.get("event_id") or "").strip()
        if not _SAFE_ID.fullmatch(event_id):
            raise ExternalEventValidationError("event_id is invalid")
        kind = str(body.get("kind") or "").strip().lower()
        if kind not in _KINDS:
            raise ExternalEventValidationError("external event kind is unsupported")
        observed = now or datetime.now(timezone.utc)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        occurred = _parse_time(body.get("occurred_at"), field="occurred_at")
        if occurred > observed.astimezone(timezone.utc) + timedelta(minutes=5):
            raise ExternalEventValidationError("occurred_at is too far in the future")
        if occurred < observed.astimezone(timezone.utc) - timedelta(days=366):
            raise ExternalEventValidationError("occurred_at is outside retention bounds")
        summary = _text(body.get("summary"), field="summary", maximum=1000)
        attributes = _typed_attributes(kind, body.get("attributes"))

        principal = _authority_id(
            authority.principal_id, field="producer principal_id", maximum=128,
        )
        credential = _authority_id(
            authority.credential_id, field="producer credential_id", maximum=192,
        )
        viewer = _authority_id(
            authority.viewer_person_id, field="viewer_person_id", maximum=128,
        )
        configured_owner = (
            os.environ.get("COLONY_OWNER_PERSON_ID", "")
            or os.environ.get("COLONY_OWNER_CONTACT_ID", "")
            or "owner"
        )
        owner = _authority_id(
            configured_owner, field="configured owner_person_id", maximum=128,
        )
        owner_private = "owner" in authority.audiences and viewer == owner
        sharing = "owner_private" if owner_private else "subject_private"
        viewer_scope = "owner" if owner_private else f"person:{viewer}"
        audience_scope = ("owner",) if owner_private else ()
        scope = {
            "schema": "ExternalCognitionScopeV1",
            "version": 1,
            "subject_person_id": viewer,
            "viewer_person_id": viewer,
            "viewer_scope": viewer_scope,
            "shareability": sharing,
            "audience_scope": list(audience_scope),
        }
        producer_revision = "external-principal:" + _digest({
            "principal_id": principal,
            "credential_id": credential,
            "scopes": sorted(authority.scopes),
        })[:24]
        payload = {
            "schema": "ExternalCognitionEventV1",
            "version": 1,
            "event_id": event_id,
            "kind": kind,
            "occurred_at": _iso(occurred),
            "summary": summary,
            "attributes": attributes,
            "producer_principal_id": principal,
            "producer_credential_id": credential,
            "producer_revision": producer_revision,
            "subject_person_id": scope["subject_person_id"],
            "viewer_person_id": scope["viewer_person_id"],
            "viewer_scope": scope["viewer_scope"],
            "shareability": scope["shareability"],
            "audience_scope": scope["audience_scope"],
            "scope_digest": _digest(scope),
            "boundary_attested": False,
        }
        return cls(
            event_id=event_id,
            kind=kind,
            occurred_at=payload["occurred_at"],
            summary=summary,
            attributes=attributes,
            producer_principal_id=principal,
            producer_credential_id=credential,
            producer_revision=producer_revision,
            subject_person_id=viewer,
            viewer_person_id=viewer,
            viewer_scope=viewer_scope,
            shareability=sharing,
            audience_scope=audience_scope,
            scope_digest=payload["scope_digest"],
            boundary_attested=False,
            event_digest=_digest(payload),
        )

    def payload(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["audience_scope"] = list(self.audience_scope)
        payload["attributes"] = dict(self.attributes)
        return payload

    def journal_payload(self) -> Dict[str, Any]:
        return {
            "schema": "ExternalCognitionJournalProjectionV2",
            "version": 2,
            "external_event_id": self.event_id,
            "external_event_digest": self.event_digest,
            "external_occurred_at": self.occurred_at,
            "kind": self.kind,
            "summary": self.summary,
            "attributes": dict(self.attributes),
            "producer_principal_id": self.producer_principal_id,
            "producer_revision": self.producer_revision,
            "subject_person_id": self.subject_person_id,
            "viewer_person_id": self.viewer_person_id,
            "viewer_scope": self.viewer_scope,
            "shareability": self.shareability,
            "audience_scope": list(self.audience_scope),
            "scope_digest": self.scope_digest,
            "boundary_attested": False,
            "evidence_status": "reported/unverified",
        }


class ExternalEventInboxStore:
    """Durable idempotency ledger for external cognition events."""

    def __init__(self, db_path: str) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS external_cognition_events (
                    event_id TEXT PRIMARY KEY,
                    event_digest TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    receipt_ref TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    journal_seq INTEGER,
                    journal_event_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.ProgrammingError:
                pass

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["event"] = json.loads(result.pop("event_json"))
        result["receipt"] = json.loads(result.pop("receipt_json"))
        return result

    def reserve(
        self,
        event: ExternalCognitionEventV1,
        *,
        now: Optional[datetime] = None,
    ) -> tuple[Dict[str, Any], bool]:
        observed = now or datetime.now(timezone.utc)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        stamp = observed.astimezone(timezone.utc).timestamp()
        receipt_ref = f"external-event-receipt:{event.event_digest[:24]}"
        accepted = {
            "schema": "ExternalEventIntakeReceiptV1",
            "version": 1,
            "receipt_ref": receipt_ref,
            "event_id": event.event_id,
            "event_digest": event.event_digest,
            "status": "accepted",
            "subject_person_id": event.subject_person_id,
            "viewer_person_id": event.viewer_person_id,
            "shareability": event.shareability,
            "scope_digest": event.scope_digest,
            "journal_seq": None,
            "journal_event_id": None,
            "accepted_at": _iso(observed),
        }
        encoded_event = _canonical(event.payload())
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM external_cognition_events WHERE event_id=?",
                (event.event_id,),
            ).fetchone()
            if row is not None:
                if (
                    row["event_digest"] != event.event_digest
                    or row["event_json"] != encoded_event
                ):
                    raise ExternalEventConflict(
                        "event_id replay changed immutable content"
                    )
                return self._row(row), False
            self._conn.execute(
                """INSERT INTO external_cognition_events
                   (event_id,event_digest,event_json,receipt_ref,state,
                    receipt_json,journal_seq,journal_event_id,created_at,updated_at)
                   VALUES (?,?,?,?, 'accepted',?,NULL,NULL,?,?)""",
                (
                    event.event_id, event.event_digest, encoded_event,
                    receipt_ref, _canonical(accepted), stamp, stamp,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM external_cognition_events WHERE event_id=?",
                (event.event_id,),
            ).fetchone()
        return self._row(row), True

    def complete_projection(
        self,
        event: ExternalCognitionEventV1,
        journal_record: Mapping[str, Any],
    ) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM external_cognition_events WHERE event_id=?",
                (event.event_id,),
            ).fetchone()
            if row is None:
                raise ValueError("external event receipt is unavailable")
            if row["event_digest"] != event.event_digest:
                raise ExternalEventConflict(
                    "event changed before journal projection"
                )
            if row["state"] == "projected":
                return self._row(row)["receipt"]
            receipt = json.loads(row["receipt_json"])
            receipt.update({
                "status": "projected",
                "journal_seq": int(journal_record["seq"]),
                "journal_event_id": str(journal_record["ulid"]),
                "projected_at": str(journal_record["recordedAt"]),
                "journal_retained": bool(journal_record.get("retained", True)),
            })
            self._conn.execute(
                """UPDATE external_cognition_events
                   SET state='projected',receipt_json=?,journal_seq=?,
                       journal_event_id=?,updated_at=? WHERE event_id=?
                       AND state='accepted'""",
                (
                    _canonical(receipt), receipt["journal_seq"],
                    receipt["journal_event_id"], time.time(), event.event_id,
                ),
            )
            self._conn.commit()
        return receipt


class ExternalEventIntake:
    """Reserve one external receipt, then project it to the host journal."""

    def __init__(
        self,
        store: ExternalEventInboxStore,
        *,
        journal_projector: Optional[Callable[..., Optional[Mapping[str, Any]]]] = None,
        journal_acknowledger: Optional[Callable[[str], bool]] = None,
    ) -> None:
        self.store = store
        if journal_projector is None:
            from colony_sidecar.events.journal import append_event_record
            journal_projector = append_event_record
        if journal_acknowledger is None:
            from colony_sidecar.events.journal import acknowledge_event_record
            journal_acknowledger = acknowledge_event_record
        self._project = journal_projector
        self._acknowledge = journal_acknowledger

    def close(self) -> None:
        self.store.close()

    def ingest(
        self,
        event: ExternalCognitionEventV1,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        row, _created = self.store.reserve(event, now=now)
        event_key = f"external-cognition:{event.event_id}"
        if row["state"] == "projected":
            # A process may have committed the inbox transaction and died
            # before releasing the journal's metadata-only handshake marker.
            self._acknowledge(event_key)
            return dict(row["receipt"])
        record = self._project(
            f"cognition.external.{event.kind}",
            event.journal_payload(),
            occurred_at=event.occurred_at,
            event_key=event_key,
        )
        if record is None:
            raise ExternalEventProjectionError(
                "external event receipt is durable; journal projection failed"
            )
        receipt = self.store.complete_projection(event, record)
        # Only the durable inbox commit can acknowledge the keyed journal
        # projection. A failed cleanup is harmless and retried on exact replay.
        self._acknowledge(event_key)
        return receipt


__all__ = [
    "EXTERNAL_JOURNAL_EVENT_ID_MAX_CHARS",
    "ExternalCognitionEventV1", "ExternalEventConflict",
    "ExternalEventInboxStore", "ExternalEventIntake",
    "ExternalEventProjectionError", "ExternalEventValidationError",
    "validate_external_journal_event_id",
    "validate_external_journal_projection",
]
