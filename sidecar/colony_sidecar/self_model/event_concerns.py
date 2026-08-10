"""Durable, scoped host-event to cognitive-concern reduction.

The host event journal is the source of truth.  This reducer never consumes
transient WebSocket buffers and never asks a model to guess scope, salience, or
deduplication.  Concern mutation, the idempotency receipt, and the durable
cursor are committed together by :class:`ConcernStore`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
import re
from typing import Any, Callable, Dict, Mapping, Optional

from colony_sidecar.events.journal import current_sequence, replay_events
from colony_sidecar.self_model.workspace import ConcernStore


_CONSUMER_ID = "workspace-concerns-v1"
_EXTERNAL_CONSUMER_ID = "workspace-external-concerns-v1"
_TURN_CONSUMER_ID = "workspace-turn-concerns-v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+\-]{0,127}$")
_SAFE_EVENT_TYPE = re.compile(r"^[a-z0-9][a-z0-9_.:\-]{0,127}$")
_SAFE_CHANNEL_LANE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,63}$")
_SAFE_SESSION_PREFIX = re.compile(r"^[a-z0-9][a-z0-9_.:@/+\-]{0,127}$")
_SHAREABILITY = {"owner_private", "subject_private", "shared", "public"}
_SUMMARY_FIELDS = ("summary", "description", "title", "reason", "observation", "service")
_REF_FIELDS = (
    "commitment_id", "project_id", "work_order_id", "step_id", "approval_id",
    "expectation_id", "surprise_id", "anomaly_id", "service_id", "source_ref",
)

_INTERNAL_TURN_CHANNELS = frozenset({
    "agent", "api", "cognition", "cron", "event", "health", "internal",
    "service", "system", "tool", "worker",
})


def event_concern_mode() -> str:
    value = os.environ.get("COLONY_EVENT_CONCERNS", "off").strip().lower()
    return value if value in {"off", "shadow", "live"} else "off"


def event_concerns_enabled() -> bool:
    return event_concern_mode() in {"shadow", "live"}


def external_event_concern_mode() -> str:
    """External-report concern producer mode; invalid/unset fails off."""

    value = os.environ.get(
        "COLONY_EXTERNAL_EVENT_CONCERNS", "off",
    ).strip().lower()
    return value if value in {"off", "shadow", "live"} else "off"


def external_event_concerns_enabled() -> bool:
    return external_event_concern_mode() in {"shadow", "live"}


def turn_concern_mode() -> str:
    """Completed-conversation concern producer mode; invalid/unset is off."""

    value = os.environ.get("COLONY_TURN_CONCERNS", "off").strip().lower()
    return value if value in {"off", "shadow", "live"} else "off"


def turn_concerns_enabled() -> bool:
    return turn_concern_mode() in {"shadow", "live"}


def _turn_channel_config() -> tuple[tuple[str, ...], bool]:
    """Return the exact configured channel-lane allowlist.

    Channel IDs are ``lane:conversation``.  Configuration names only the lane
    because per-conversation identifiers are dynamic.  One malformed entry
    invalidates the complete allowlist rather than broadening a typo.
    """

    raw = os.environ.get("COLONY_TURN_CONCERNS_CHANNELS", "")
    if not raw.strip():
        return (), False
    parts = raw.split(",")
    if any(not item.strip() for item in parts):
        return (), False
    values = tuple(item.strip().lower() for item in parts)
    if any(not _SAFE_CHANNEL_LANE.fullmatch(item) for item in values):
        return (), False
    return tuple(dict.fromkeys(values)), True


def turn_concern_channels() -> tuple[str, ...]:
    return _turn_channel_config()[0]


def _turn_excluded_platform_config() -> tuple[tuple[str, ...], bool]:
    raw = os.environ.get(
        "COLONY_TURN_CONCERNS_EXCLUDED_PLATFORMS", "",
    )
    if not raw.strip():
        return (), True
    parts = raw.split(",")
    if any(not item.strip() for item in parts):
        return (), False
    values = tuple(item.strip().lower() for item in parts)
    if any(not _SAFE_CHANNEL_LANE.fullmatch(item) for item in values):
        return (), False
    return tuple(dict.fromkeys(values)), True


def turn_concern_excluded_platforms() -> tuple[str, ...]:
    return _turn_excluded_platform_config()[0]


def _turn_excluded_session_prefix_config() -> tuple[tuple[str, ...], bool]:
    raw = os.environ.get(
        "COLONY_TURN_CONCERNS_EXCLUDED_SESSION_PREFIXES", "",
    )
    if not raw.strip():
        return (), True
    parts = raw.split(",")
    if any(not item.strip() for item in parts):
        return (), False
    values = tuple(item.strip().lower() for item in parts)
    if any(not _SAFE_SESSION_PREFIX.fullmatch(item) for item in values):
        return (), False
    return tuple(dict.fromkeys(values)), True


def turn_concern_excluded_session_prefixes() -> tuple[str, ...]:
    return _turn_excluded_session_prefix_config()[0]


def _bootstrap_mode() -> str:
    value = os.environ.get("COLONY_EVENT_CONCERNS_BOOTSTRAP", "tail").strip().lower()
    return value if value in {"tail", "replay"} else "tail"


def _external_bootstrap_mode() -> str:
    # External intake already made these reports durable while this consumer
    # was off.  First enablement therefore replays retained history instead of
    # silently losing the off-period backlog.
    return "replay"


def _turn_bootstrap_mode() -> str:
    value = os.environ.get(
        "COLONY_TURN_CONCERNS_BOOTSTRAP", "tail",
    ).strip().lower()
    return value if value in {"tail", "replay"} else "tail"


def _owner_person_id() -> str:
    return (
        os.environ.get("COLONY_OWNER_PERSON_ID", "").strip()
        or os.environ.get("COLONY_OWNER_CONTACT_ID", "").strip()
        or "owner"
    )[:128]


def _clean_id(value: Any) -> str:
    candidate = str(value or "").strip()
    return candidate if _SAFE_ID.fullmatch(candidate) else ""


def _clean_text(value: Any, maximum: int = 300) -> str:
    raw = str(value or "")
    cleaned = " ".join(
        "".join(char if 32 <= ord(char) < 127 else " " for char in raw).split()
    )
    return cleaned[:maximum]


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class EventProjection:
    operation: str
    kind: str
    summary: str
    salience: float
    dedup_key: str
    sources: tuple[str, ...]
    subject_person_id: str
    viewer_scope: str
    shareability: str
    occurred_at: str
    material_digest: str
    note: str = ""
    producer_name: str = "event_concerns"
    producer_mode: str = ""
    producer_revision: str = "event-concern-reducer-v2"
    external_event_projection: bool = False

    def store_payload(self) -> Dict[str, Any]:
        return {
            "operation": self.operation,
            "kind": self.kind,
            "summary": self.summary,
            "salience": self.salience,
            "dedup_key": self.dedup_key,
            "sources": list(self.sources),
            "subject_person_id": self.subject_person_id,
            "viewer_scope": self.viewer_scope,
            "shareability": self.shareability,
            "occurred_at": self.occurred_at,
            "note": self.note,
            "producer_name": self.producer_name,
            "producer_mode": self.producer_mode or event_concern_mode(),
            "producer_revision": self.producer_revision,
            "external_event_projection": self.external_event_projection,
        }


def _event_identity(event: Mapping[str, Any]) -> tuple[int, str, str, str, Mapping[str, Any]]:
    sequence = int(event.get("seq") or 0)
    event_id = _clean_id(event.get("ulid")) or f"journal-seq-{sequence}"
    event_type = str(event.get("type") or "").strip().lower()
    occurred_at = _clean_text(
        event.get("occurredAt") or event.get("recordedAt") or "", 64,
    )
    data = event.get("data")
    if sequence < 1 or not _SAFE_EVENT_TYPE.fullmatch(event_type):
        raise ValueError("malformed journal event identity")
    if not isinstance(data, Mapping):
        raise ValueError("malformed journal event data")
    return sequence, event_id, event_type, occurred_at, data


def _subject_and_scope(data: Mapping[str, Any]) -> tuple[str, str, str]:
    subject = ""
    for key in ("subject_person_id", "person_id", "contact_id"):
        subject = _clean_id(data.get(key))
        if subject:
            break
    subject = subject or _owner_person_id()

    requested = str(
        data.get("shareability") or data.get("privacy_scope") or "owner_private"
    ).strip().lower()
    shareability = requested if requested in _SHAREABILITY else "owner_private"
    # A payload can originate at a transport boundary.  Shared/public is
    # accepted only when a server-side boundary explicitly attested it.
    if shareability in {"shared", "public"} and data.get("boundary_attested") is not True:
        shareability = "owner_private"
    if shareability == "subject_private":
        viewer_scope = f"person:{subject}"
    elif shareability in {"shared", "public"}:
        viewer_scope = shareability
    else:
        viewer_scope = "owner"
    return subject, viewer_scope, shareability


def _entity(data: Mapping[str, Any], event_type: str, summary: str) -> str:
    if event_type.startswith(("work_order.", "work.")):
        ordered_fields = (
            "work_order_id", "step_id", "project_id",
            *tuple(key for key in _REF_FIELDS if key not in {
                "work_order_id", "step_id", "project_id",
            }),
        )
    elif event_type.startswith("project."):
        ordered_fields = (
            "project_id",
            *tuple(key for key in _REF_FIELDS if key != "project_id"),
        )
    else:
        ordered_fields = _REF_FIELDS
    for key in ordered_fields:
        value = _clean_id(data.get(key))
        if value:
            return value
    generic = _clean_id(data.get("id"))
    if generic:
        return generic
    return _digest({"type": event_type, "summary": summary})[:24]


def _summary(event_type: str, data: Mapping[str, Any]) -> str:
    for key in _SUMMARY_FIELDS:
        value = _clean_text(data.get(key))
        if value:
            return value
    return event_type.replace(".", " ").replace("_", " ")


def _source_refs(sequence: int, event_id: str, data: Mapping[str, Any]) -> tuple[str, ...]:
    values = [f"journal:{sequence}:{event_id}"]
    for key in _REF_FIELDS:
        value = _clean_id(data.get(key))
        if value:
            values.append(f"{key.removesuffix('_id')}:{value}")
    return tuple(dict.fromkeys(values))[:20]


def project_event(event: Mapping[str, Any]) -> tuple[Optional[EventProjection], str, str]:
    """Return ``(projection, skip_reason, raw_digest)`` for one host event."""

    sequence, event_id, event_type, occurred_at, data = _event_identity(event)
    raw_digest = _digest({
        "type": event_type,
        "occurred_at": occurred_at,
        "data": data,
    })
    summary = _summary(event_type, data)
    subject, viewer_scope, shareability = _subject_and_scope(data)
    sources = _source_refs(sequence, event_id, data)

    category = ""
    operation = "upsert"
    kind = "thread"
    salience = 0.5

    if event_type == "commitment.overdue":
        category, kind, salience = "commitment", "goal", 0.75
    elif event_type in {"commitment.fulfilled", "commitment.cancelled"}:
        category, operation, kind = "commitment", "resolve", "goal"
    elif event_type in {"surprise.high", "surprise.accumulation", "expectation.missed"}:
        category, kind, salience = "expectation", "question", 0.72
    elif event_type in {"expectation.hit", "expectation.resolved"}:
        category, operation, kind = "expectation", "resolve", "question"
    elif "anomaly" in event_type or event_type.startswith("health.") \
            or event_type.startswith("service."):
        category = "service" if event_type.startswith(("health.", "service.")) else "anomaly"
        kind = "maintenance" if category == "service" else "anomaly"
        terminal = event_type.endswith((".recovered", ".resolved", ".healthy"))
        operation = "resolve" if terminal else "upsert"
        salience = 0.78 if operation == "upsert" else 0.0
    elif event_type.startswith(("project.", "work_order.", "work.")):
        category, kind = "project", "goal"
        status = str(data.get("status") or event_type.rsplit(".", 1)[-1]).lower()
        if status in {
            "blocked", "failed", "overdue", "stalled", "unverified",
            "verification_pending", "abandoned",
        }:
            operation, salience = "upsert", 0.8
        elif status in {"completed", "verified", "cancelled", "resolved"}:
            operation, salience = "resolve", 0.0
        else:
            return None, "non_material_project_event", raw_digest
    elif event_type.startswith("approval."):
        category, kind = "approval", "maintenance"
        status = str(data.get("status") or event_type.rsplit(".", 1)[-1]).lower()
        if status in {"pending", "required", "awaiting_approval"}:
            operation, salience = "upsert", 0.7
        elif status in {"approved", "denied", "expired", "cancelled", "consumed"}:
            operation, salience = "resolve", 0.0
        else:
            return None, "non_material_approval_event", raw_digest
    elif event_type in {"relationship.follow_up_due", "contact.cadence_overdue"}:
        category, kind, salience = "relationship", "thread", 0.58
    elif event_type in {"relationship.followed_up", "contact.cadence_satisfied"}:
        category, operation, kind = "relationship", "resolve", "thread"
    else:
        return None, "unmapped_event_type", raw_digest

    entity = _entity(data, event_type, summary)
    dedup_key = f"event:{category}:{subject}:{entity}"[:200]
    material = {
        "operation": operation,
        "category": category,
        "entity": entity,
        "subject": subject,
        "summary": summary if operation == "upsert" else "",
        "shareability": shareability,
        "status": str(data.get("status") or event_type.rsplit(".", 1)[-1]).lower(),
    }
    projection = EventProjection(
        operation=operation,
        kind=kind,
        summary=summary,
        salience=salience,
        dedup_key=dedup_key,
        sources=sources,
        subject_person_id=subject,
        viewer_scope=viewer_scope,
        shareability=shareability,
        occurred_at=occurred_at,
        material_digest=_digest(material),
        note=(f"resolved by {event_type}" if operation == "resolve" else ""),
    )
    return projection, "", raw_digest


def _turn_text(value: Any, maximum: int = 300) -> str:
    if type(value) is not str:
        return ""
    return " ".join(
        "".join(
            char if char >= " " and char != "\x7f" else " "
            for char in value
        ).split()
    )[:maximum]


def _turn_id(value: Any) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return ""
    return value


def _turn_session(value: Any) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return ""
    return value


def _turn_channel(value: Any) -> tuple[str, str]:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 512
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return "", ""
    lane = value.split(":", 1)[0].lower()
    if not _SAFE_CHANNEL_LANE.fullmatch(lane):
        return "", ""
    return value, lane


def _channel_lane_matches(lane: str, blocked: frozenset[str]) -> bool:
    return any(
        lane == value
        or any(lane.startswith(value + separator) for separator in ("-", "_", "."))
        for value in blocked
    )


def _configured_turn_owner() -> str:
    value = (
        os.environ.get("COLONY_OWNER_PERSON_ID", "").strip()
        or os.environ.get("COLONY_OWNER_CONTACT_ID", "").strip()
    )
    return value if _SAFE_ID.fullmatch(value) else ""


def project_turn_concern_hold_reason(project: Any, concern_store: Any) -> str:
    """Return the runtime hold for exact cognition-spine turn provenance."""

    if str(getattr(project, "source", "") or "") != "cognition_spine":
        return ""
    concern_id = str(getattr(project, "concern_id", "") or "")
    if not concern_id:
        return ""
    if concern_store is None:
        return "project_hold_reason_unavailable"
    concern = concern_store.get(concern_id)
    if concern is None:
        return "project_hold_reason_unavailable"
    if str(getattr(concern, "producer_name", "") or "") != "turn_concerns":
        return ""
    if turn_concern_mode() != "live":
        return "turn_concerns_current_mode_not_live"
    return ""


def project_conversation_turn(
    event: Mapping[str, Any],
) -> tuple[Optional[EventProjection], str, str]:
    """Project one server-attested completed human turn into a concern.

    Conversation prose is untrusted evidence.  This adapter accepts no action,
    capability, approval, receipt, or effect fields; it only preserves the
    server-produced subject and private viewer lane for the existing policy
    spine to evaluate later.
    """

    sequence, event_id, event_type, occurred_at, data = _event_identity(event)
    raw_digest = _digest({
        "type": event_type,
        "occurred_at": occurred_at,
        "data": data,
    })
    if event_type != "conversation.turn":
        return None, "not_conversation_turn", raw_digest
    if data.get("turn_scope_schema") != "ConversationTurnJournalScopeV1":
        return None, "missing_turn_scope_envelope", raw_digest

    session_id = _turn_session(data.get("session_id"))
    if not session_id:
        return None, "missing_turn_session", raw_digest
    excluded_prefixes, session_config_valid = (
        _turn_excluded_session_prefix_config()
    )
    if not session_config_valid:
        return None, "session_prefix_config_invalid", raw_digest
    if any(session_id.lower().startswith(prefix) for prefix in excluded_prefixes):
        return None, "excluded_session_prefix", raw_digest

    turn_id = _turn_id(data.get("turn_id"))
    if not turn_id:
        return None, "missing_turn_id", raw_digest
    turn_id_source = data.get("turn_id_source")
    if (
        data.get("turn_id_attested") is not True
        or turn_id_source not in {"client_idempotency_key", "server_digest"}
    ):
        return None, "turn_id_not_attested", raw_digest

    channel_id, channel_lane = _turn_channel(data.get("channel_id"))
    if not channel_lane:
        return None, "missing_channel", raw_digest
    if _channel_lane_matches(channel_lane, _INTERNAL_TURN_CHANNELS):
        return None, "internal_channel", raw_digest
    if channel_lane not in turn_concern_channels():
        return None, "channel_not_allowed", raw_digest

    source_platform = data.get("source_platform")
    if (
        type(source_platform) is not str
        or not _SAFE_CHANNEL_LANE.fullmatch(source_platform)
        or data.get("source_platform_attested") is not True
    ):
        return None, "source_platform_not_attested", raw_digest
    excluded_platforms, platform_config_valid = (
        _turn_excluded_platform_config()
    )
    if not platform_config_valid:
        return None, "excluded_platform_config_invalid", raw_digest
    if source_platform in excluded_platforms:
        return None, "excluded_source_platform", raw_digest

    contact = data.get("contact_id")
    subject_value = data.get("subject_person_id")
    subject = (
        subject_value
        if type(subject_value) is str and _SAFE_ID.fullmatch(subject_value)
        else ""
    )
    if (
        not subject
        or type(contact) is not str
        or contact != subject
        or subject == "system"
    ):
        return None, "missing_attested_subject", raw_digest
    if data.get("identity_attested") is not True:
        return None, "turn_identity_not_attested", raw_digest
    if data.get("scope_attested") is not True:
        return None, "turn_scope_not_attested", raw_digest

    principal = data.get("source_principal_id")
    attribution = data.get("attribution_method")
    if (
        type(principal) is not str
        or not _SAFE_ID.fullmatch(principal)
        or type(attribution) is not str
        or not _SAFE_ID.fullmatch(attribution)
    ):
        return None, "missing_sender_attestation", raw_digest

    owner = _configured_turn_owner()
    if not owner:
        return None, "owner_identity_unconfigured", raw_digest
    expected_scope = "owner" if subject == owner else f"person:{subject}"
    expected_sharing = (
        "owner_private" if subject == owner else "subject_private"
    )
    viewer_scope = data.get("viewer_scope")
    shareability = data.get("shareability")
    if (
        viewer_scope != expected_scope
        or shareability != expected_sharing
        or data.get("boundary_attested") is not False
    ):
        return None, "unsafe_turn_scope", raw_digest

    summary = _turn_text(data.get("summary"))
    if not summary:
        return None, "missing_turn_summary", raw_digest
    turn_digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
    sources = [
        f"journal:{sequence}:{event_id}",
        f"turn:{turn_digest}",
        f"channel:{channel_lane}",
        f"platform:{source_platform}",
        f"principal:{principal}",
    ]
    session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    sources.append(f"conversation:{session_digest}")
    material = {
        "namespace": "turn_concerns",
        "turn_digest": turn_digest,
        "turn_id_source": turn_id_source,
        "session_digest": session_digest,
        "channel_lane": channel_lane,
        "source_platform": source_platform,
        "subject_person_id": subject,
        "viewer_scope": viewer_scope,
        "shareability": shareability,
        "summary": summary,
        "source_principal_id": principal,
        "attribution_method": attribution,
        "evidence_status": "reported/unverified",
    }
    return EventProjection(
        operation="upsert",
        kind="thread",
        summary=summary,
        salience=0.5,
        dedup_key=f"turn:{subject}:{turn_digest}"[:200],
        sources=tuple(dict.fromkeys(sources)),
        subject_person_id=subject,
        viewer_scope=str(viewer_scope),
        shareability=str(shareability),
        occurred_at=occurred_at,
        material_digest=_digest(material),
        producer_name="turn_concerns",
        producer_mode=turn_concern_mode(),
        producer_revision="conversation-turn-concern-reducer-v1",
    ), "", raw_digest


def _external_occurred_at(value: Any) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("external cognition occurredAt is not canonical text")
    text = value
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError("external cognition occurredAt is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("external cognition occurredAt requires timezone authority")
    return text


def project_external_event(
    event: Mapping[str, Any],
) -> tuple[Optional[EventProjection], str, str]:
    """Project one strictly validated external report into its own namespace.

    Terminal reports may only close the concern with the exact deterministic
    subject/producer/kind/entity identity.  They never join a Project, action,
    grant, effect, or competence ledger.
    """

    if type(event.get("seq")) is not int or event.get("seq") < 1:
        raise ValueError("external cognition journal sequence is not canonical")
    from colony_sidecar.cognition.external_events import (
        validate_external_journal_event_id,
        validate_external_journal_projection,
    )

    validate_external_journal_event_id(event.get("ulid"))
    sequence, event_id, event_type, _occurred, data = _event_identity(event)
    raw_digest = _digest({
        "type": event_type,
        "occurred_at": event.get("occurredAt"),
        "data": data,
    })
    if not event_type.startswith("cognition.external."):
        return None, "not_external_cognition_event", raw_digest
    if type(event.get("ulid")) is not str or event.get("ulid") != event_id:
        raise ValueError("external cognition journal ID is not canonical")
    if type(event.get("type")) is not str or event.get("type") != event_type:
        raise ValueError("external cognition journal type is not canonical")

    projected = validate_external_journal_projection(event_type, data)
    occurred_at = _external_occurred_at(event.get("occurredAt"))
    if occurred_at != projected["external_occurred_at"]:
        raise ValueError(
            "external cognition host time differs from its server projection"
        )
    kind = projected["kind"]
    attributes = dict(projected["attributes"])
    producer = projected["producer_principal_id"]
    subject = projected["subject_person_id"]
    sharing = projected["shareability"]
    viewer_scope = projected["viewer_scope"]
    summary = (
        attributes["observation"]
        if kind == "text_turn_observation"
        else projected["summary"]
    )

    entity_fields = {
        "action_outcome": ("action_id",),
        "delivery_outcome": ("delivery_ref", "message_ref"),
        "service_state": ("service",),
        "approval_state": ("request_id",),
        "operator_reaction": ("target_ref",),
        "text_turn_observation": ("turn_id",),
    }
    entity = next(
        str(attributes.get(field) or "")
        for field in entity_fields[kind]
        if attributes.get(field)
    )
    state_field = {
        "action_outcome": "outcome",
        "delivery_outcome": "outcome",
        "service_state": "state",
        "approval_state": "state",
        "operator_reaction": "reaction",
        "text_turn_observation": "channel",
    }[kind]
    state = str(attributes[state_field])
    resolve_states = {
        "action_outcome": {"succeeded", "cancelled"},
        "delivery_outcome": {"delivered", "read", "acknowledged"},
        "service_state": {"healthy"},
        "approval_state": {"approved", "rejected", "expired", "cancelled"},
        "operator_reaction": {"positive", "neutral", "dismissed"},
        "text_turn_observation": set(),
    }
    operation = "resolve" if state in resolve_states[kind] else "upsert"
    concern_kind = (
        "thread"
        if kind in {"operator_reaction", "text_turn_observation"}
        else "maintenance"
    )
    salience = {
        "action_outcome": 0.78,
        "delivery_outcome": 0.66,
        "service_state": 0.76,
        "approval_state": 0.62,
        "operator_reaction": 0.58,
        "text_turn_observation": 0.5,
    }[kind] if operation == "upsert" else 0.0
    identity = {
        "namespace": "external_event_concerns",
        "subject_person_id": subject,
        "producer_principal_id": producer,
        "kind": kind,
        "entity": entity,
    }
    dedup_key = f"external:{kind}:{_digest(identity)}"
    sources = (
        f"journal:{sequence}:{event_id}",
        f"xevent:{projected['external_event_id']}",
        f"xdigest:{projected['external_event_digest']}",
        f"external_producer:{producer}",
        f"external_revision:{projected['producer_revision']}",
        f"external_kind:{kind}",
        f"xentity:{entity}",
    )
    material = {
        **identity,
        "operation": operation,
        "state": state,
        "summary": summary if operation == "upsert" else "",
        "attributes": attributes,
        "external_event_digest": projected["external_event_digest"],
        "external_producer_revision": projected["producer_revision"],
        "reducer_revision": "external-event-concern-reducer-v1",
        "scope_digest": projected["scope_digest"],
        "boundary_attested": False,
        "evidence_status": "reported/unverified",
    }
    return EventProjection(
        operation=operation,
        kind=concern_kind,
        summary=summary,
        salience=salience,
        dedup_key=dedup_key,
        sources=sources,
        subject_person_id=subject,
        viewer_scope=viewer_scope,
        shareability=sharing,
        occurred_at=occurred_at,
        material_digest=_digest(material),
        note=(
            f"resolved by reported external {kind} state {state}"
            if operation == "resolve" else ""
        ),
        producer_name="external_event_concerns",
        producer_mode=external_event_concern_mode(),
        producer_revision="external-event-concern-reducer-v1",
        external_event_projection=True,
    ), "", raw_digest


class EventConcernReducer:
    """Idempotently consume the durable host journal into ``ConcernStore``."""

    def __init__(
        self,
        store: ConcernStore,
        *,
        consumer_id: str = _CONSUMER_ID,
        replay_fn: Callable[..., Mapping[str, Any]] = replay_events,
        current_sequence_fn: Callable[[], int] = current_sequence,
        projection_fn: Callable[
            [Mapping[str, Any]],
            tuple[Optional[EventProjection], str, str],
        ] = project_event,
        mode_fn: Callable[[], str] = event_concern_mode,
        bootstrap_mode_fn: Callable[[], str] = _bootstrap_mode,
        gap_policy_env: str = "COLONY_EVENT_CONCERNS_GAP_POLICY",
        malformed_skip_reason: str = "",
    ) -> None:
        self.store = store
        self.consumer_id = consumer_id
        self._replay = replay_fn
        self._current_sequence = current_sequence_fn
        self._project = projection_fn
        self._mode = mode_fn
        self._bootstrap = bootstrap_mode_fn
        self._gap_policy_env = str(gap_policy_env)
        self._malformed_skip_reason = str(malformed_skip_reason or "")[:100]

    @property
    def mode(self) -> str:
        return self._mode()

    def status(self) -> Dict[str, Any]:
        state = self.store.event_reducer_status(self.consumer_id)
        state.update({
            "enabled": self.mode != "off",
            "mode": self.mode,
            "consumer_id": self.consumer_id,
            "healthy": not bool(state.get("last_error")),
            "journal_high_water": int(self._current_sequence()),
        })
        return state

    @staticmethod
    def _replay_integrity_error(snapshot: Mapping[str, Any]) -> str:
        if snapshot.get("replayError"):
            return "event_journal_unavailable"
        try:
            corrupt = int(snapshot.get("corruptCount") or 0)
            first = int(snapshot.get("firstAvailableSeq") or 0)
            high = int(snapshot.get("journalLastSeq") or 0)
        except (TypeError, ValueError):
            return "event_journal_metadata_invalid"
        if corrupt < 0 or first < 0 or high < 0 or (first and first > high):
            return "event_journal_metadata_invalid"
        if corrupt:
            return f"journal_corruption_detected:{corrupt}"
        if not isinstance(snapshot.get("events") or (), (list, tuple)):
            return "event_journal_metadata_invalid"
        return ""

    def _gap_policy(self) -> str:
        return os.environ.get(
            self._gap_policy_env, "stop",
        ).strip().lower()

    def _stop(self, error: str, *, processed: int = 0,
              cursor: Optional[int] = None) -> Dict[str, Any]:
        self.store.set_event_error(self.consumer_id, error)
        result: Dict[str, Any] = {
            "enabled": True, "processed": int(processed), "error": error,
        }
        if cursor is not None:
            result["cursor"] = int(cursor)
        return result

    def _initialize(self) -> Dict[str, Any]:
        snapshot = self._replay(after_seq=0, limit=1)
        bootstrap = self._bootstrap()
        error = self._replay_integrity_error(snapshot)
        if error:
            initial = 0
        else:
            first = int(snapshot.get("firstAvailableSeq") or 0)
            high_water = int(snapshot.get("journalLastSeq") or 0)
            initial = high_water if bootstrap == "tail" else max(0, first - 1)
        cursor = self.store.initialize_event_cursor(
            self.consumer_id,
            initial,
            bootstrap_mode=bootstrap,
        )
        result = {
            "bootstrapped": True, "bootstrap_mode": bootstrap, "cursor": cursor,
        }
        if error:
            self.store.set_event_error(self.consumer_id, error)
            result["error"] = error
        return result

    def run_once(self, *, limit: int = 100) -> Dict[str, Any]:
        if self.mode == "off":
            return {"enabled": False, "processed": 0}
        cursor = self.store.event_cursor(self.consumer_id)
        if cursor is None:
            initialized = self._initialize()
            cursor = int(initialized["cursor"])
            if initialized.get("error"):
                return self._stop(str(initialized["error"]), cursor=cursor)
            if initialized["bootstrap_mode"] == "tail":
                return {**initialized, "enabled": True, "processed": 0}

        batch = self._replay(after_seq=cursor, limit=max(1, min(500, int(limit))))
        integrity_error = self._replay_integrity_error(batch)
        if integrity_error:
            return self._stop(integrity_error, cursor=cursor)
        journal_high = int(batch.get("journalLastSeq") or 0)
        if cursor > journal_high:
            return self._stop(
                f"event_journal_rewind:{cursor}:{journal_high}",
                cursor=cursor,
            )

        first = int(batch.get("firstAvailableSeq") or 0)
        if first and cursor < first - 1:
            message = f"journal_retention_gap:{cursor}:{first}"
            if self._gap_policy() == "acknowledge":
                cursor = self.store.acknowledge_event_gap(
                    self.consumer_id,
                    prior_cursor=cursor,
                    resume_after=first - 1,
                    reason=message,
                )
                batch = self._replay(
                    after_seq=cursor, limit=max(1, min(500, int(limit))),
                )
                integrity_error = self._replay_integrity_error(batch)
                if integrity_error:
                    return self._stop(integrity_error, cursor=cursor)
                journal_high = int(batch.get("journalLastSeq") or 0)
                if cursor > journal_high:
                    return self._stop(
                        f"event_journal_rewind:{cursor}:{journal_high}",
                        cursor=cursor,
                    )
            else:
                return self._stop(message, cursor=cursor)

        counts: Dict[str, int] = {}
        last_seq = cursor
        for raw in batch.get("events") or []:
            try:
                if not isinstance(raw, Mapping):
                    raise ValueError("journal event is not an object")
                sequence = int(raw.get("seq") or 0)
                if sequence <= last_seq:
                    raise ValueError("journal event sequence is not increasing")
                if sequence != last_seq + 1:
                    message = f"journal_sequence_gap:{last_seq}:{sequence}"
                    if self._gap_policy() != "acknowledge":
                        return self._stop(
                            message, processed=sum(counts.values()),
                            cursor=last_seq,
                        )
                    last_seq = self.store.acknowledge_event_gap(
                        self.consumer_id,
                        prior_cursor=last_seq,
                        resume_after=sequence - 1,
                        reason=message,
                    )
                sequence, event_id, event_type, _, _ = _event_identity(raw)
                projection, skip_reason, raw_digest = self._project(raw)
                result = self.store.apply_event(
                    consumer_id=self.consumer_id,
                    event_seq=sequence,
                    event_id=event_id,
                    event_type=event_type,
                    material_digest=(
                        projection.material_digest if projection is not None else raw_digest
                    ),
                    projection=(projection.store_payload() if projection is not None else None),
                    skip_reason=skip_reason,
                )
            except Exception as exc:
                # If the journal supplied a usable sequence, record malformed
                # content as skipped and advance.  Identity-less records stop
                # the batch because advancing would guess a cursor.
                sequence = int(raw.get("seq") or 0) if isinstance(raw, Mapping) else 0
                if sequence < 1:
                    self.store.set_event_error(self.consumer_id, "malformed_event_without_sequence")
                    return {
                        "enabled": True,
                        "processed": sum(counts.values()),
                        "error": "malformed_event_without_sequence",
                    }
                event_id = _clean_id(raw.get("ulid")) or f"journal-seq-{sequence}"
                event_type = str(raw.get("type") or "malformed")[:128]
                result = self.store.apply_event(
                    consumer_id=self.consumer_id,
                    event_seq=sequence,
                    event_id=event_id,
                    event_type=(event_type if _SAFE_EVENT_TYPE.fullmatch(event_type) else "malformed"),
                    material_digest=_digest({"malformed": raw}),
                    projection=None,
                    skip_reason=(
                        self._malformed_skip_reason
                        or f"malformed_event:{type(exc).__name__}"
                    ),
                )
            disposition = str(result.get("disposition") or result.get("status") or "unknown")
            counts[disposition] = counts.get(disposition, 0) + 1
            last_seq = max(last_seq, int(result.get("cursor") or last_seq))

        journal_high = int(batch.get("journalLastSeq") or 0)
        if not bool(batch.get("hasMore")) and journal_high > last_seq:
            message = f"journal_sequence_gap:{last_seq}:{journal_high + 1}"
            if self._gap_policy() != "acknowledge":
                return self._stop(
                    message, processed=sum(counts.values()), cursor=last_seq,
                )
            last_seq = self.store.acknowledge_event_gap(
                self.consumer_id,
                prior_cursor=last_seq,
                resume_after=journal_high,
                reason=message,
            )

        return {
            "enabled": True,
            "processed": sum(counts.values()),
            "dispositions": counts,
            "cursor": last_seq,
            "has_more": bool(batch.get("hasMore")),
        }


class ExternalEventConcernReducer(EventConcernReducer):
    """Independent durable consumer for reported external cognition events."""

    def __init__(
        self,
        store: ConcernStore,
        *,
        consumer_id: str = _EXTERNAL_CONSUMER_ID,
        replay_fn: Callable[..., Mapping[str, Any]] = replay_events,
        current_sequence_fn: Callable[[], int] = current_sequence,
    ) -> None:
        super().__init__(
            store,
            consumer_id=consumer_id,
            replay_fn=replay_fn,
            current_sequence_fn=current_sequence_fn,
            projection_fn=project_external_event,
            mode_fn=external_event_concern_mode,
            bootstrap_mode_fn=_external_bootstrap_mode,
            gap_policy_env="COLONY_EXTERNAL_EVENT_CONCERNS_GAP_POLICY",
            malformed_skip_reason="malformed_event:ValueError",
        )


class ConversationTurnConcernReducer(EventConcernReducer):
    """Independent cursor for eligible, server-attested completed turns."""

    def __init__(
        self,
        store: ConcernStore,
        *,
        consumer_id: str = _TURN_CONSUMER_ID,
        replay_fn: Callable[..., Mapping[str, Any]] = replay_events,
        current_sequence_fn: Callable[[], int] = current_sequence,
    ) -> None:
        super().__init__(
            store,
            consumer_id=consumer_id,
            replay_fn=replay_fn,
            current_sequence_fn=current_sequence_fn,
            projection_fn=project_conversation_turn,
            mode_fn=turn_concern_mode,
            bootstrap_mode_fn=_turn_bootstrap_mode,
            gap_policy_env="COLONY_TURN_CONCERNS_GAP_POLICY",
            malformed_skip_reason="malformed_turn_event",
        )

    @staticmethod
    def _config_error() -> str:
        _channels, channels_valid = _turn_channel_config()
        _prefixes, prefixes_valid = _turn_excluded_session_prefix_config()
        _platforms, platforms_valid = _turn_excluded_platform_config()
        if not channels_valid:
            return "turn_concern_channels_config_invalid"
        if not prefixes_valid:
            return "turn_concern_session_prefix_config_invalid"
        if not platforms_valid:
            return "turn_concern_excluded_platform_config_invalid"
        if not _configured_turn_owner():
            return "turn_concern_owner_identity_config_invalid"
        return ""

    def run_once(self, *, limit: int = 100) -> Dict[str, Any]:
        if self.mode == "off":
            return super().run_once(limit=limit)
        error = self._config_error()
        if error:
            return self._stop(error)
        # A corrected configuration immediately releases a prior config stop;
        # the generic reducer then initializes/replays from the retained event.
        self.store.set_event_error(self.consumer_id, "")
        return super().run_once(limit=limit)

    def status(self) -> Dict[str, Any]:
        state = super().status()
        channels, channels_valid = _turn_channel_config()
        state["allowed_channels"] = list(channels)
        state["channel_config_valid"] = channels_valid
        prefixes, valid = _turn_excluded_session_prefix_config()
        state["excluded_session_prefixes"] = list(prefixes)
        state["session_prefix_config_valid"] = valid
        platforms, platforms_valid = _turn_excluded_platform_config()
        state["excluded_platforms"] = list(platforms)
        state["excluded_platform_config_valid"] = platforms_valid
        state["owner_identity_configured"] = bool(_configured_turn_owner())
        config_error = self._config_error() if self.mode != "off" else ""
        state["config_error"] = config_error
        if config_error:
            state["last_error"] = config_error
            state["healthy"] = False
        return state
