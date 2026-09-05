"""Governed Colony sidecar integration for Hermes.

The general plugin is deliberately a narrow transport adapter.  It exposes
private legacy reads only to a transport-attested owner/system turn, converts
every enabled model-requested effect into an immutable
``HermesToolActionIntentV1``, and writes
one participant-bound turn observation.  Colony's memory provider remains the
canonical context path for guests.

No import performs I/O. Registration reads configuration and initializes the
private local turn outbox, but does not call Colony, start a subscriber, modify
Hermes configuration, or mutate process environment.
"""

from __future__ import annotations

import copy
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import logging
import os
import re
import threading
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlsplit
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .colony_hostworker.catalog import (
    ACTION_MODEL_TOOL_SCHEMAS as _CATALOG_ACTION_MODEL_TOOL_SCHEMAS,
    ACTION_TOOL_NAMES as _CATALOG_ACTION_TOOL_NAMES,
    identifier_model_schema as _identifier_model_schema,
    validate_tool_args as _validate_action_tool_args,
)
from .client import (
    ColonyClient,
    PrivateSQLitePathError,
    TurnOutbox,
    TurnOutboxConflict,
    TurnOutboxFull,
    TurnOutboxPayloadError,
    derive_hermes_turn_id,
)
from .slash import SLASH_COMMANDS


logger = logging.getLogger(__name__)


SUPPORTED_HERMES_TURN_FINALIZER = {
    "tag": "v2026.7.7.2",
    "commit": "9de9c25f620ff7f1ce0fd5457d596052d5159596",
    "sha256": "01602214acdb686338fa93580e3fe6ae1bdbc4731f246df0ba1f749ca2930663",
    "transform_precedes_post": True,
    "post_receives_transformed_response": True,
}


def _canonical_json(value: Any) -> str:
    """Return the one serialization used by every governed digest."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _parameters(
    properties: Mapping[str, Any], required: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


# Governed action schemas come directly from colony_hostworker.catalog, the
# authoritative catalog.  Reads and owner-message intents remain local because
# they are not part of that governed-action execution boundary.  The merged
# model catalog is sorted before its exact JSON shape is hashed for preflight.
_LOCAL_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "colony_autonomy_status",
        "description": "Read bounded Colony autonomy status in an attested private scope.",
        "parameters": _parameters({}),
    },
    {
        "name": "colony_get_initiative",
        "description": "Read one Colony initiative in an attested private viewer scope.",
        "parameters": _parameters({
            "initiative_id": _identifier_model_schema(),
        }, ("initiative_id",)),
    },
    {
        "name": "colony_list_commitments",
        "description": "List commitments in an attested private viewer scope.",
        "parameters": _parameters({
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            "status": {"type": "string", "default": "pending,overdue"},
        }),
    },
    {
        "name": "colony_list_goals",
        "description": "List goals in an attested private viewer scope.",
        "parameters": _parameters({
            "status": {
                "type": "string",
                "enum": ["active", "blocked", "completed", "all"],
                "default": "active",
            },
        }),
    },
    {
        "name": "colony_list_initiatives",
        "description": "List initiatives in an attested private viewer scope.",
        "parameters": _parameters({
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            "status": {
                "type": "string",
                "enum": [
                    "pending", "assigned", "acknowledged", "completed",
                    "failed", "cancelled",
                ],
            },
        }),
    },
    {
        "name": "colony_memory_search",
        "description": "Search private legacy memory in an attested owner/system scope.",
        "parameters": _parameters({
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
            "query": {"type": "string"},
        }, ("query",)),
    },
    {
        "name": "colony_query_entities",
        "description": "Query the private world model in an attested viewer scope.",
        "parameters": _parameters({
            "entity_type": {
                "type": "string",
                "enum": ["person", "place", "organization", "concept", "all"],
                "default": "all",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            "query": {"type": "string"},
        }, ("query",)),
    },
    {
        "name": "colony_queue_stats",
        "description": "Read task queue statistics in an attested private viewer scope.",
        "parameters": _parameters({}),
    },
    {
        "name": "colony_send_message",
        "description": (
            "Submit a governed text message to one existing owner-approved "
            "Colony contact by display name. Omit channel for the compatible "
            "WhatsApp default."
        ),
        "parameters": _parameters({
            "channel": {
                "type": "string",
                "enum": ["whatsapp", "rcs", "sms"],
            },
            "message": {"type": "string", "minLength": 1, "maxLength": 16000},
            "recipient": {"type": "string", "minLength": 1, "maxLength": 160},
        }, ("recipient", "message")),
    },
]

_TOOL_SCHEMAS: list[dict[str, Any]] = sorted(
    [
        *copy.deepcopy(_CATALOG_ACTION_MODEL_TOOL_SCHEMAS),
        *_LOCAL_TOOL_SCHEMAS,
    ],
    key=lambda item: item["name"],
)


_READ_TOOL_NAMES: tuple[str, ...] = (
    "colony_autonomy_status",
    "colony_get_initiative",
    "colony_list_commitments",
    "colony_list_goals",
    "colony_list_initiatives",
    "colony_memory_search",
    "colony_query_entities",
    "colony_queue_stats",
)

_ACTION_INTENT_TOOL_NAMES: tuple[str, ...] = tuple(
    sorted(_CATALOG_ACTION_TOOL_NAMES)
)

_OWNER_MESSAGE_TOOL_NAMES: tuple[str, ...] = ("colony_send_message",)

# No event can be injected until Colony exposes an exact viewer-attested event
# projection.  An empty catalog is an intentional security and attribution
# boundary, not a missing connection.
GOVERNED_EVENT_TYPES: tuple[str, ...] = ()


def governance_attestation() -> dict[str, Any]:
    """Describe source governance without claiming runtime or live readiness."""

    names = [item["name"] for item in _TOOL_SCHEMAS]
    events = sorted(GOVERNED_EVENT_TYPES)
    return {
        "schema": "ColonyHermesGeneralGovernanceAttestationV2",
        "version": 2,
        "source_ready": True,
        "runtime_ready": False,
        "live_ready": False,
        "runtime_attestation_schema": (
            "ColonyHermesGeneralRuntimeAttestationV1"
        ),
        "posture": {
            "direct_effect_handlers": "action_intent_only_or_absent",
            "event_state": "per_session_transport_scope",
            "session_state": "per_session_transport_scope",
            "read_viewer_authority": "transport_attested_not_model_selectable",
            "handler_context": "preserved",
            "startup_llm_mutation": "disabled",
            "memory_provider_writers": "disabled",
            "legacy_effect_workers": "inert_not_installable",
        },
        "read_tool_names": list(_READ_TOOL_NAMES),
        "action_intent_tool_names": list(_ACTION_INTENT_TOOL_NAMES),
        "owner_message_intent_tool_names": list(_OWNER_MESSAGE_TOOL_NAMES),
        "direct_effect_tool_names": [],
        "model_visible_tool_names": names,
        "event_types": events,
        "model_visible_schema_sha256": _sha256_json(list(_TOOL_SCHEMAS)),
        "event_catalog_sha256": _sha256_json(events),
        "action_intent_schema": "HermesToolActionIntentV1",
        "runtime_readiness": {
            "source_catalog_executable": False,
            "private_text_runtime_ready": False,
            "turn_outbox_ready": False,
            "turn_outbox_configuration_ready": False,
            "physical_power_loss_verified": False,
            "effect_mediator_runtime_ready": False,
            "owner_message_mediator_runtime_ready": False,
            "read_registration": (
                "full_catalog_default_or_explicit_configured_subset"
            ),
            "effect_registration": "explicit_configured_subset_only",
            "requires": [
                "allowed_action_mediator_origin",
                "resolved_action_mediator_credential",
                "safe_action_mediator_principal",
                "mediator_backed_enabled_action_tools",
                "owner_message_mediator_and_enabled_message_tool",
            ],
        },
        "turn_writer": {
            "mode": "sqlite_full_sync_configuration_outbox",
            "delivery": "cooperative_deadline_exact_idempotent_put",
            "physical_power_loss_verified": False,
            "supported_hermes_turn_finalizer": dict(SUPPORTED_HERMES_TURN_FINALIZER),
        },
    }


@dataclass(frozen=True, slots=True)
class _TransportScope:
    session_id: str
    task_id: str
    turn_id: str
    platform: str
    sender_id: str
    contact_id: str
    authority_lane: str
    resolution_status: str
    user_message: str = ""

    @property
    def valid_participant(self) -> bool:
        return bool(
            self.session_id
            and self.platform
            and self.contact_id
            and self.authority_lane in {"owner", "guest", "system"}
            and self.resolution_status in {"resolved", "attested_system"}
        )


class _TransportScopeRegistry:
    """Bounded, lock-protected transport scopes with no cross-session fallback."""

    def __init__(self, maximum: int = 2048):
        self._maximum = max(64, int(maximum))
        self._lock = threading.RLock()
        self._by_turn: OrderedDict[tuple[str, str, str], _TransportScope] = OrderedDict()
        self._current_by_session: dict[str, tuple[str, str, str]] = {}

    @staticmethod
    def _key(session_id: str, task_id: str, turn_id: str) -> tuple[str, str, str]:
        return (str(session_id or ""), str(task_id or ""), str(turn_id or ""))

    def clear(self) -> None:
        with self._lock:
            self._by_turn.clear()
            self._current_by_session.clear()

    def put(self, scope: _TransportScope) -> _TransportScope:
        key = self._key(scope.session_id, scope.task_id, scope.turn_id)
        with self._lock:
            previous = self._by_turn.get(key)
            if previous is not None and previous != scope:
                # The same host turn cannot acquire a different sender or
                # authority.  Poison that exact scope rather than selecting a
                # winner based on callback order.
                scope = _TransportScope(
                    session_id=scope.session_id,
                    task_id=scope.task_id,
                    turn_id=scope.turn_id,
                    platform=scope.platform,
                    sender_id=scope.sender_id,
                    contact_id="",
                    authority_lane="unresolved",
                    resolution_status="conflict",
                    user_message=scope.user_message,
                )
            self._by_turn[key] = scope
            self._by_turn.move_to_end(key)
            if scope.session_id:
                self._current_by_session[scope.session_id] = key
            while len(self._by_turn) > self._maximum:
                old_key, _old_scope = self._by_turn.popitem(last=False)
                session = old_key[0]
                if self._current_by_session.get(session) == old_key:
                    self._current_by_session.pop(session, None)
            return scope

    def for_execution(
        self, *, session_id: str, task_id: str, turn_id: str,
    ) -> _TransportScope | None:
        session = str(session_id or "")
        if not session:
            return None
        exact = self._key(session, task_id, turn_id)
        with self._lock:
            scope = self._by_turn.get(exact)
            if scope is None:
                current_key = self._current_by_session.get(session)
                scope = self._by_turn.get(current_key) if current_key else None
            if scope is None:
                return None
            if task_id and str(task_id) != scope.task_id:
                return None
            if turn_id and str(turn_id) != scope.turn_id:
                return None
            return scope

    def for_session(self, session_id: str) -> _TransportScope | None:
        session = str(session_id or "")
        if not session:
            return None
        with self._lock:
            key = self._current_by_session.get(session)
            return self._by_turn.get(key) if key else None


_TRANSPORT_SCOPES = _TransportScopeRegistry()


def _resolve_scope(
    client: ColonyClient,
    *,
    session_id: str,
    task_id: str,
    turn_id: str,
    platform: str,
    sender_id: str,
    user_message: str,
    owner_contact_id: str,
    attested_system_platforms: frozenset[str],
) -> _TransportScope:
    session = str(session_id or "").strip()
    task = str(task_id or "").strip()
    turn = str(turn_id or "").strip()
    transport = str(platform or "").strip().lower()
    sender = str(sender_id or "").strip()
    owner = str(owner_contact_id or "").strip()

    if transport in attested_system_platforms:
        if owner:
            return _TransportScope(
                session, task, turn, transport, sender, owner,
                "system", "attested_system", str(user_message or ""),
            )
        return _TransportScope(
            session, task, turn, transport, sender, "", "unresolved",
            "system_contact_missing", str(user_message or ""),
        )

    if not (session and transport and sender):
        return _TransportScope(
            session, task, turn, transport, sender, "", "unresolved",
            "transport_identity_missing", str(user_message or ""),
        )

    contact_id = ""
    resolution_status = "resolution_failed"
    try:
        response = client.get(
            "/v1/host/contacts/resolve",
            params={"gateway": transport, "address": sender, "create": "false"},
            timeout=4,
        )
        if int(getattr(response, "status_code", 0) or 0) == 200:
            value = response.json()
            if isinstance(value, Mapping):
                contact_id = str(value.get("contact_id") or "").strip()
                if contact_id:
                    resolution_status = "resolved"
    except BaseException:
        contact_id = ""
        resolution_status = "resolution_failed"

    lane = "owner" if contact_id and owner and contact_id == owner else "guest"
    if not contact_id:
        lane = "unresolved"
    return _TransportScope(
        session, task, turn, transport, sender, contact_id, lane,
        resolution_status, str(user_message or ""),
    )


_TOOL_EXECUTION_CONTEXT: ContextVar[dict[str, str] | None] = ContextVar(
    "colony_tool_execution_context", default=None,
)


def _tool_execution_middleware(**kwargs: Any) -> Any:
    """Carry exact Hermes execution metadata through its reduced handler API."""

    next_call = kwargs.get("next_call")
    if not callable(next_call):
        return json.dumps({
            "effect_performed": False,
            "reason": "Hermes tool execution context is unavailable",
            "status": "unavailable",
        }, sort_keys=True)
    args = kwargs.get("args")
    if not isinstance(args, dict):
        args = {}
    snapshot = {
        key: str(kwargs.get(key) or "")
        for key in (
            "api_request_id", "session_id", "task_id", "tool_call_id",
            "tool_name", "turn_id",
        )
    }
    token = _TOOL_EXECUTION_CONTEXT.set(snapshot)
    try:
        return next_call(args)
    finally:
        _TOOL_EXECUTION_CONTEXT.reset(token)


@dataclass(frozen=True, slots=True)
class HermesToolActionIntentV1:
    """Immutable JSON-backed request handed to the separate action mediator."""

    intent_id: str
    idempotency_key: str
    tool_name: str
    args_sha256: str
    context_sha256: str
    intent_digest: str
    _args_json: str
    _context_json: str

    @classmethod
    def build(
        cls, *, tool_name: str, args: Mapping[str, Any], context: Mapping[str, str],
    ) -> "HermesToolActionIntentV1":
        args_json = _canonical_json(_validate_action_tool_args(tool_name, args))
        context_json = _canonical_json(dict(context))
        args_sha = hashlib.sha256(args_json.encode("utf-8")).hexdigest()
        context_sha = hashlib.sha256(context_json.encode("utf-8")).hexdigest()
        idempotency_key = _sha256_json({
            "schema": "HermesActionCallV1",
            "tool_name": tool_name,
            "api_request_id": context.get("api_request_id", ""),
            "session_id": context.get("session_id", ""),
            "task_id": context.get("task_id", ""),
            "tool_call_id": context.get("tool_call_id", ""),
            "turn_id": context.get("turn_id", ""),
        })
        intent_id = "hti_" + idempotency_key[:32]
        unsigned = {
            "schema": "HermesToolActionIntentV1",
            "version": 1,
            "intent_id": intent_id,
            "idempotency_key": idempotency_key,
            "tool_name": tool_name,
            "args": json.loads(args_json),
            "args_sha256": args_sha,
            "context": json.loads(context_json),
            "context_sha256": context_sha,
        }
        return cls(
            intent_id=intent_id,
            idempotency_key=idempotency_key,
            tool_name=tool_name,
            args_sha256=args_sha,
            context_sha256=context_sha,
            intent_digest=_sha256_json(unsigned),
            _args_json=args_json,
            _context_json=context_json,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "HermesToolActionIntentV1",
            "version": 1,
            "intent_id": self.intent_id,
            "idempotency_key": self.idempotency_key,
            "tool_name": self.tool_name,
            "args": json.loads(self._args_json),
            "args_sha256": self.args_sha256,
            "context": json.loads(self._context_json),
            "context_sha256": self.context_sha256,
            "intent_digest": self.intent_digest,
        }


class _ActionIntentLedger:
    """Detect changed replays before they reach an external mediator."""

    def __init__(self, maximum: int = 4096):
        self._maximum = max(64, int(maximum))
        self._lock = threading.Lock()
        self._digests: OrderedDict[str, str] = OrderedDict()

    def accept(self, intent: HermesToolActionIntentV1) -> bool:
        with self._lock:
            previous = self._digests.get(intent.idempotency_key)
            if previous is not None:
                self._digests.move_to_end(intent.idempotency_key)
                return previous == intent.intent_digest
            self._digests[intent.idempotency_key] = intent.intent_digest
            while len(self._digests) > self._maximum:
                self._digests.popitem(last=False)
            return True


_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TURN_WRITER_PLATFORM_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _mediator_origin(value: str) -> str:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return ""
    try:
        parsed_port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed_port is not None and not (1 <= parsed_port <= 65535)
    ):
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _mediator_url_allowed(value: str, approved_origins: frozenset[str]) -> bool:
    origin = _mediator_origin(value)
    if not origin:
        return False
    parsed = urlsplit(value)
    hostname = str(parsed.hostname or "").lower()
    loopback = hostname == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = False
    return loopback or origin in approved_origins


def _validated_action_admission(
    value: Any, intent: HermesToolActionIntentV1,
) -> dict[str, Any]:
    """Return the sole safe projection accepted from the action mediator."""

    if not isinstance(value, Mapping) or set(value) != {
        "schema", "version", "status", "effect_performed", "intent_id",
        "action_id", "action_digest", "approval_id",
    }:
        raise RuntimeError("action mediator response is invalid")
    if value.get("schema") != "HermesToolActionAdmissionV1":
        raise RuntimeError("action mediator response schema is invalid")
    if value.get("version") != 1 or isinstance(value.get("version"), bool):
        raise RuntimeError("action mediator response version is invalid")
    if value.get("effect_performed") is not False:
        raise RuntimeError("action mediator submission effect claim is invalid")
    if value.get("intent_id") != intent.intent_id:
        raise RuntimeError("action mediator intent binding is invalid")
    status = value.get("status")
    if status != "pending":
        raise RuntimeError("action mediator status is invalid")
    action_id = str(value.get("action_id") or "")
    try:
        if str(uuid.UUID(action_id)) != action_id:
            raise ValueError("non-canonical UUID")
    except (ValueError, AttributeError):
        raise RuntimeError("action mediator canonical action id is invalid") from None
    action_digest = str(value.get("action_digest") or "")
    if not _SHA256_RE.fullmatch(action_digest):
        raise RuntimeError("action mediator canonical action digest is invalid")
    approval_id = str(value.get("approval_id") or "")
    if not _SAFE_IDENTIFIER_RE.fullmatch(approval_id):
        raise RuntimeError("action mediator approval id is invalid")
    return {
        "schema": "HermesToolActionAdmissionV1",
        "version": 1,
        "status": status,
        "effect_performed": False,
        "intent_id": intent.intent_id,
        "action_id": action_id,
        "action_digest": action_digest,
        "approval_id": approval_id,
    }


class ActionMediator:
    """Separate effect boundary; it never falls back to the Colony client."""

    def __init__(
        self,
        *,
        url: str = "",
        api_key: str = "",
        principal: str = "",
        allowed_origins: Sequence[str] = (),
    ):
        self.url = str(url or "").strip()
        self.api_key = _resolve_env_placeholder(api_key)
        self.principal = str(principal or "").strip()
        origin_values: Sequence[str]
        if isinstance(allowed_origins, str):
            origin_values = tuple(allowed_origins.split(","))
        else:
            origin_values = allowed_origins
        self.allowed_origins = frozenset(
            origin
            for origin in (_mediator_origin(str(item)) for item in origin_values)
            if origin
        )

    @property
    def safe_origin(self) -> bool:
        return _mediator_url_allowed(self.url, self.allowed_origins)

    @property
    def credential_resolved(self) -> bool:
        return bool(self.api_key and len(self.api_key) <= 4096)

    @property
    def principal_valid(self) -> bool:
        return bool(_SAFE_IDENTIFIER_RE.fullmatch(self.principal))

    @property
    def configured(self) -> bool:
        return bool(
            self.safe_origin
            and self.credential_resolved
            and self.principal_valid
        )

    def submit(self, intent: HermesToolActionIntentV1) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("action mediator is not configured")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.principal:
            headers["X-Colony-Principal"] = self.principal
        with httpx.Client(timeout=8, follow_redirects=False) as client:
            response = client.post(self.url, json=intent.to_dict(), headers=headers)
        response.raise_for_status()
        return _validated_action_admission(response.json(), intent)


@dataclass(frozen=True, slots=True)
class HermesOwnerMessageIntentV1:
    """Recipient-safe, retry-stable request for a governed message producer.

    An omitted channel retains the deployed V1 wire contract exactly.  A
    bounded explicit channel uses V2 so an older deployment fails closed
    instead of silently sending on the legacy WhatsApp default.
    """

    delivery_id: str
    source_id: str
    idempotency_key: str
    intent_digest: str
    _recipient: str
    _message: str
    _channel: str | None
    _initiator_lane: str

    @classmethod
    def build(
        cls,
        *,
        recipient: str,
        message: str,
        context: Mapping[str, str],
        channel: str | None = None,
        initiator_lane: str = "owner",
    ) -> "HermesOwnerMessageIntentV1":
        recipient_value = str(recipient)
        message_value = str(message)
        if (
            recipient_value != recipient_value.strip()
            or not recipient_value
            or len(recipient_value) > 160
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in recipient_value)
        ):
            raise ValueError("recipient is invalid")
        if (
            not message_value.strip()
            or len(message_value) > 16000
            or any(
                (ord(character) < 0x20 and character not in "\n\t")
                or ord(character) == 0x7F
                for character in message_value
            )
        ):
            raise ValueError("message is invalid")
        channel_value: str | None
        if channel is None:
            channel_value = None
        elif isinstance(channel, str) and channel in {"whatsapp", "rcs", "sms"}:
            channel_value = channel
        else:
            raise ValueError("channel is invalid")
        if initiator_lane not in {"owner", "attested_system"}:
            raise ValueError("initiator lane is invalid")
        if initiator_lane == "attested_system" and channel_value is None:
            channel_value = "whatsapp"
        system_initiated = initiator_lane == "attested_system"
        if system_initiated:
            call_identity = {
                "schema": "HermesContactMessageCallV2",
                "initiator_lane": "attested_system",
                "api_request_id": context.get("api_request_id", ""),
                "session_id": context.get("session_id", ""),
                "task_id": context.get("task_id", ""),
                "tool_call_id": context.get("tool_call_id", ""),
                "turn_id": context.get("turn_id", ""),
            }
            source_identity = {
                "schema": "HermesContactMessageSourceV2",
                "initiator_lane": "attested_system",
                "session_id": context.get("session_id", ""),
                "task_id": context.get("task_id", ""),
                "turn_id": context.get("turn_id", ""),
            }
        else:
            # Compatibility boundary: owner V1/V2 identity material predates
            # the attested-system lane.  It is deliberately kept byte-exact;
            # adding even a defaulted origin field would fork replay keys and
            # every downstream immutable digest.
            call_identity = {
                "schema": "HermesOwnerMessageCallV1",
                "api_request_id": context.get("api_request_id", ""),
                "session_id": context.get("session_id", ""),
                "task_id": context.get("task_id", ""),
                "tool_call_id": context.get("tool_call_id", ""),
                "turn_id": context.get("turn_id", ""),
            }
            source_identity = {
                "schema": "HermesOwnerMessageSourceV1",
                "session_id": context.get("session_id", ""),
                "task_id": context.get("task_id", ""),
                "turn_id": context.get("turn_id", ""),
            }
        identity_sha = _sha256_json(call_identity)
        request: dict[str, Any] = {
            "schema": (
                "HermesContactMessageIntentV3"
                if system_initiated
                else "HermesOwnerMessageIntentV1"
                if channel_value is None
                else "HermesOwnerMessageIntentV2"
            ),
            "version": 3 if system_initiated else 1 if channel_value is None else 2,
            "recipient": recipient_value,
            "message": message_value,
            "delivery_id": (
                "hermes-contact:" if system_initiated else "hermes-owner:"
            ) + identity_sha,
            "source_id": (
                "hermes-system-turn:" if system_initiated else "hermes-turn:"
            ) + _sha256_json(source_identity),
        }
        if channel_value is not None:
            request["channel"] = channel_value
        if system_initiated:
            request["initiator_lane"] = "attested_system"
        return cls(
            delivery_id=request["delivery_id"],
            source_id=request["source_id"],
            idempotency_key=identity_sha,
            intent_digest=_sha256_json(request),
            _recipient=recipient_value,
            _message=message_value,
            _channel=channel_value,
            _initiator_lane=initiator_lane,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": (
                "HermesContactMessageIntentV3"
                if self._initiator_lane == "attested_system"
                else "HermesOwnerMessageIntentV1"
                if self._channel is None
                else "HermesOwnerMessageIntentV2"
            ),
            "version": (
                3
                if self._initiator_lane == "attested_system"
                else 1
                if self._channel is None
                else 2
            ),
            "recipient": self._recipient,
            "message": self._message,
            "delivery_id": self.delivery_id,
            "source_id": self.source_id,
        }
        if self._channel is not None:
            result["channel"] = self._channel
        if self._initiator_lane == "attested_system":
            result["initiator_lane"] = "attested_system"
        return result


def _validated_owner_message_admission(
    value: Any, intent: HermesOwnerMessageIntentV1,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema", "version", "delivery_id", "state", "intent_id",
        "provider_delivered",
    }:
        raise RuntimeError("owner message mediator response is invalid")
    state = value.get("state")
    if (
        value.get("schema") != "GovernedOutboundAdmissionV1"
        or type(value.get("version")) is not int
        or value.get("version") != 1
        or value.get("delivery_id") != intent.delivery_id
        or state not in {
            "accepted", "held", "delivered", "failed", "ambiguous",
        }
        or type(value.get("provider_delivered")) is not bool
        or value.get("provider_delivered") is not (state == "delivered")
    ):
        raise RuntimeError("owner message mediator response is invalid")
    intent_id = str(value.get("intent_id") or "")
    if (
        state == "held" and intent_id
        or state != "held"
        and not re.fullmatch(r"colony-intent:[a-f0-9]{64}", intent_id)
    ):
        raise RuntimeError("owner message mediator intent binding is invalid")
    return {
        "schema": "ColonyOwnerMessageAdmissionV1",
        "version": 1,
        "status": state,
        "effect_performed": False,
        "delivery_id": intent.delivery_id,
        "intent_id": intent_id,
        "provider_delivered": value["provider_delivered"],
    }


class OwnerMessageMediator:
    """Dedicated admission boundary; it cannot call a provider directly."""

    def __init__(
        self,
        *,
        url: str = "",
        api_key: str = "",
        principal: str = "",
        allowed_origins: Sequence[str] = (),
    ):
        self.url = str(url or "").strip()
        self.api_key = _resolve_env_placeholder(api_key)
        self.principal = str(principal or "").strip()
        origin_values = (
            tuple(allowed_origins.split(","))
            if isinstance(allowed_origins, str) else allowed_origins
        )
        self.allowed_origins = frozenset(
            origin
            for origin in (_mediator_origin(str(item)) for item in origin_values)
            if origin
        )

    @property
    def safe_origin(self) -> bool:
        if not _mediator_url_allowed(self.url, self.allowed_origins):
            return False
        try:
            parsed = urlsplit(self.url)
        except ValueError:
            return False
        return parsed.path == "/internal/owner-deliver"

    @property
    def credential_resolved(self) -> bool:
        return bool(self.api_key and 32 <= len(self.api_key) <= 512)

    @property
    def principal_valid(self) -> bool:
        return bool(_SAFE_IDENTIFIER_RE.fullmatch(self.principal))

    @property
    def configured(self) -> bool:
        return bool(
            self.safe_origin and self.credential_resolved and self.principal_valid
        )

    def submit(self, intent: HermesOwnerMessageIntentV1) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("owner message mediator is not configured")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=8, follow_redirects=False) as client:
            response = client.post(self.url, json=intent.to_dict(), headers=headers)
        response.raise_for_status()
        return _validated_owner_message_admission(response.json(), intent)


_FORBIDDEN_MODEL_KEYS = frozenset({
    "api_key", "authority", "authorization", "contact_id", "credential",
    "credentials", "person_id", "principal", "sender_id", "session_id",
    "token", "transport_authority", "viewer", "viewer_authority",
})


def _model_override_path(value: Any, path: str = "") -> str:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            here = f"{path}.{key}" if path else key
            if key in _FORBIDDEN_MODEL_KEYS or key.endswith("_credential"):
                return here
            found = _model_override_path(item, here)
            if found:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = _model_override_path(item, f"{path}[{index}]")
            if found:
                return found
    return ""


_AUTONOMY_STATUS_TEXT_LIMIT = 128
_AUTONOMY_STATUS_COUNTER_LIMIT = (1 << 63) - 1
_AUTONOMY_STATUS_TEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:/-]{0,127}$")


def _bounded_autonomy_status(value: Any) -> dict[str, Any]:
    """Project the sidecar status onto one exact non-secret read schema."""

    if not isinstance(value, Mapping):
        raise RuntimeError("autonomy status response is invalid")
    booleans: dict[str, bool] = {}
    for key in ("running", "in_quiet_hours"):
        item = value.get(key)
        if not isinstance(item, bool):
            raise RuntimeError("autonomy status boolean is invalid")
        booleans[key] = item
    mode_value = value.get("mode")
    mode = mode_value.strip() if isinstance(mode_value, str) else ""
    if mode not in {"reactive", "proactive"}:
        raise RuntimeError("autonomy status mode is invalid")
    timezone_value = value.get("timezone")
    timezone_name = (
        timezone_value.strip() if isinstance(timezone_value, str) else ""
    )
    if (
        not timezone_name
        or len(timezone_name) > _AUTONOMY_STATUS_TEXT_LIMIT
        or not _AUTONOMY_STATUS_TEXT_RE.fullmatch(timezone_name)
    ):
        raise RuntimeError("autonomy status timezone is invalid")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise RuntimeError("autonomy status timezone is invalid") from None
    counters: dict[str, int] = {}
    for key in (
        "ticks", "events_processed", "goals_checked",
        "initiatives_generated", "actions_executed", "errors",
    ):
        item = value.get(key)
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or not 0 <= item <= _AUTONOMY_STATUS_COUNTER_LIMIT
        ):
            raise RuntimeError("autonomy status counter is invalid")
        counters[key] = item
    return {
        "schema": "ColonyAutonomyStatusProjectionV1",
        "version": 1,
        "running": booleans["running"],
        "mode": mode,
        "timezone": timezone_name,
        "in_quiet_hours": booleans["in_quiet_hours"],
        **counters,
    }


_QUEUE_STATS_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _queue_stats_counts(value: Any, *, prefix: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise RuntimeError("queue stats counts are invalid")
    counts: dict[str, int] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not _QUEUE_STATS_KEY_RE.fullmatch(key):
            raise RuntimeError("queue stats count key is invalid")
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or not 0 <= item <= _AUTONOMY_STATUS_COUNTER_LIMIT
        ):
            raise RuntimeError("queue stats counter is invalid")
        counts[prefix + key] = item
    return dict(sorted(counts.items()))


def _bounded_queue_stats(value: Any) -> dict[str, Any]:
    """Project the sidecar queue statistics onto one exact count-only schema.

    Free text from the sidecar (hold reasons, delivery errors) stays on the
    host. Status and type counters are keyed ``status_<name>`` and
    ``type_<name>``: a raw ``by_status`` map carries a literal ``"failed"``
    key, which the host's tool-result failure heuristic reads as a failed
    call and logs every healthy read as an error.
    """

    if not isinstance(value, Mapping):
        raise RuntimeError("queue stats response is invalid")
    workers: dict[str, int] = {}
    for key in (
        "total_workers", "available_workers", "registered_workers",
        "active_workers", "stale_workers", "worker_heartbeat_ttl_secs",
    ):
        item = value.get(key)
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or not 0 <= item <= _AUTONOMY_STATUS_COUNTER_LIMIT
        ):
            raise RuntimeError("queue stats worker counter is invalid")
        workers[key] = item
    governance = value.get("governance")
    if not isinstance(governance, Mapping):
        raise RuntimeError("queue stats governance is invalid")
    held_total = governance.get("held_total", 0)
    if (
        isinstance(held_total, bool)
        or not isinstance(held_total, int)
        or not 0 <= held_total <= _AUTONOMY_STATUS_COUNTER_LIMIT
    ):
        raise RuntimeError("queue stats held total is invalid")
    scheduler = value.get("scheduler")
    if not isinstance(scheduler, Mapping):
        raise RuntimeError("queue stats scheduler is invalid")
    scheduler_flags: dict[str, bool] = {}
    for key in ("running", "healthy"):
        item = scheduler.get(key)
        if not isinstance(item, bool):
            raise RuntimeError("queue stats scheduler flag is invalid")
        scheduler_flags[key] = item
    return {
        "schema": "ColonyQueueStatsProjectionV1",
        "version": 1,
        "tasks_by_status": _queue_stats_counts(
            value.get("by_status"), prefix="status_"),
        "tasks_by_type": _queue_stats_counts(
            value.get("by_type"), prefix="type_"),
        "workers": workers,
        "held_total": held_total,
        "holds": _queue_stats_counts(
            governance.get("holds", {}), prefix="hold_"),
        "scheduler": scheduler_flags,
    }


class _ToolDispatcher:
    def __init__(
        self,
        *,
        client: ColonyClient,
        mediator: ActionMediator,
        owner_message_mediator: OwnerMessageMediator | None = None,
        owner_contact_id: str,
        attested_system_platforms: Sequence[str],
        enabled_action_tools: Sequence[str] = (),
        enabled_message_tools: Sequence[str] = (),
        enabled_read_tools: Sequence[str] = _READ_TOOL_NAMES,
        scopes: _TransportScopeRegistry | None = None,
    ):
        self._client = client
        self._mediator = mediator
        self._owner_message_mediator = owner_message_mediator or OwnerMessageMediator()
        self._owner_contact_id = str(owner_contact_id or "").strip()
        self._attested_system_platforms = frozenset(
            str(item).strip().lower()
            for item in attested_system_platforms
            if str(item).strip()
        )
        self._enabled_action_tools = frozenset(
            str(item).strip() for item in enabled_action_tools if str(item).strip()
        )
        self._enabled_message_tools = frozenset(
            str(item).strip() for item in enabled_message_tools if str(item).strip()
        )
        self._enabled_read_tools = frozenset(
            str(item).strip() for item in enabled_read_tools if str(item).strip()
        )
        self._scopes = scopes or _TRANSPORT_SCOPES
        self._intent_ledger = _ActionIntentLedger()

    def dispatch(self, name: str, args: Mapping[str, Any], **handler_kwargs: Any) -> str:
        try:
            return self._dispatch(name, args, **handler_kwargs)
        except BaseException as error:
            logger.warning("governed Colony tool %s failed closed: %s", name, error)
            return _canonical_json({
                "effect_performed": False,
                "reason": "governed tool execution failed",
                "status": "unavailable",
            })

    def _dispatch(self, name: str, args: Mapping[str, Any], **handler_kwargs: Any) -> str:
        if name not in (
            set(_READ_TOOL_NAMES)
            | set(_ACTION_INTENT_TOOL_NAMES)
            | set(_OWNER_MESSAGE_TOOL_NAMES)
        ):
            return _canonical_json({"reason": "unknown Colony tool", "status": "denied"})
        if not isinstance(args, Mapping):
            return _canonical_json({"reason": "tool arguments must be an object", "status": "denied"})
        clean_args = dict(args)
        forbidden = _model_override_path(clean_args)
        if forbidden:
            return _canonical_json({
                "reason": f"model-selected authority or credential field is forbidden: {forbidden}",
                "status": "denied",
            })

        preserved = dict(_TOOL_EXECUTION_CONTEXT.get() or {})
        # Hermes 0.18.2 deliberately reduces registry-handler kwargs.  Preserve
        # those kwargs, but never allow them to replace richer middleware data.
        for key in ("session_id", "task_id", "turn_id", "tool_call_id", "api_request_id"):
            if not preserved.get(key) and handler_kwargs.get(key) is not None:
                preserved[key] = str(handler_kwargs.get(key) or "")
        if preserved.get("tool_name") and preserved["tool_name"] != name:
            return _canonical_json({"reason": "tool execution binding mismatch", "status": "denied"})
        scope = self._scopes.for_execution(
            session_id=preserved.get("session_id", ""),
            task_id=preserved.get("task_id", ""),
            turn_id=preserved.get("turn_id", ""),
        )
        if scope is None:
            return _canonical_json({
                "reason": "exact transport scope is unavailable",
                "status": "denied",
            })

        if name in _READ_TOOL_NAMES:
            if name not in self._enabled_read_tools:
                return _canonical_json({
                    "reason": (
                        "read tool is not enabled by runtime capability "
                        "configuration"
                    ),
                    "status": "unavailable",
                })
            if scope.authority_lane not in {"owner", "system"}:
                reason = (
                    "contact resolution failed"
                    if scope.authority_lane == "unresolved"
                    else "private legacy reads require owner or system authority"
                )
                return _canonical_json({"reason": reason, "status": "denied"})
            return self._read(name, clean_args, scope)
        if name in _OWNER_MESSAGE_TOOL_NAMES:
            if name not in self._enabled_message_tools:
                return _canonical_json({
                    "effect_performed": False,
                    "reason": "owner message tool is not enabled by runtime capability configuration",
                    "status": "unavailable",
                })
            return self._owner_message(clean_args, preserved, scope)
        if name not in self._enabled_action_tools:
            return _canonical_json({
                "effect_performed": False,
                "reason": "action tool is not enabled by runtime capability configuration",
                "status": "unavailable",
            })
        return self._action(name, clean_args, preserved, scope)

    def _owner_message(
        self,
        args: dict[str, Any],
        preserved: Mapping[str, str],
        scope: _TransportScope,
    ) -> str:
        authenticated_initiator = bool(
            scope.valid_participant
            and (
                (
                    scope.authority_lane == "owner"
                    and scope.resolution_status == "resolved"
                )
                or (
                    scope.authority_lane == "system"
                    and scope.resolution_status == "attested_system"
                )
            )
        )
        if not authenticated_initiator or _voice_surface(scope.platform):
            return _canonical_json({
                "effect_performed": False,
                "reason": (
                    "contact messages require an authenticated owner text turn "
                    "or an attested local system turn"
                ),
                "status": "denied",
            })
        if not self._owner_message_mediator.configured:
            return _canonical_json({
                "effect_performed": False,
                "reason": "owner message mediator is not configured",
                "status": "unavailable",
            })
        if not preserved.get("session_id") or not preserved.get("tool_call_id"):
            return _canonical_json({
                "effect_performed": False,
                "reason": "stable Hermes message identity is unavailable",
                "status": "denied",
            })
        if not (preserved.get("turn_id") or preserved.get("task_id")):
            return _canonical_json({
                "effect_performed": False,
                "reason": "stable Hermes turn identity is unavailable",
                "status": "denied",
            })
        if set(args) not in (
            {"recipient", "message"},
            {"recipient", "message", "channel"},
        ):
            return _canonical_json({
                "effect_performed": False,
                "reason": "owner message arguments are invalid",
                "status": "denied",
            })
        context = {
            "api_request_id": preserved.get("api_request_id", ""),
            "session_id": preserved.get("session_id", ""),
            "task_id": preserved.get("task_id", ""),
            "tool_call_id": preserved.get("tool_call_id", ""),
            "turn_id": preserved.get("turn_id", ""),
        }
        try:
            intent = HermesOwnerMessageIntentV1.build(
                recipient=args["recipient"],
                message=args["message"],
                context=context,
                channel=args.get("channel"),
                initiator_lane=(
                    "attested_system"
                    if scope.authority_lane == "system"
                    else "owner"
                ),
            )
        except (TypeError, ValueError):
            return _canonical_json({
                "effect_performed": False,
                "reason": "owner message arguments are invalid",
                "status": "denied",
            })
        if not self._intent_ledger.accept(intent):
            return _canonical_json({
                "effect_performed": False,
                "reason": "message idempotency conflict",
                "status": "conflict",
            })
        try:
            result = self._owner_message_mediator.submit(intent)
        except BaseException:
            return _canonical_json({
                "effect_performed": False,
                "delivery_id": intent.delivery_id,
                "reason": "owner message mediator is unavailable",
                "status": "unavailable",
            })
        return _canonical_json(result)

    def _action(
        self,
        name: str,
        args: dict[str, Any],
        preserved: Mapping[str, str],
        scope: _TransportScope,
    ) -> str:
        if not scope.valid_participant:
            return _canonical_json({
                "effect_performed": False,
                "reason": "exact participant resolution is unavailable",
                "status": "denied",
            })
        if not self._mediator.configured:
            return _canonical_json({
                "effect_performed": False,
                "reason": "action mediator is not configured",
                "status": "unavailable",
            })
        if not preserved.get("session_id") or not preserved.get("tool_call_id"):
            return _canonical_json({
                "effect_performed": False,
                "reason": "stable Hermes action identity is unavailable",
                "status": "denied",
            })
        if not (preserved.get("turn_id") or preserved.get("task_id")):
            return _canonical_json({
                "effect_performed": False,
                "reason": "stable Hermes turn identity is unavailable",
                "status": "denied",
            })
        context = {
            "api_request_id": preserved.get("api_request_id", ""),
            "authority_lane": scope.authority_lane,
            "contact_id": scope.contact_id,
            "platform": scope.platform,
            "sender_id": scope.sender_id,
            "session_id": preserved.get("session_id", ""),
            "task_id": preserved.get("task_id", ""),
            "tool_call_id": preserved.get("tool_call_id", ""),
            "turn_id": preserved.get("turn_id", ""),
        }
        try:
            intent = HermesToolActionIntentV1.build(
                tool_name=name, args=args, context=context,
            )
        except (TypeError, ValueError) as error:
            return _canonical_json({
                "effect_performed": False,
                "reason": f"tool arguments are invalid: {error}",
                "status": "denied",
            })
        if not self._intent_ledger.accept(intent):
            return _canonical_json({
                "effect_performed": False,
                "reason": "action idempotency conflict",
                "status": "conflict",
            })
        try:
            result = _validated_action_admission(
                self._mediator.submit(intent), intent,
            )
        except BaseException:
            return _canonical_json({
                "effect_performed": False,
                "intent_digest": intent.intent_digest,
                "intent_id": intent.intent_id,
                "reason": "action mediator is unavailable",
                "status": "unavailable",
            })
        return _canonical_json(result)

    def _read(self, name: str, args: dict[str, Any], scope: _TransportScope) -> str:
        try:
            if name == "colony_autonomy_status":
                response = self._client.get(
                    "/v1/host/autonomy/status", timeout=5,
                )
            elif name == "colony_get_initiative":
                initiative_id = str(args.get("initiative_id", ""))
                response = self._client.get(
                    f"/v1/host/initiatives/{initiative_id}", timeout=5,
                )
            elif name == "colony_list_commitments":
                params: dict[str, Any] = {"limit": int(args.get("limit", 20) or 20)}
                status = str(args.get("status", "pending,overdue") or "").strip()
                if status and status != "all":
                    params["status"] = status
                response = self._client.get("/v1/host/commitments", params=params, timeout=5)
            elif name == "colony_list_goals":
                response = self._client.get(
                    "/v1/host/goals",
                    params={"status_filter": args.get("status", "active")},
                    timeout=5,
                )
            elif name == "colony_list_initiatives":
                params = {"limit": int(args.get("limit", 50) or 50)}
                if args.get("status"):
                    params["status"] = args["status"]
                response = self._client.get("/v1/host/initiatives", params=params, timeout=5)
            elif name == "colony_memory_search":
                response = self._client.post(
                    "/v1/host/memory/search",
                    json={
                        "identity": {"host_id": "hermes"},
                        "context": {
                            "session_id": scope.session_id,
                            "contact_id": scope.contact_id,
                        },
                        "query": str(args.get("query", "")),
                        "limit": int(args.get("limit", 5) or 5),
                    },
                    timeout=5,
                )
            elif name == "colony_query_entities":
                response = self._client.post(
                    "/v1/host/world/entities/query",
                    json={
                        "identity": {"host_id": "hermes"},
                        "query": str(args.get("query", "")),
                        "entity_type": str(args.get("entity_type") or "all"),
                        "limit": int(args.get("limit", 10) or 10),
                    },
                    timeout=5,
                )
            elif name == "colony_queue_stats":
                response = self._client.get("/v1/host/queue/stats", timeout=5)
            else:  # pragma: no cover - catalog/dispatcher classification prevents this
                raise RuntimeError("read handler is unavailable")
            response.raise_for_status()
            value = response.json()
            if name == "colony_autonomy_status":
                value = _bounded_autonomy_status(value)
            elif name == "colony_queue_stats":
                value = _bounded_queue_stats(value)
            return _canonical_json(value)
        except BaseException:
            return _canonical_json({
                "reason": "private read is unavailable",
                "status": "unavailable",
            })


_GUARD_CHAT_MODES = frozenset({"off", "shadow", "enforce"})
_GUARD_WITHHELD_TEXT = "[message withheld: the outbound reply tripped the response guard]"
_GUARD_EXACT_RESPONSE_LIMIT = 8000
_GUARD_POLICY_ID = "response-guard-surface-policy-v1"
_GUARD_POLICY_DIGEST = "712a2b620aa135b372e738ca56e83549b830132e3574b256890edd5cab281c8f"


def _guard_chat_mode() -> str:
    raw = os.environ.get("COLONY_GUARD_CHAT_MODE", "").strip().lower()
    if raw in _GUARD_CHAT_MODES:
        return raw
    legacy = os.environ.get("COLONY_GUARD_CHAT_SHADOW", "0").strip().lower()
    return "shadow" if legacy in {"1", "true", "yes", "on"} else "off"


def _voice_surface(platform: str) -> bool:
    value = str(platform or "").strip().lower().replace("-", "_")
    return any(token in value for token in (
        "audio", "call", "google_meet", "intercom", "meet", "phone",
        "realtime_voice", "sip", "voice",
    ))


def _guard_verdict_valid(value: Any, *, candidate: str, mode: str) -> bool:
    if not isinstance(value, Mapping):
        return False
    decision = value.get("decision")
    status = value.get("guard_status")
    if decision not in {"allow", "block", "revise"}:
        return False
    if decision == "allow" and status != "evaluated":
        return False
    return bool(
        value.get("mode") == mode
        and value.get("surface") == "text_chat"
        and value.get("surface_family") == "text"
        and value.get("applicability") == "guarded"
        and status in {"evaluated", "degraded"}
        and value.get("policy_id") == _GUARD_POLICY_ID
        and value.get("policy_digest") == _GUARD_POLICY_DIGEST
        and value.get("candidate_digest")
        == hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    )


def _resolve_env_placeholder(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("${") and text.endswith("}") and len(text) > 3:
        return os.environ.get(text[2:-1], "")
    return text


def _plugin_config(ctx: Any) -> dict[str, Any]:
    raw = getattr(ctx, "config", None)
    if isinstance(raw, Mapping):
        plugins = raw.get("plugins")
        if isinstance(plugins, Mapping) and isinstance(plugins.get("colony"), Mapping):
            return dict(plugins["colony"])

    # Pinned Hermes does not expose config on PluginContext.  Reading its
    # already-existing config is the only fallback; it performs no network or
    # mutation and is skipped by tests/future contexts that supply config.
    try:
        from hermes_cli.config import cfg_get, load_config

        loaded = load_config()
        config = dict(cfg_get(loaded, "plugins", "colony", default={}) or {})
        memory = cfg_get(loaded, "memory", "config", default={}) or {}
        if isinstance(memory, Mapping):
            for key, value in memory.items():
                config.setdefault(key, value)
        return config
    except BaseException:
        return {}


@dataclass(frozen=True, slots=True)
class _RuntimeBoundary:
    """Validated local resources used by one registered plugin instance."""

    mediator: ActionMediator
    owner_message_mediator: OwnerMessageMediator
    enabled_read_tools: frozenset[str]
    enabled_read_tools_source: str
    enabled_action_tools: frozenset[str]
    enabled_message_tools: frozenset[str]
    turn_writer_platforms: frozenset[str] | None
    turn_writer_platforms_source: str
    turn_outbox: TurnOutbox
    attestation: dict[str, Any]


def _configured_sequence(
    config: Mapping[str, Any], key: str, *, comma_separated: bool,
) -> tuple[str, ...]:
    value = config.get(key, ())
    if isinstance(value, str) and comma_separated:
        value = tuple(value.split(","))
    if not isinstance(value, (list, tuple, set, frozenset)):
        suffix = " or comma-separated string" if comma_separated else ""
        raise RuntimeError(f"{key} must be a list{suffix}")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise RuntimeError(f"{key} entries must be strings")
        if item.strip():
            normalized.append(item.strip())
    return tuple(normalized)


def _configured_read_tools(
    config: Mapping[str, Any],
) -> tuple[frozenset[str], str]:
    """Return the exact model-visible read subset and how it was selected.

    Omitting ``enabled_read_tools`` is the compatibility path and retains the
    complete historical read catalog.  An explicit value is deliberately
    stricter than the older action/message parsers: blank entries, duplicate
    normalized names, unordered sets, and unknown tools all fail before any
    Hermes capability is registered.
    """

    if "enabled_read_tools" not in config:
        return frozenset(_READ_TOOL_NAMES), "default_full_catalog"

    value = config["enabled_read_tools"]
    if isinstance(value, str):
        entries: Sequence[Any] = value.split(",")
    elif isinstance(value, (list, tuple)):
        entries = value
    else:
        raise RuntimeError(
            "enabled_read_tools must be a list or comma-separated string"
        )

    normalized: list[str] = []
    for item in entries:
        if not isinstance(item, str):
            raise RuntimeError("enabled_read_tools entries must be strings")
        name = item.strip()
        if not name:
            raise RuntimeError("enabled_read_tools entries must not be blank")
        normalized.append(name)

    if len(normalized) != len(set(normalized)):
        raise RuntimeError("enabled_read_tools contains duplicate tools")
    unknown = set(normalized).difference(_READ_TOOL_NAMES)
    if unknown:
        raise RuntimeError(
            "enabled_read_tools contains unknown tools: "
            + ", ".join(sorted(unknown))
        )
    return frozenset(normalized), "explicit_subset"


def _configured_turn_writer_platforms(
    config: Mapping[str, Any],
) -> tuple[frozenset[str] | None, str]:
    """Return the exact producer allowlist or the compatibility default.

    An omitted key preserves the general-purpose plugin's historical behavior:
    every transport-resolved participant may be observed. Deployments that
    configure this key get a strict producer boundary; unsupported platforms
    are ignored before the durable outbox is touched.
    """

    if "turn_writer_platforms" not in config:
        return None, "compatibility_all_resolved"

    value = config["turn_writer_platforms"]
    if isinstance(value, str):
        entries: Sequence[Any] = value.split(",")
    elif isinstance(value, (list, tuple)):
        entries = value
    else:
        raise RuntimeError(
            "turn_writer_platforms must be a list or comma-separated string"
        )

    normalized: list[str] = []
    for item in entries:
        if not isinstance(item, str):
            raise RuntimeError("turn_writer_platforms entries must be strings")
        platform = item.strip()
        if not platform:
            raise RuntimeError(
                "turn_writer_platforms entries must not be blank"
            )
        if platform != platform.lower() or not _TURN_WRITER_PLATFORM_RE.fullmatch(
            platform
        ):
            raise RuntimeError(
                "turn_writer_platforms entries must be canonical lowercase "
                "platform identifiers"
            )
        normalized.append(platform)

    if len(normalized) != len(set(normalized)):
        raise RuntimeError("turn_writer_platforms contains duplicate platforms")
    return frozenset(normalized), "explicit_allowlist"


def _configured_text(config: Mapping[str, Any], key: str) -> str:
    value = config.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RuntimeError(f"{key} must be a string")
    return value


def _turn_outbox_path(config: Mapping[str, Any]) -> str:
    configured = config.get("turn_outbox_path")
    if configured is not None and not isinstance(configured, (str, os.PathLike)):
        raise RuntimeError("turn_outbox_path must be a filesystem path")
    return str(
        configured
        or os.environ.get("COLONY_HERMES_TURN_OUTBOX")
        or os.path.join(
            os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"),
            "state", "colony-turn-outbox.sqlite3",
        )
    )


def _mediator_runtime_posture(mediator: ActionMediator) -> dict[str, bool]:
    """Return only non-secret booleans about the exact mediator boundary."""

    configured = bool(getattr(mediator, "configured", False))
    # The fallbacks preserve compatibility with a deliberately injected test
    # mediator. The shipped ActionMediator exposes all three exact properties.
    safe_origin = bool(getattr(mediator, "safe_origin", configured))
    credential_resolved = bool(getattr(
        mediator,
        "credential_resolved",
        bool(
            getattr(mediator, "api_key", "")
            and len(str(getattr(mediator, "api_key", ""))) <= 4096
        ),
    ))
    principal = str(getattr(mediator, "principal", "") or "")
    principal_valid = bool(getattr(
        mediator,
        "principal_valid",
        _SAFE_IDENTIFIER_RE.fullmatch(principal),
    ))
    ready = bool(
        configured and safe_origin and credential_resolved and principal_valid
    )
    return {
        "ready": ready,
        "safe_origin": safe_origin,
        "credential_resolved": credential_resolved,
        "principal_valid": principal_valid,
    }


def _prepare_runtime_boundary(config: Mapping[str, Any]) -> _RuntimeBoundary:
    """Initialize and attest every local boundary before any registration."""

    if not isinstance(config, Mapping):
        raise RuntimeError("Colony plugin runtime configuration must be an object")

    requested_reads, requested_reads_source = _configured_read_tools(config)
    turn_writer_platforms, turn_writer_platforms_source = (
        _configured_turn_writer_platforms(config)
    )

    requested_actions = frozenset(_configured_sequence(
        config, "enabled_action_tools", comma_separated=True,
    ))
    unknown_actions = requested_actions.difference(_ACTION_INTENT_TOOL_NAMES)
    if unknown_actions:
        raise RuntimeError(
            "enabled_action_tools contains unknown tools: "
            + ", ".join(sorted(unknown_actions))
        )

    requested_messages = frozenset(_configured_sequence(
        config, "enabled_message_tools", comma_separated=True,
    ))
    unknown_messages = requested_messages.difference(_OWNER_MESSAGE_TOOL_NAMES)
    if unknown_messages:
        raise RuntimeError(
            "enabled_message_tools contains unknown tools: "
            + ", ".join(sorted(unknown_messages))
        )

    configured_origins = _configured_sequence(
        config, "action_mediator_allowed_origins", comma_separated=True,
    )
    mediator = ActionMediator(
        url=_configured_text(config, "action_mediator_url"),
        api_key=_configured_text(config, "action_mediator_api_key"),
        principal=_configured_text(config, "action_mediator_principal"),
        allowed_origins=configured_origins,
    )
    mediator_posture = _mediator_runtime_posture(mediator)
    message_origins = _configured_sequence(
        config, "owner_message_mediator_allowed_origins", comma_separated=True,
    )
    owner_message_mediator = OwnerMessageMediator(
        url=_configured_text(config, "owner_message_mediator_url"),
        api_key=_configured_text(config, "owner_message_mediator_api_key"),
        principal=_configured_text(config, "owner_message_mediator_principal"),
        allowed_origins=message_origins,
    )
    message_mediator_posture = _mediator_runtime_posture(owner_message_mediator)

    turn_outbox = TurnOutbox(_turn_outbox_path(config))
    outbox_attestation = turn_outbox.prepare()
    turn_outbox_ready = bool(
        outbox_attestation.get("schema")
        == "PrivateSQLiteDurabilityConfigurationAttestationV2"
        and outbox_attestation.get("version") == 2
        and outbox_attestation.get("configuration_ready") is True
        and outbox_attestation.get("physical_power_loss_verified") is False
        and outbox_attestation.get("readiness_scope")
        == "sqlite_and_filesystem_configuration"
        and outbox_attestation.get("private_parent") is True
        and outbox_attestation.get("regular_file") is True
        and outbox_attestation.get("current_euid_owner") is True
        and outbox_attestation.get("single_link") is True
        and outbox_attestation.get("mode") == "0600"
        and outbox_attestation.get("journal_mode") == "delete"
        and outbox_attestation.get("synchronous") == "FULL"
        and outbox_attestation.get("fullfsync") == "ON"
        and outbox_attestation.get("checkpoint_fullfsync") == "ON"
        and outbox_attestation.get("application_id") == TurnOutbox._APPLICATION_ID
        and outbox_attestation.get("user_version") == TurnOutbox._USER_VERSION
    )
    if not turn_outbox_ready:  # pragma: no cover - TurnOutbox.prepare is exact
        raise PrivateSQLitePathError(
            "private SQLite runtime attestation is incomplete"
        )

    runtime_enabled_actions = (
        requested_actions if mediator_posture["ready"] else frozenset()
    )
    runtime_enabled_messages = (
        requested_messages if message_mediator_posture["ready"] else frozenset()
    )
    effect_mediator_runtime_ready = bool(
        mediator_posture["ready"] and runtime_enabled_actions
    )
    message_mediator_runtime_ready = bool(
        message_mediator_posture["ready"] and runtime_enabled_messages
    )
    # A prepared owner-private FULL outbox is the local write boundary required
    # for safe private-text registration. Network delivery is intentionally not
    # part of this non-I/O health claim.
    private_text_runtime_ready = turn_outbox_ready
    runtime_ready = bool(
        private_text_runtime_ready
        and turn_outbox_ready
        and (effect_mediator_runtime_ready or message_mediator_runtime_ready)
    )
    reason: str | None
    if effect_mediator_runtime_ready or message_mediator_runtime_ready:
        reason = None
    elif requested_messages and not message_mediator_posture["ready"]:
        reason = "owner_message_mediator_not_ready"
    elif not mediator_posture["ready"]:
        reason = "action_mediator_not_ready"
    elif not runtime_enabled_actions:
        reason = "enabled_action_subset_empty"
    else:
        reason = None
    sorted_actions = sorted(runtime_enabled_actions)
    sorted_messages = sorted(runtime_enabled_messages)
    sorted_reads = sorted(requested_reads)
    sorted_turn_writer_platforms = (
        sorted(turn_writer_platforms)
        if turn_writer_platforms is not None else None
    )
    attestation = {
        "schema": "ColonyHermesGeneralRuntimeAttestationV1",
        "version": 1,
        "source_schema": "ColonyHermesGeneralGovernanceAttestationV2",
        "source_ready": True,
        "private_text_runtime_ready": private_text_runtime_ready,
        "turn_outbox_ready": turn_outbox_ready,
        "turn_outbox_configuration_ready": turn_outbox_ready,
        "physical_power_loss_verified": False,
        "effect_mediator_runtime_ready": effect_mediator_runtime_ready,
        "owner_message_mediator_runtime_ready": message_mediator_runtime_ready,
        "runtime_ready": runtime_ready,
        # This probe performs no network request or canary. ``runtime_ready``
        # means the local boundaries are usable; operational live readiness
        # must be established by a separate deployment-owned canary.
        "live_ready": False,
        "reason": reason,
        "action_mediator": mediator_posture,
        "owner_message_mediator": message_mediator_posture,
        "enabled_read_tools": sorted_reads,
        "enabled_read_tools_sha256": _sha256_json(sorted_reads),
        "enabled_read_tools_source": requested_reads_source,
        "enabled_action_tools": sorted_actions,
        "enabled_action_tools_sha256": _sha256_json(sorted_actions),
        "enabled_message_tools": sorted_messages,
        "enabled_message_tools_sha256": _sha256_json(sorted_messages),
        "turn_writer_platforms": sorted_turn_writer_platforms,
        "turn_writer_platforms_sha256": _sha256_json(
            sorted_turn_writer_platforms
        ),
        "turn_writer_platforms_source": turn_writer_platforms_source,
        "turn_outbox": outbox_attestation,
    }
    return _RuntimeBoundary(
        mediator=mediator,
        owner_message_mediator=owner_message_mediator,
        enabled_read_tools=requested_reads,
        enabled_read_tools_source=requested_reads_source,
        enabled_action_tools=runtime_enabled_actions,
        enabled_message_tools=runtime_enabled_messages,
        turn_writer_platforms=turn_writer_platforms,
        turn_writer_platforms_source=turn_writer_platforms_source,
        turn_outbox=turn_outbox,
        attestation=attestation,
    )


def runtime_governance_attestation(config: Mapping[str, Any]) -> dict[str, Any]:
    """Attest initialized runtime resources without contacting another service."""

    return _prepare_runtime_boundary(config).attestation


def recover_turn_outbox(
    config: Mapping[str, Any],
    deliver: Any,
    *,
    limit: int = 16,
    timeout_seconds: float = 0.25,
) -> int:
    """Caller-driven bounded recovery for committed Hermes turn observations.

    This recovery call starts no delivery daemon and performs no network I/O
    itself. The caller supplies an exact idempotent delivery function accepting
    the remaining ``timeout_seconds`` keyword and honoring that cooperative
    deadline.
    """

    if not isinstance(config, Mapping):
        raise RuntimeError("Colony plugin runtime configuration must be an object")
    if not callable(deliver):
        raise RuntimeError("turn outbox recovery requires a delivery callable")
    try:
        bounded_limit = max(1, min(int(limit), 100))
        bounded_timeout = max(0.01, min(float(timeout_seconds), 1.0))
    except (TypeError, ValueError, OverflowError):
        raise RuntimeError("turn outbox recovery bounds are invalid") from None
    outbox = TurnOutbox(_turn_outbox_path(config))
    outbox.prepare()
    return outbox.drain(
        deliver,
        limit=bounded_limit,
        timeout_seconds=bounded_timeout,
    )


def _require_coexistence_latches() -> None:
    values = {
        "COLONY_GENERAL_PLUGIN_ACTIVE": os.environ.get("COLONY_GENERAL_PLUGIN_ACTIVE", ""),
        "COLONY_MEMORY_WORKER_TOOLS": os.environ.get("COLONY_MEMORY_WORKER_TOOLS", ""),
        "COLONY_MEMORY_TURN_WRITER": os.environ.get("COLONY_MEMORY_TURN_WRITER", ""),
    }
    expected = {
        "COLONY_GENERAL_PLUGIN_ACTIVE": "1",
        "COLONY_MEMORY_WORKER_TOOLS": "0",
        "COLONY_MEMORY_TURN_WRITER": "disabled",
    }
    if values != expected:
        raise RuntimeError(
            "Colony memory coexistence latches are unsafe; require "
            "GENERAL_PLUGIN_ACTIVE=1, MEMORY_WORKER_TOOLS=0, and "
            "MEMORY_TURN_WRITER=disabled"
        )


def register(ctx: Any) -> None:
    """Register the governed adapter against Hermes 0.18.2-compatible APIs."""

    _require_coexistence_latches()
    for method in ("register_tool", "register_hook", "register_middleware"):
        if not callable(getattr(ctx, method, None)):
            raise RuntimeError(f"Hermes governance capability is unavailable: {method}")

    config = _plugin_config(ctx)
    url = str(config.get("url") or os.environ.get("COLONY_URL") or "http://127.0.0.1:7777")
    api_key = _resolve_env_placeholder(
        config.get("api_key") or os.environ.get("COLONY_API_KEY") or ""
    )
    owner_contact_id = str(
        config.get("owner_contact_id")
        or (
            config.get("contact_id")
            if str(config.get("contact_id") or "") not in {"", "default"}
            else ""
        )
        or os.environ.get("COLONY_OWNER_CONTACT_ID")
        or ""
    ).strip()
    configured_platforms = config.get("attested_system_platforms", ("cli",))
    if isinstance(configured_platforms, str):
        configured_platforms = [item for item in configured_platforms.split(",")]
    if not isinstance(configured_platforms, (list, tuple, set, frozenset)):
        configured_platforms = ("cli",)
    attested_system_platforms = frozenset(
        str(item).strip().lower()
        for item in configured_platforms
        if str(item).strip()
    )

    try:
        drain_timeout_seconds = max(
            0.01,
            min(float(config.get("turn_outbox_drain_timeout_ms", 250)) / 1000, 1.0),
        )
    except (TypeError, ValueError):
        raise RuntimeError("turn_outbox_drain_timeout_ms must be numeric") from None
    try:
        drain_limit = max(
            1, min(int(config.get("turn_outbox_drain_limit", 16)), 100),
        )
    except (TypeError, ValueError, OverflowError):
        raise RuntimeError("turn_outbox_drain_limit must be an integer") from None

    # This may initialize the configured local ledger, but it performs no
    # network I/O. It must succeed before the first middleware, tool, hook, or
    # command becomes visible to Hermes.
    boundary = _prepare_runtime_boundary(config)
    client = ColonyClient(url=url, api_key=api_key)
    mediator = boundary.mediator
    runtime_enabled_actions = boundary.enabled_action_tools
    turn_writer_platforms = boundary.turn_writer_platforms
    turn_outbox = boundary.turn_outbox
    _TRANSPORT_SCOPES.clear()
    dispatcher = _ToolDispatcher(
        client=client,
        mediator=mediator,
        owner_message_mediator=boundary.owner_message_mediator,
        owner_contact_id=owner_contact_id,
        attested_system_platforms=tuple(attested_system_platforms),
        enabled_action_tools=tuple(runtime_enabled_actions),
        enabled_message_tools=tuple(boundary.enabled_message_tools),
        enabled_read_tools=tuple(boundary.enabled_read_tools),
        scopes=_TRANSPORT_SCOPES,
    )

    def pre_llm_call(**kwargs: Any) -> None:
        scope = _resolve_scope(
            client,
            session_id=str(kwargs.get("session_id") or ""),
            task_id=str(kwargs.get("task_id") or ""),
            turn_id=str(kwargs.get("turn_id") or ""),
            platform=str(kwargs.get("platform") or ""),
            sender_id=str(kwargs.get("sender_id") or ""),
            user_message=str(kwargs.get("user_message") or ""),
            owner_contact_id=owner_contact_id,
            attested_system_platforms=attested_system_platforms,
        )
        _TRANSPORT_SCOPES.put(scope)
        return None

    def post_llm_call(**kwargs: Any) -> None:
        session_id = str(kwargs.get("session_id") or "")
        scope = _TRANSPORT_SCOPES.for_execution(
            session_id=session_id,
            task_id=str(kwargs.get("task_id") or ""),
            turn_id=str(kwargs.get("turn_id") or ""),
        )
        if scope is None or not scope.valid_participant:
            return None
        if (
            turn_writer_platforms is not None
            and scope.platform not in turn_writer_platforms
        ):
            return None
        user_message = str(kwargs.get("user_message") or scope.user_message or "")
        assistant_message = str(kwargs.get("assistant_response") or "")
        if not (user_message or assistant_message):
            return None
        stable_turn_id = derive_hermes_turn_id(
            session_id=session_id,
            turn_id=str(kwargs.get("turn_id") or ""),
            task_id=str(kwargs.get("task_id") or ""),
            user_message=user_message,
            assistant_response=assistant_message,
            conversation_history=kwargs.get("conversation_history") or [],
            model=str(kwargs.get("model") or ""),
            platform=scope.platform,
        )
        payload = {
            "session_id": session_id,
            "contact_id": scope.contact_id,
            "turn_id": stable_turn_id,
            "user_message": user_message,
            "assistant_message": assistant_message,
            "require_source_receipt": True,
            "summary": (
                f"User: {user_message[:300]}\nAgent: {assistant_message[:300]}"
                if user_message and assistant_message else ""
            ),
            "model": str(kwargs.get("model") or ""),
            "sender": {"platform": scope.platform, "user_id": scope.sender_id},
        }
        try:
            receipt = turn_outbox.enqueue(stable_turn_id, payload)
        except TurnOutboxConflict:
            logger.critical(
                "stable Hermes turn id was reused with different canonical content"
            )
            return None
        except (TurnOutboxFull, TurnOutboxPayloadError, OSError) as error:
            logger.error(
                "durable Hermes turn enqueue failed (%s)", type(error).__name__,
            )
            return None
        except BaseException as error:
            # Turn observation must never suppress an already-safe user reply.
            logger.error(
                "durable Hermes turn enqueue failed (%s)", type(error).__name__,
            )
            return None
        if receipt.get("state") == "pending":
            try:
                def deliver_turn(
                    stored: Mapping[str, Any], *, timeout_seconds: float,
                ) -> bool:
                    return client.sync_turn(
                        **stored, outbox=turn_outbox, timeout_seconds=timeout_seconds,
                    )

                turn_outbox.drain(
                    deliver_turn,
                    limit=drain_limit,
                    timeout_seconds=drain_timeout_seconds,
                )
            except BaseException as error:
                # The committed row remains available to the next resident or
                # one-shot Hermes process; only a redacted type is logged.
                logger.warning(
                    "durable Hermes turn drain deferred (%s)", type(error).__name__,
                )
        return None

    def transform_llm_output(**kwargs: Any) -> str | None:
        candidate = str(kwargs.get("response_text") or "")
        platform = str(kwargs.get("platform") or "")
        if not candidate or _voice_surface(platform):
            return None
        mode = _guard_chat_mode()
        if mode == "off":
            return None
        try:
            if len(candidate) > _GUARD_EXACT_RESPONSE_LIMIT:
                return _GUARD_WITHHELD_TEXT if mode == "enforce" else None
            scope = _TRANSPORT_SCOPES.for_session(str(kwargs.get("session_id") or ""))
            if scope is None or not scope.valid_participant:
                return _GUARD_WITHHELD_TEXT if mode == "enforce" else None
            payload = {
                "surface": "text_chat",
                "response_text": candidate,
                "incoming_message_text": scope.user_message[:2000],
                "target_contact_id": scope.contact_id,
                "target_gateway": scope.platform,
                "session_id": scope.session_id,
                "mode": mode,
            }
            if mode == "shadow":
                def shadow_check() -> None:
                    try:
                        client.post(
                            "/v1/host/response-guard/check", json=payload, timeout=3,
                        )
                    except BaseException:
                        return None

                try:
                    threading.Thread(target=shadow_check, daemon=True).start()
                except BaseException:
                    return None
                return None

            response = client.post(
                "/v1/host/response-guard/check", json=payload, timeout=3,
            )
            response.raise_for_status()
            verdict = response.json()
            if not _guard_verdict_valid(verdict, candidate=candidate, mode="enforce"):
                return _GUARD_WITHHELD_TEXT
            return None if verdict.get("decision") == "allow" else _GUARD_WITHHELD_TEXT
        except BaseException:
            # ``transform_llm_output`` is the actual pinned Hermes mutation
            # point.  Enforce therefore fails closed, including non-Exception
            # transport interruptions; shadow remains observational.
            return _GUARD_WITHHELD_TEXT if mode == "enforce" else None

    # Register the context carrier before any model-visible handler.
    ctx.register_middleware("tool_execution", _tool_execution_middleware)
    for schema in _TOOL_SCHEMAS:
        name = schema["name"]
        if name in _READ_TOOL_NAMES and name not in boundary.enabled_read_tools:
            continue
        if name in _ACTION_INTENT_TOOL_NAMES and name not in runtime_enabled_actions:
            continue
        if name in _OWNER_MESSAGE_TOOL_NAMES and name not in boundary.enabled_message_tools:
            continue
        ctx.register_tool(
            name=name,
            toolset="colony",
            schema=schema,
            handler=(
                lambda args=None, _name=name, **kwargs:
                dispatcher.dispatch(_name, args or {}, **kwargs)
            ),
        )

    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("transform_llm_output", transform_llm_output)
    ctx.register_hook("post_llm_call", post_llm_call)

    register_command = getattr(ctx, "register_command", None) or getattr(
        ctx, "register_slash_command", None
    )
    if callable(register_command):
        for command_name, handler in SLASH_COMMANDS.items():
            register_command(
                f"colony {command_name}",
                lambda args="", _handler=handler: _handler(args),
            )

    logger.info(
        "governed Colony plugin registered "
        "(runtime_ready=%s, mediator=%s, reads=%d, intents=%d, messages=%d)",
        boundary.attestation["runtime_ready"],
        "configured" if _mediator_runtime_posture(mediator)["ready"] else "unavailable",
        len(boundary.enabled_read_tools),
        len(runtime_enabled_actions),
        len(boundary.enabled_message_tools),
    )


__all__ = [
    "HermesToolActionIntentV1",
    "HermesOwnerMessageIntentV1",
    "ActionMediator",
    "OwnerMessageMediator",
    "GOVERNED_EVENT_TYPES",
    "PrivateSQLitePathError",
    "_TOOL_SCHEMAS",
    "governance_attestation",
    "recover_turn_outbox",
    "register",
    "runtime_governance_attestation",
]
