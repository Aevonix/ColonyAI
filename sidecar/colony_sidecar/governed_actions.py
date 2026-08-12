"""Durable, generic execution boundary for owner-governed Colony actions.

This module deliberately knows nothing about conversational context.  One
dedicated API principal supplies the owner/participant binding, while the
request supplies only an immutable, already-approved action description.

The ledger uses a conservative one-mutation-at-most protocol:

``prepared -> executing -> completed``

Any exception after ``executing`` is durable ``ambiguous`` and can never be
retried as a mutation.  An ``executing`` row found at process startup is also
recovered as ``ambiguous``.  This intentionally prefers an honest uncertain
result over accidentally applying an effect twice.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from typing import Any, Mapping
import uuid

from colony_sidecar.api.authority import RequestAuthority


logger = logging.getLogger(__name__)

GOVERNED_ACTION_PRINCIPAL = "host-action-worker"
GOVERNED_ACTION_SCOPES = frozenset({"actions:execute", "actions:verify"})
ACTION_TOOL_NAMES = frozenset(
    {
        "colony_autonomy_disable",
        "colony_autonomy_enable",
        "colony_create_commitment",
        "colony_initiative_feedback",
        "colony_record_insight",
        "colony_research",
        "colony_resolve_commitment",
        "colony_task_complete",
        "colony_task_dismiss",
        "colony_task_snooze",
    }
)

GOVERNED_ACTION_REQUEST_MAX_BYTES = 32 * 1024
_REQUEST_MAX = GOVERNED_ACTION_REQUEST_MAX_BYTES
_REQUEST_FIELDS = frozenset(
    {
        "schema",
        "version",
        "action_id",
        "action_digest",
        "intent_id",
        "intent_digest",
        "tool_name",
        "args",
        "args_sha256",
        "approval",
        "execution_digest",
    }
)
_APPROVAL_FIELDS = frozenset(
    {
        "schema",
        "version",
        "approval_id",
        "decision_id",
        "revision",
        "authorization_receipt_sha256",
        "decided_at",
        "expires_at",
    }
)
_EFFECT_FIELDS = frozenset(
    {"schema", "version", "effect_id", "outcome", "verification"}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_INTENT_ID_RE = re.compile(r"^hti_[0-9a-f]{32}$")
_APPROVAL_ID_RE = re.compile(r"^APR-[A-Z0-9]{12}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
GOVERNED_IDENTIFIER_PATTERN = (
    r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}(?![\s\S])"
)
_REQUEST_ID_RE = re.compile(GOVERNED_IDENTIFIER_PATTERN)
_STATES = frozenset(
    {"prepared", "executing", "completed", "failed", "ambiguous"}
)
GOVERNED_RESEARCH_TOPIC_MAX_CHARS = 1400
GOVERNED_COMMITMENT_PRIORITY_DEFAULT = 60
GOVERNED_COMMITMENT_DESCRIPTION_MAX_CHARS = 8000
GOVERNED_COMMITMENT_DUE_AT_MAX_CHARS = 256
GOVERNED_INSIGHT_CONTENT_MAX_CHARS = 16000
GOVERNED_FREEFORM_REASON_MAX_CHARS = 8000
GOVERNED_IDENTIFIER_MAX_CHARS = 256
GOVERNED_DETAILS_MAX_NODES = 512
GOVERNED_DETAILS_MAX_DEPTH = 8
GOVERNED_DETAILS_STRING_MAX_CHARS = 4096
GOVERNED_DETAILS_KEY_MAX_CHARS = 128
GOVERNED_DETAILS_INTEGER_MAX = (1 << 63) - 1


class GovernedActionValidationError(ValueError):
    """The external execution document is not the exact bounded contract."""


class GovernedActionConflict(ValueError):
    """An action identifier is already bound to a different request."""


class GovernedActionNotFound(LookupError):
    """No durable execution record exists for the requested action."""


class PrivateGovernedActionLedgerError(OSError):
    """The governed-action ledger path is not owner-private and stable."""


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise GovernedActionValidationError("value is not canonical JSON") from exc


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _wire_sha256_json(value: Any) -> str:
    """Hash the host Action Plane wire form without changing local storage.

    The host's args/intent digests intentionally use the historical ASCII-escaped
    canonical form above.  Only its outer execution envelope and returned
    effect digest use canonical UTF-8 JSON, so those two fields call this
    narrowly scoped helper.
    """

    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise GovernedActionValidationError("value is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


class _FrozenJSONDict(dict):
    """JSON-serializable mapping that refuses in-place authority changes."""

    @staticmethod
    def _immutable(*_args, **_kwargs):
        raise TypeError("governed execution context is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _FrozenJSONDict({
            key: _freeze_json(item) for key, item in value.items()
        })
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _strict_json(raw: bytes, *, maximum: int = _REQUEST_MAX) -> Any:
    if not isinstance(raw, bytes) or not raw or len(raw) > maximum:
        raise GovernedActionValidationError("execution document is outside its size bound")

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise GovernedActionValidationError("JSON object keys must be unique")
            result[key] = value
        return result

    def constant(_value):
        raise GovernedActionValidationError("JSON numbers must be finite")

    def integer(value):
        if len(value.lstrip("-")) > 19:
            raise GovernedActionValidationError("JSON integer is outside its bound")
        parsed = int(value)
        if abs(parsed) > (1 << 63) - 1:
            raise GovernedActionValidationError("JSON integer is outside its bound")
        return parsed

    def floating(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise GovernedActionValidationError("JSON numbers must be finite")
        return parsed

    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=constant,
            parse_int=integer,
            parse_float=floating,
        )
    except GovernedActionValidationError:
        raise
    except (UnicodeError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise GovernedActionValidationError("execution document is malformed") from exc

    stack = [(value, 0)]
    count = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > 1024 or depth > 16:
            raise GovernedActionValidationError("execution document is too complex")
        if isinstance(item, str):
            if len(item) > 16_000 or any(
                ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF
                for character in item
            ):
                raise GovernedActionValidationError("execution text is unsafe")
        elif isinstance(item, Mapping):
            stack.extend((key, depth + 1) for key in item)
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif item is not None and not isinstance(item, (bool, int, float)):
            raise GovernedActionValidationError("execution document contains a non-JSON value")
    return value


def _exact_mapping(value: Any, fields, *, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(fields):
        raise GovernedActionValidationError(f"{name} fields are invalid")
    if any(not isinstance(key, str) for key in value):
        raise GovernedActionValidationError(f"{name} keys must be strings")
    return dict(value)


def _text(
    value: Any,
    name: str,
    maximum: int,
    *,
    allow_empty: bool = False,
    identifier: bool = False,
) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise GovernedActionValidationError(f"{name} must be a bounded string")
    if any(ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise GovernedActionValidationError(f"{name} contains invalid characters")
    if not allow_empty and not value.strip():
        raise GovernedActionValidationError(f"{name} cannot be empty")
    if identifier and not _REQUEST_ID_RE.fullmatch(value):
        raise GovernedActionValidationError(f"{name} is not a canonical identifier")
    return value


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GovernedActionValidationError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise GovernedActionValidationError(f"{name} is outside its range")
    return value


def _number(value: Any, name: str, minimum: float, maximum: float) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GovernedActionValidationError(f"{name} must be a number")
    try:
        finite = math.isfinite(float(value))
    except (ValueError, OverflowError):
        finite = False
    if not finite or not minimum <= value <= maximum:
        raise GovernedActionValidationError(f"{name} is outside its range")
    return value


def _enum(value: Any, name: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise GovernedActionValidationError(f"{name} is not an allowed value")
    return value


def _bounded_json(value: Any, name: str, *, depth=0, counter=None) -> Any:
    if counter is None:
        counter = [0]
    counter[0] += 1
    if (
        counter[0] > GOVERNED_DETAILS_MAX_NODES
        or depth > GOVERNED_DETAILS_MAX_DEPTH
    ):
        raise GovernedActionValidationError(f"{name} is too complex")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > GOVERNED_DETAILS_INTEGER_MAX:
            raise GovernedActionValidationError(f"{name} integer is too large")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GovernedActionValidationError(f"{name} number must be finite")
        return value
    if isinstance(value, str):
        return _text(
            value, name, GOVERNED_DETAILS_STRING_MAX_CHARS, allow_empty=True,
        )
    if isinstance(value, list):
        return [
            _bounded_json(item, name, depth=depth + 1, counter=counter)
            for item in value
        ]
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            key = _text(
                key, f"{name} key", GOVERNED_DETAILS_KEY_MAX_CHARS,
                identifier=True,
            )
            result[key] = _bounded_json(
                item, name, depth=depth + 1, counter=counter
            )
        return result
    raise GovernedActionValidationError(f"{name} contains a non-JSON value")


def _bounded_verification(value: Any, *, depth=0, counter=None) -> Any:
    """Match the host worker's fixed verification projection bounds exactly."""

    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > 256 or depth > 6:
        raise RuntimeError("governed action verification is too complex")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > (1 << 63) - 1:
            raise RuntimeError("governed action verification integer is unsafe")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RuntimeError("governed action verification number is unsafe")
        return value
    if isinstance(value, str):
        if len(value) > 2048 or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in value
        ):
            raise RuntimeError("governed action verification text is unsafe")
        return value
    if isinstance(value, list):
        return [
            _bounded_verification(item, depth=depth + 1, counter=counter)
            for item in value
        ]
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or not _SAFE_ID_RE.fullmatch(key):
                raise RuntimeError("governed action verification key is unsafe")
            result[key] = _bounded_verification(
                item, depth=depth + 1, counter=counter
            )
        return result
    raise RuntimeError("governed action verification is not JSON")


def _validate_args(tool_name: str, raw: Any) -> dict[str, Any]:
    if tool_name in {"colony_autonomy_disable", "colony_autonomy_enable"}:
        return _exact_mapping(raw, set(), name="args")
    if tool_name == "colony_create_commitment":
        args = _exact_mapping(
            raw,
            set(raw) if isinstance(raw, Mapping) else set(),
            name="args",
        )
        if set(args) - {"description", "due_at", "priority"} or "description" not in args:
            raise GovernedActionValidationError("args fields are invalid")
        _text(
            args["description"], "description",
            GOVERNED_COMMITMENT_DESCRIPTION_MAX_CHARS,
        )
        if "due_at" in args:
            _text(
                args["due_at"], "due_at", GOVERNED_COMMITMENT_DUE_AT_MAX_CHARS,
                allow_empty=True,
            )
        if "priority" in args:
            _integer(args["priority"], "priority", 0, 100)
        return args
    if tool_name == "colony_initiative_feedback":
        if not isinstance(raw, Mapping):
            raise GovernedActionValidationError("args must be an object")
        args = dict(raw)
        if (
            set(args) - {"initiative_id", "action", "details"}
            or not {"initiative_id", "action"} <= set(args)
        ):
            raise GovernedActionValidationError("args fields are invalid")
        _text(
            args["initiative_id"], "initiative_id", GOVERNED_IDENTIFIER_MAX_CHARS,
            identifier=True,
        )
        _enum(
            args["action"],
            "action",
            frozenset({"acknowledged", "actioned", "dismissed", "snoozed"}),
        )
        if "details" in args:
            if not isinstance(args["details"], Mapping):
                raise GovernedActionValidationError("details must be an object")
            args["details"] = _bounded_json(args["details"], "details")
        return args
    if tool_name == "colony_record_insight":
        if not isinstance(raw, Mapping):
            raise GovernedActionValidationError("args must be an object")
        args = dict(raw)
        if (
            set(args) - {"confidence", "content", "insight_type"}
            or not {"content", "insight_type"} <= set(args)
        ):
            raise GovernedActionValidationError("args fields are invalid")
        _text(
            args["content"], "content", GOVERNED_INSIGHT_CONTENT_MAX_CHARS,
        )
        _enum(
            args["insight_type"],
            "insight_type",
            frozenset(
                {
                    "preference",
                    "connection",
                    "fact",
                    "goal_hint",
                    "relationship_update",
                }
            ),
        )
        if "confidence" in args:
            _number(args["confidence"], "confidence", 0.0, 1.0)
        return args
    if tool_name == "colony_research":
        if not isinstance(raw, Mapping):
            raise GovernedActionValidationError("args must be an object")
        args = dict(raw)
        if set(args) - {"depth", "topic"} or "topic" not in args:
            raise GovernedActionValidationError("args fields are invalid")
        _text(
            args["topic"], "topic", GOVERNED_RESEARCH_TOPIC_MAX_CHARS,
        )
        if "depth" in args:
            _enum(args["depth"], "depth", frozenset({"quick", "standard", "deep"}))
        return args
    if tool_name == "colony_resolve_commitment":
        if not isinstance(raw, Mapping):
            raise GovernedActionValidationError("args must be an object")
        args = dict(raw)
        if set(args) - {"commitment_id", "outcome", "reason"} or "commitment_id" not in args:
            raise GovernedActionValidationError("args fields are invalid")
        _text(
            args["commitment_id"], "commitment_id", GOVERNED_IDENTIFIER_MAX_CHARS,
            identifier=True,
        )
        if "outcome" in args:
            _enum(
                args["outcome"],
                "outcome",
                frozenset({"done", "invalid", "duplicate", "wont_do", "obsolete"}),
            )
        if "reason" in args:
            _text(
                args["reason"], "reason", GOVERNED_FREEFORM_REASON_MAX_CHARS,
                allow_empty=True,
            )
        return args
    if tool_name == "colony_task_complete":
        args = _exact_mapping(raw, {"task_id"}, name="args")
        _text(
            args["task_id"], "task_id", GOVERNED_IDENTIFIER_MAX_CHARS,
            identifier=True,
        )
        return args
    if tool_name == "colony_task_dismiss":
        if not isinstance(raw, Mapping):
            raise GovernedActionValidationError("args must be an object")
        args = dict(raw)
        if set(args) - {"task_id", "reason"} or "task_id" not in args:
            raise GovernedActionValidationError("args fields are invalid")
        _text(
            args["task_id"], "task_id", GOVERNED_IDENTIFIER_MAX_CHARS,
            identifier=True,
        )
        if "reason" in args:
            _enum(
                args["reason"],
                "reason",
                frozenset({"stale", "completed", "abandoned", "not_applicable"}),
            )
        return args
    if tool_name == "colony_task_snooze":
        if not isinstance(raw, Mapping):
            raise GovernedActionValidationError("args must be an object")
        args = dict(raw)
        if set(args) - {"task_id", "hours", "reason"} or "task_id" not in args:
            raise GovernedActionValidationError("args fields are invalid")
        _text(
            args["task_id"], "task_id", GOVERNED_IDENTIFIER_MAX_CHARS,
            identifier=True,
        )
        if "hours" in args:
            _integer(args["hours"], "hours", 1, 168)
        if "reason" in args:
            _text(
                args["reason"], "reason", GOVERNED_FREEFORM_REASON_MAX_CHARS,
                allow_empty=True,
            )
        return args
    raise GovernedActionValidationError("tool is not a governed Colony action")


def _valid_action_id(value: Any) -> str:
    if not isinstance(value, str):
        raise GovernedActionValidationError("action_id is invalid")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise GovernedActionValidationError("action_id is invalid") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise GovernedActionValidationError("action_id must be a canonical UUIDv4")
    return value


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise GovernedActionValidationError(f"{name} is invalid")
    return value


def parse_execution_request(
    raw: bytes,
    *,
    path_action_id: str,
) -> dict[str, Any]:
    document = _exact_mapping(_strict_json(raw), _REQUEST_FIELDS, name="execution")
    if (
        document["schema"] != "ColonyGovernedActionExecutionV1"
        or isinstance(document["version"], bool)
        or document["version"] != 1
    ):
        raise GovernedActionValidationError("execution schema is invalid")
    action_id = _valid_action_id(document["action_id"])
    if action_id != _valid_action_id(path_action_id):
        raise GovernedActionValidationError("URL action_id does not match the execution")
    _digest(document["action_digest"], "action_digest")
    _digest(document["intent_digest"], "intent_digest")
    _digest(document["args_sha256"], "args_sha256")
    _digest(document["execution_digest"], "execution_digest")
    if not isinstance(document["intent_id"], str) or not _INTENT_ID_RE.fullmatch(
        document["intent_id"]
    ):
        raise GovernedActionValidationError("intent_id is invalid")
    tool_name = document["tool_name"]
    if not isinstance(tool_name, str) or tool_name not in ACTION_TOOL_NAMES:
        raise GovernedActionValidationError("tool is not a governed Colony action")
    args = _validate_args(tool_name, document["args"])
    if document["args_sha256"] != sha256_json(args):
        raise GovernedActionValidationError("args digest does not match")

    approval = _exact_mapping(document["approval"], _APPROVAL_FIELDS, name="approval")
    if (
        approval["schema"] != "ColonyOwnerApprovalExecutionBindingV1"
        or isinstance(approval["version"], bool)
        or approval["version"] != 1
        or not isinstance(approval["approval_id"], str)
        or not _APPROVAL_ID_RE.fullmatch(approval["approval_id"])
        or isinstance(approval["revision"], bool)
        or approval["revision"] != 1
    ):
        raise GovernedActionValidationError("approval binding is invalid")
    _text(approval["decision_id"], "decision_id", 128, identifier=True)
    _digest(
        approval["authorization_receipt_sha256"],
        "authorization_receipt_sha256",
    )
    for field in ("decided_at", "expires_at"):
        value = approval[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise GovernedActionValidationError("approval time is invalid")
        try:
            finite = math.isfinite(float(value))
        except (ValueError, OverflowError):
            finite = False
        if not finite or float(value) <= 0:
            raise GovernedActionValidationError("approval time is invalid")
    decided_at = float(approval["decided_at"])
    expires_at = float(approval["expires_at"])
    if expires_at <= decided_at or expires_at - decided_at > 86_400:
        raise GovernedActionValidationError("approval lifetime is invalid")

    normalized = dict(document)
    normalized["args"] = args
    normalized["approval"] = approval
    unsigned = {key: value for key, value in normalized.items() if key != "execution_digest"}
    if normalized["execution_digest"] != _wire_sha256_json(unsigned):
        raise GovernedActionValidationError("execution digest does not match")
    # One final canonicalization catches exotic mapping/value behavior and is
    # also the exact document persisted in the ledger.
    canonical_json(normalized)
    return normalized


def _clock_value(clock) -> float:
    try:
        now = float(clock())
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("governed action clock is unavailable") from exc
    if not math.isfinite(now) or now <= 0:
        raise RuntimeError("governed action clock is unavailable")
    return now


def _assert_live_approval(request: Mapping[str, Any], now: float) -> None:
    approval = request["approval"]
    if float(approval["decided_at"]) > now + 30 or float(approval["expires_at"]) <= now:
        raise GovernedActionValidationError("approval binding is not live")


def _owner_from_authority(
    authority: RequestAuthority,
    *,
    required_scope: str,
) -> str:
    if (
        not isinstance(authority, RequestAuthority)
        or not authority.authenticated
        or authority.legacy
        or authority.anonymous
        or authority.principal_id != GOVERNED_ACTION_PRINCIPAL
        or not authority.credential_id
        or authority.allow_unscoped_api is not False
        or authority.scopes != GOVERNED_ACTION_SCOPES
        or required_scope not in authority.scopes
        or authority.audiences != frozenset({"owner"})
        or not authority.viewer_person_id
        or authority.viewer_person_id not in authority.person_ids
    ):
        raise PermissionError("exact owner-bound governed-action principal required")
    return authority.viewer_person_id


def _status_text(value: Any) -> str:
    raw = getattr(value, "value", value)
    text = str(raw or "unknown")
    return text if _SAFE_ID_RE.fullmatch(text) else "unknown"


def _effect_id(kind: str, value: Any) -> str:
    text = str(value or "")
    if _SAFE_ID_RE.fullmatch(text):
        return text
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:32]
    return f"{kind}-{digest}"


def _validated_effect(value: Any) -> dict[str, Any]:
    effect = _exact_mapping(value, _EFFECT_FIELDS, name="effect")
    if (
        effect["schema"] != "ColonyGovernedActionEffectV1"
        or isinstance(effect["version"], bool)
        or effect["version"] != 1
        or not isinstance(effect["effect_id"], str)
        or not _SAFE_ID_RE.fullmatch(effect["effect_id"])
        or not isinstance(effect["outcome"], str)
        or not _SAFE_ID_RE.fullmatch(effect["outcome"])
    ):
        raise RuntimeError("governed action effect projection is invalid")
    verification = _bounded_verification(effect["verification"])
    normalized = dict(effect)
    normalized["verification"] = verification
    if len(canonical_json(normalized).encode("utf-8")) > 8 * 1024:
        raise RuntimeError("governed action effect projection is too large")
    return normalized


def _state_result(
    request: Mapping[str, Any],
    *,
    state: str,
    observed_at: float,
    effect: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if state not in _STATES:
        raise RuntimeError("invalid governed action ledger state")
    if state == "completed":
        if effect is None:
            raise RuntimeError("completed action is missing its effect")
        normalized_effect = _validated_effect(effect)
        effect_state = "performed"
    else:
        normalized_effect = {
            "schema": "ColonyGovernedActionEffectV1",
            "version": 1,
            "effect_id": request["action_id"],
            "outcome": state,
            "verification": {"state": state},
        }
        effect_state = {
            "prepared": "not_started",
            "executing": "uncertain",
            "failed": "not_performed",
            "ambiguous": "uncertain",
        }[state]
    result = {
        "schema": "ColonyGovernedActionExecutionResultV1",
        "version": 1,
        "execution_digest": request["execution_digest"],
        "action_id": request["action_id"],
        "action_digest": request["action_digest"],
        "intent_id": request["intent_id"],
        "intent_digest": request["intent_digest"],
        "tool_name": request["tool_name"],
        "status": state,
        "effect_state": effect_state,
        "effect": normalized_effect,
        "effect_digest": _wire_sha256_json(normalized_effect),
        "observed_at": observed_at,
    }
    encoded = canonical_json(result)
    if len(encoded.encode("utf-8")) > 16 * 1024:
        raise RuntimeError("governed action result is too large")
    # Returning the canonical round-trip keeps the first HTTP projection and
    # every ledger replay identical down to JSON member order.
    return json.loads(encoded)


class _PrivateLedgerPath:
    """Bind SQLite to one no-follow, owner-private local path.

    The immediate parent is an exact mode-0700 current-user directory, so
    SQLite's transient rollback journal remains private regardless of process
    umask. The database is an atomically created or pre-existing mode-0600,
    current-user, single-link regular file. Directory components and the leaf
    are opened with ``O_NOFOLLOW`` and the leaf inode is re-attested around
    every ledger operation.
    """

    def __init__(self, path: str | Path) -> None:
        raw = os.path.expanduser(str(path))
        candidate = Path(raw)
        if (
            not candidate.is_absolute()
            or candidate != Path(os.path.normpath(raw))
            or not candidate.name
            or any(component in {".", ".."} for component in candidate.parts)
        ):
            raise PrivateGovernedActionLedgerError(
                "governed action ledger path must be absolute and normalized"
            )
        required = ("O_DIRECTORY", "O_NOFOLLOW")
        if any(not hasattr(os, name) for name in required) or not hasattr(
            os, "geteuid"
        ):
            raise PrivateGovernedActionLedgerError(
                "governed action ledger requires POSIX no-follow file access"
            )
        self.path = candidate
        self.euid = os.geteuid()

    @staticmethod
    def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
        return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)

    def _validate_directory(
        self, value: os.stat_result, *, private_parent: bool
    ) -> None:
        if not stat.S_ISDIR(value.st_mode):
            raise PrivateGovernedActionLedgerError(
                "governed action ledger parent chain is not a directory"
            )
        if private_parent:
            if value.st_uid != self.euid or stat.S_IMODE(value.st_mode) != 0o700:
                raise PrivateGovernedActionLedgerError(
                    "governed action ledger parent must be current-user mode 0700"
                )
        elif value.st_uid not in {0, self.euid}:
            raise PrivateGovernedActionLedgerError(
                "governed action ledger ancestor has an untrusted owner"
            )

    def _open_parent(self) -> int:
        flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            current_fd = os.open(self.path.anchor, flags)
        except OSError:
            raise PrivateGovernedActionLedgerError(
                "governed action ledger root cannot be opened"
            ) from None
        components = self.path.parts[1:-1]
        try:
            self._validate_directory(
                os.fstat(current_fd), private_parent=not components
            )
            for index, component in enumerate(components):
                private_parent = index == len(components) - 1
                try:
                    before = os.stat(
                        component, dir_fd=current_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    try:
                        os.mkdir(component, 0o700, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                    except OSError:
                        raise PrivateGovernedActionLedgerError(
                            "governed action ledger parent cannot be created"
                        ) from None
                    try:
                        before = os.stat(
                            component, dir_fd=current_fd, follow_symlinks=False
                        )
                    except OSError:
                        raise PrivateGovernedActionLedgerError(
                            "governed action ledger parent is unstable"
                        ) from None
                self._validate_directory(before, private_parent=private_parent)
                try:
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                except OSError:
                    raise PrivateGovernedActionLedgerError(
                        "governed action ledger parent cannot be opened without links"
                    ) from None
                try:
                    after = os.fstat(next_fd)
                    self._validate_directory(after, private_parent=private_parent)
                    if not self._same_inode(before, after):
                        raise PrivateGovernedActionLedgerError(
                            "governed action ledger parent changed during open"
                        )
                except BaseException:
                    os.close(next_fd)
                    raise
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except BaseException:
            os.close(current_fd)
            raise

    def _validate_leaf(self, value: os.stat_result) -> None:
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_uid != self.euid
            or value.st_nlink != 1
            or stat.S_IMODE(value.st_mode) != 0o600
        ):
            raise PrivateGovernedActionLedgerError(
                "governed action ledger must be a current-user mode-0600 "
                "single-link file"
            )

    def _assert_leaf(self, parent_fd: int, identity: tuple[int, int]) -> None:
        try:
            current = os.stat(
                self.path.name, dir_fd=parent_fd, follow_symlinks=False
            )
        except OSError:
            raise PrivateGovernedActionLedgerError(
                "governed action ledger disappeared"
            ) from None
        self._validate_leaf(current)
        if (current.st_dev, current.st_ino) != identity:
            raise PrivateGovernedActionLedgerError(
                "governed action ledger changed identity"
            )

    def _assert_no_wal_sidecars(self, parent_fd: int) -> None:
        for suffix in ("-wal", "-shm"):
            try:
                os.stat(
                    self.path.name + suffix,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            except OSError:
                raise PrivateGovernedActionLedgerError(
                    "governed action ledger sidecar posture is unavailable"
                ) from None
            raise PrivateGovernedActionLedgerError(
                "governed action ledger must not retain WAL or SHM sidecars"
            )

    def _open_leaf(self) -> tuple[int, int, tuple[int, int]]:
        parent_fd = self._open_parent()
        flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        leaf_fd: int | None = None
        try:
            try:
                before = os.stat(
                    self.path.name, dir_fd=parent_fd, follow_symlinks=False
                )
                self._validate_leaf(before)
                leaf_fd = os.open(self.path.name, flags, dir_fd=parent_fd)
                opened = os.fstat(leaf_fd)
                self._validate_leaf(opened)
                if not self._same_inode(before, opened):
                    raise PrivateGovernedActionLedgerError(
                        "governed action ledger changed during open"
                    )
            except FileNotFoundError:
                try:
                    leaf_fd = os.open(
                        self.path.name,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=parent_fd,
                    )
                except FileExistsError:
                    before = os.stat(
                        self.path.name, dir_fd=parent_fd, follow_symlinks=False
                    )
                    self._validate_leaf(before)
                    leaf_fd = os.open(self.path.name, flags, dir_fd=parent_fd)
                    opened = os.fstat(leaf_fd)
                    if not self._same_inode(before, opened):
                        raise PrivateGovernedActionLedgerError(
                            "governed action ledger changed during create"
                        )
                else:
                    os.fchmod(leaf_fd, 0o600)
                    opened = os.fstat(leaf_fd)
                self._validate_leaf(opened)
            identity = (opened.st_dev, opened.st_ino)
            self._assert_leaf(parent_fd, identity)
            self._assert_no_wal_sidecars(parent_fd)
            return parent_fd, leaf_fd, identity
        except BaseException:
            if leaf_fd is not None:
                os.close(leaf_fd)
            os.close(parent_fd)
            raise

    def connect(self) -> tuple[sqlite3.Connection, tuple[int, int]]:
        parent_fd, leaf_fd, identity = self._open_leaf()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                str(self.path), check_same_thread=False, timeout=5.0
            )
            self._assert_leaf(parent_fd, identity)
            mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if str(mode).lower() != "delete":
                raise PrivateGovernedActionLedgerError(
                    "governed action ledger could not disable persistent sidecars"
                )
            self._assert_no_wal_sidecars(parent_fd)
            return connection, identity
        except BaseException:
            if connection is not None:
                connection.close()
            raise
        finally:
            os.close(leaf_fd)
            os.close(parent_fd)

    def assert_current(self, identity: tuple[int, int]) -> None:
        parent_fd = self._open_parent()
        try:
            self._assert_leaf(parent_fd, identity)
            self._assert_no_wal_sidecars(parent_fd)
        finally:
            os.close(parent_fd)


class GovernedActionLedger:
    """SQLite truth for one-mutation-at-most governed effects."""

    def __init__(self, db_path: str | Path, *, clock=time.time) -> None:
        self._storage = _PrivateLedgerPath(db_path)
        self.path = self._storage.path
        self.clock = clock
        self._lock = threading.RLock()
        self._conn, self._identity = self._storage.connect()
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS governed_actions (
                action_id TEXT PRIMARY KEY,
                request_sha256 TEXT NOT NULL,
                execution_digest TEXT NOT NULL,
                request_json TEXT NOT NULL,
                owner_person_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('prepared','executing','completed','failed','ambiguous')
                ),
                result_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL
            )
            """
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS governed_actions_execution_digest "
            "ON governed_actions(execution_digest)"
        )
        self._conn.commit()
        self._storage.assert_current(self._identity)
        self._recover_executing()

    @staticmethod
    def _request_sha(request: Mapping[str, Any]) -> str:
        return sha256_json(dict(request))

    def _recover_executing(self) -> None:
        now = _clock_value(self.clock)
        with self._lock:
            self._storage.assert_current(self._identity)
            rows = self._conn.execute(
                "SELECT action_id, request_json FROM governed_actions WHERE state = 'executing'"
            ).fetchall()
            if not rows:
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for row in rows:
                    request = json.loads(row["request_json"])
                    result = _state_result(request, state="ambiguous", observed_at=now)
                    self._conn.execute(
                        "UPDATE governed_actions SET state = 'ambiguous', result_json = ?, "
                        "updated_at = ? WHERE action_id = ? AND state = 'executing'",
                        (canonical_json(result), now, row["action_id"]),
                    )
                self._conn.commit()
                self._storage.assert_current(self._identity)
            except Exception:
                self._conn.rollback()
                self._storage.assert_current(self._identity)
                raise

    def get(self, action_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._storage.assert_current(self._identity)
            row = self._conn.execute(
                "SELECT * FROM governed_actions WHERE action_id = ?", (action_id,)
            ).fetchone()
            self._storage.assert_current(self._identity)
        if row is None:
            return None
        value = dict(row)
        value["request"] = json.loads(value.pop("request_json"))
        raw_result = value.pop("result_json")
        value["result"] = json.loads(raw_result) if raw_result is not None else None
        return value

    def prepare_execution(
        self,
        request: Mapping[str, Any],
        *,
        owner_person_id: str,
    ) -> tuple[dict[str, Any], bool]:
        action_id = request["action_id"]
        request_json = canonical_json(dict(request))
        request_sha = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        now = _clock_value(self.clock)
        with self._lock:
            self._storage.assert_current(self._identity)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM governed_actions WHERE action_id = ?", (action_id,)
                ).fetchone()
                created = row is None
                if row is None:
                    try:
                        self._conn.execute(
                            """
                            INSERT INTO governed_actions (
                                action_id, request_sha256, execution_digest, request_json,
                                owner_person_id, state, result_json, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, 'prepared', NULL, ?, ?)
                            """,
                            (
                                action_id,
                                request_sha,
                                request["execution_digest"],
                                request_json,
                                owner_person_id,
                                now,
                                now,
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise GovernedActionConflict(
                            "execution digest is already bound to another action"
                        ) from exc
                else:
                    if (
                        row["request_sha256"] != request_sha
                        or row["execution_digest"] != request["execution_digest"]
                        or row["owner_person_id"] != owner_person_id
                    ):
                        raise GovernedActionConflict(
                            "action_id is already bound to another execution"
                        )
                self._conn.commit()
                self._storage.assert_current(self._identity)
            except Exception:
                self._conn.rollback()
                self._storage.assert_current(self._identity)
                raise
        record = self.get(action_id)
        assert record is not None
        return record, created

    def mark_executing(self, action_id: str) -> dict[str, Any]:
        now = _clock_value(self.clock)
        with self._lock:
            self._storage.assert_current(self._identity)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(
                    "UPDATE governed_actions SET state = 'executing', result_json = NULL, "
                    "started_at = ?, updated_at = ? "
                    "WHERE action_id = ? AND state = 'prepared'",
                    (now, now, action_id),
                )
                if cursor.rowcount != 1:
                    raise GovernedActionConflict("action is not prepared for execution")
                self._conn.commit()
                self._storage.assert_current(self._identity)
            except Exception:
                self._conn.rollback()
                self._storage.assert_current(self._identity)
                raise
        record = self.get(action_id)
        assert record is not None
        return record

    def finish(
        self,
        action_id: str,
        *,
        state: str,
        effect: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if state not in {"completed", "ambiguous"}:
            raise ValueError("executing action may only complete or become ambiguous")
        now = _clock_value(self.clock)
        record = self.get(action_id)
        if record is None or record["state"] != "executing":
            raise GovernedActionConflict("action is not executing")
        result = _state_result(
            record["request"], state=state, observed_at=now, effect=effect
        )
        encoded = canonical_json(result)
        with self._lock:
            self._storage.assert_current(self._identity)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(
                    "UPDATE governed_actions SET state = ?, result_json = ?, updated_at = ? "
                    "WHERE action_id = ? AND state = 'executing'",
                    (state, encoded, now, action_id),
                )
                if cursor.rowcount != 1:
                    raise GovernedActionConflict("action execution state changed")
                self._conn.commit()
                self._storage.assert_current(self._identity)
            except Exception:
                self._conn.rollback()
                self._storage.assert_current(self._identity)
                raise
        return result

    def fail_prepared(self, action_id: str) -> dict[str, Any]:
        now = _clock_value(self.clock)
        record = self.get(action_id)
        if record is None or record["state"] != "prepared":
            raise GovernedActionConflict("action is not prepared")
        result = _state_result(record["request"], state="failed", observed_at=now)
        encoded = canonical_json(result)
        with self._lock:
            self._storage.assert_current(self._identity)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(
                    "UPDATE governed_actions SET state = 'failed', result_json = ?, "
                    "updated_at = ? WHERE action_id = ? AND state = 'prepared'",
                    (encoded, now, action_id),
                )
                if cursor.rowcount != 1:
                    raise GovernedActionConflict("action preparation state changed")
                self._conn.commit()
                self._storage.assert_current(self._identity)
            except Exception:
                self._conn.rollback()
                self._storage.assert_current(self._identity)
                raise
        return result

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class GovernedActionService:
    """Validate, fence, execute once, and project one governed action."""

    def __init__(self, ledger: GovernedActionLedger, executor, *, clock=time.time) -> None:
        if not isinstance(ledger, GovernedActionLedger):
            raise TypeError("governed action ledger is required")
        if not callable(getattr(executor, "prepare", None)) or not callable(
            getattr(executor, "perform", None)
        ):
            raise TypeError("governed action executor is invalid")
        self.ledger = ledger
        self.executor = executor
        self.clock = clock
        # Owner-approved mutations are rare.  A single dispatch lock avoids a
        # growing per-action lock cache and makes same-process concurrency
        # obviously one-writer without affecting read-only observations.
        self._dispatch_lock = asyncio.Lock()

    async def execute(
        self,
        path_action_id: str,
        raw: bytes,
        authority: RequestAuthority,
    ) -> dict[str, Any]:
        owner = _owner_from_authority(authority, required_scope="actions:execute")
        request = parse_execution_request(raw, path_action_id=path_action_id)
        execution_context = _freeze_json(request)
        async with self._dispatch_lock:
            existing = self.ledger.get(request["action_id"])
            if existing is not None:
                expected = sha256_json(request)
                if (
                    existing["request_sha256"] != expected
                    or existing["owner_person_id"] != owner
                ):
                    raise GovernedActionConflict(
                        "action_id is already bound to another execution"
                    )
                if existing["state"] != "prepared":
                    if existing["result"] is None:
                        return _state_result(
                            existing["request"],
                            state=existing["state"],
                            observed_at=_clock_value(self.clock),
                        )
                    return existing["result"]
                try:
                    _assert_live_approval(request, _clock_value(self.clock))
                except GovernedActionValidationError:
                    return self.ledger.fail_prepared(request["action_id"])
            else:
                _assert_live_approval(request, _clock_value(self.clock))
                existing, _created = self.ledger.prepare_execution(
                    request, owner_person_id=owner
                )

            try:
                await self.executor.prepare(execution_context, owner)
            except Exception as exc:
                logger.warning(
                    "Governed action %s failed read-only preparation (type=%s)",
                    request["action_id"], type(exc).__name__,
                )
                return self.ledger.fail_prepared(request["action_id"])

            try:
                _assert_live_approval(request, _clock_value(self.clock))
            except GovernedActionValidationError:
                return self.ledger.fail_prepared(request["action_id"])

            self.ledger.mark_executing(request["action_id"])
            try:
                effect = await self.executor.perform(
                    execution_context, owner
                )
                effect = _validated_effect(effect)
            except BaseException as exc:
                # BaseException is deliberate: cancellation, shutdown, or any
                # other unwind after the durable executing marker has an
                # uncertain effect outcome and therefore must never retry.
                logger.warning(
                    "Governed action %s has an ambiguous effect outcome (type=%s)",
                    request["action_id"], type(exc).__name__,
                )
                ambiguous = self.ledger.finish(
                    request["action_id"], state="ambiguous"
                )
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
                return ambiguous
            return self.ledger.finish(
                request["action_id"], state="completed", effect=effect
            )

    async def observe(
        self, action_id: str, authority: RequestAuthority
    ) -> dict[str, Any]:
        _owner_from_authority(authority, required_scope="actions:verify")
        action_id = _valid_action_id(action_id)
        record = self.ledger.get(action_id)
        if record is None:
            raise GovernedActionNotFound("governed action was not found")
        if record["owner_person_id"] != authority.viewer_person_id:
            raise PermissionError("governed action is outside the owner binding")
        if record["result"] is not None:
            return record["result"]
        return _state_result(
            record["request"],
            state=record["state"],
            observed_at=_clock_value(self.clock),
        )

    def close(self) -> None:
        self.ledger.close()


async def _await(value):
    return await value if inspect.isawaitable(value) else value


class ColonySubsystemActionExecutor:
    """Map the ten generic action names onto existing Colony subsystems."""

    def __init__(
        self,
        *,
        graph=None,
        goals=None,
        commitments=None,
        initiatives=None,
        projects=None,
        feedback=None,
        autonomy_enable=None,
        autonomy_disable=None,
        autonomy_running=None,
    ) -> None:
        self.graph = graph
        self.goals = goals
        self.commitments = commitments
        self.initiatives = initiatives
        self.projects = projects
        self.feedback = feedback
        self.autonomy_enable = autonomy_enable
        self.autonomy_disable = autonomy_disable
        self.autonomy_running = autonomy_running

    def _dependency(self, value, name: str):
        if value is None:
            raise RuntimeError(f"{name} is not initialized")
        return value

    @staticmethod
    def _governed_research_project(
        request: Mapping[str, Any], owner_person_id: str,
    ):
        from colony_sidecar.projects import Project
        from colony_sidecar.work_orders import action_authority

        args = request["args"]
        approval = request["approval"]
        identity_material = {
            "schema": "ColonyGovernedResearchProjectIdentityV1",
            "version": 1,
            "owner_person_id": owner_person_id,
            "action_id": request["action_id"],
            "action_digest": request["action_digest"],
            "execution_digest": request["execution_digest"],
        }
        identity = sha256_json(identity_material)
        depth = args.get("depth", "quick")
        return Project(
            id="proj-governed-" + identity[:20],
            title="Governed research " + identity[:8],
            objective=(
                "Research topic:\n" + args["topic"] + "\nDepth: " + depth
            ),
            source="governed_action",
            status="planning",
            outcome="pending",
            entity_ids=[],
            reason="",
            replans=0,
            next_review_at=0.0,
            source_event_refs=[
                "governed-action:" + request["action_id"],
                "governed-intent:" + request["intent_id"],
            ],
            evidence_refs=[
                "action-digest:" + request["action_digest"],
                "intent-digest:" + request["intent_digest"],
                "args-digest:" + request["args_sha256"],
                "execution-digest:" + request["execution_digest"],
            ],
            policy_decision_refs=[
                "approval:" + approval["approval_id"],
                "decision:" + approval["decision_id"],
                "approval-revision:" + str(approval["revision"]),
                "authorization-receipt-digest:"
                + approval["authorization_receipt_sha256"],
            ],
            subject_person_id=owner_person_id,
            viewer_scope="owner",
            shareability="owner_private",
            capability_allowlist=list(action_authority("research")[1]),
            goal_fingerprint=identity,
        )

    async def prepare(
        self, request: Mapping[str, Any], owner_person_id: str,
    ) -> None:
        tool_name = request["tool_name"]
        args = request["args"]
        if tool_name not in ACTION_TOOL_NAMES:
            raise RuntimeError("unknown governed Colony action")
        if tool_name == "colony_autonomy_enable":
            self._dependency(self.autonomy_enable, "autonomy control")
            self._dependency(self.autonomy_running, "autonomy status")
        elif tool_name == "colony_autonomy_disable":
            self._dependency(self.autonomy_disable, "autonomy control")
            self._dependency(self.autonomy_running, "autonomy status")
        elif tool_name == "colony_create_commitment":
            self._dependency(self.commitments, "commitment store")
        elif tool_name == "colony_resolve_commitment":
            store = self._dependency(self.commitments, "commitment store")
            row = store.get(args["commitment_id"])
            if row is None or row.get("person_id") != owner_person_id:
                raise RuntimeError("owner commitment was not found")
        elif tool_name in {
            "colony_task_complete",
            "colony_task_snooze",
            "colony_task_dismiss",
        }:
            goals = self._dependency(self.goals, "goal store")
            try:
                goals.get_goal(args["task_id"])
            except Exception as exc:
                raise RuntimeError("task was not found") from exc
        elif tool_name == "colony_initiative_feedback":
            initiatives = self._dependency(self.initiatives, "initiative store")
            if initiatives.get(args["initiative_id"]) is None:
                raise RuntimeError("initiative was not found")
        elif tool_name == "colony_record_insight":
            graph = self._dependency(self.graph, "memory graph")
            if not callable(getattr(graph, "store_memory", None)):
                raise RuntimeError("memory graph insight writer is not initialized")
        elif tool_name == "colony_research":
            projects = self._dependency(self.projects, "ProjectEngine")
            prepare = getattr(projects, "prepare_governed_research", None)
            enqueue = getattr(projects, "enqueue_governed_research", None)
            if not callable(prepare) or not callable(enqueue):
                raise RuntimeError(
                    "ProjectEngine governed research handoff is not initialized"
                )
            prepare(self._governed_research_project(request, owner_person_id))

    async def perform(
        self, request: Mapping[str, Any], owner_person_id: str
    ) -> dict[str, Any]:
        tool_name = request["tool_name"]
        args = request["args"]
        if tool_name == "colony_autonomy_enable":
            await _await(self.autonomy_enable())
            running = bool(await _await(self.autonomy_running()))
            return self._effect(
                "autonomy",
                "enabled" if running else "start_requested",
                {"running": running},
            )
        if tool_name == "colony_autonomy_disable":
            await _await(self.autonomy_disable())
            running = bool(await _await(self.autonomy_running()))
            return self._effect(
                "autonomy",
                "stop_requested" if running else "disabled",
                {"running": running},
            )
        if tool_name == "colony_create_commitment":
            row = self.commitments.create(
                person_id=owner_person_id,
                description=args["description"],
                due_at=args.get("due_at") or None,
                priority=args.get(
                    "priority", GOVERNED_COMMITMENT_PRIORITY_DEFAULT,
                ),
                source_type="governed_action",
                source_context="owner_authorized",
                metadata={"governed": True},
            )
            effect_id = _effect_id("commitment", row.get("id"))
            return self._effect(
                effect_id,
                "created",
                {"commitment_id": effect_id, "status": _status_text(row.get("status"))},
            )
        if tool_name == "colony_resolve_commitment":
            row = self.commitments.resolve(
                args["commitment_id"],
                outcome=args.get("outcome", "done"),
                note=args.get("reason", ""),
                resolved_by="governed-owner",
            )
            if row is None:
                raise RuntimeError("commitment was not found")
            effect_id = _effect_id("commitment", row.get("id") or args["commitment_id"])
            return self._effect(
                effect_id,
                "resolved",
                {"commitment_id": effect_id, "status": _status_text(row.get("status"))},
            )
        if tool_name == "colony_initiative_feedback":
            item = self.initiatives.get(args["initiative_id"])
            status_map = {
                "acknowledged": "acknowledged",
                "dismissed": "cancelled",
                "snoozed": "pending",
                "actioned": "completed",
            }
            action = args["action"]
            updated = self.initiatives.update(
                args["initiative_id"], status=status_map[action]
            )
            if updated is None:
                raise RuntimeError("initiative update failed")
            try:
                self.initiatives.log_history(
                    args["initiative_id"],
                    action="governed_" + action,
                    agent_id="governed-action-worker",
                    details=json.loads(canonical_json(args.get("details", {}))),
                )
            except Exception:
                logger.warning("Governed initiative history could not be recorded", exc_info=True)
            if self.feedback is not None and getattr(item, "type", None):
                try:
                    self.feedback.record(item.type, action)
                except Exception:
                    logger.warning("Governed initiative feedback could not be recorded", exc_info=True)
            effect_id = _effect_id("initiative", args["initiative_id"])
            return self._effect(
                effect_id,
                action,
                {
                    "initiative_id": effect_id,
                    "action": action,
                    "status": _status_text(getattr(updated, "status", status_map[action])),
                },
            )
        if tool_name == "colony_record_insight":
            content_hash = sha256_json({
                "schema": "ColonyGovernedInsightIdentityV1",
                "version": 1,
                "person_id": owner_person_id,
                "insight_type": args["insight_type"],
                "source": "governed_action",
                "content": args["content"],
            })
            insight_id = await self.graph.store_memory(
                content=args["content"],
                memory_type="semantic",
                entities=[],
                metadata={
                    "governed": True,
                    "insight_type": args["insight_type"],
                    "source": "governed_action",
                },
                importance=args.get("confidence", 0.7),
                person_id=owner_person_id,
                source_type="inference",
                content_hash=content_hash,
            )
            effect_id = _effect_id("insight", insight_id)
            return self._effect(
                effect_id,
                "recorded",
                {"insight_id": effect_id, "insight_type": args["insight_type"]},
            )
        if tool_name == "colony_research":
            project = self.projects.enqueue_governed_research(
                self._governed_research_project(request, owner_person_id)
            )
            effect_id = _effect_id("project", project.id)
            return self._effect(
                effect_id,
                "queued",
                {
                    "project_id": effect_id,
                    "depth": args.get("depth", "quick"),
                    "status": _status_text(project.status),
                },
            )
        if tool_name == "colony_task_complete":
            if not self.goals.complete_task(args["task_id"]):
                raise RuntimeError("task completion failed")
            effect_id = _effect_id("task", args["task_id"])
            return self._effect(
                effect_id,
                "completed",
                {"task_id": effect_id, "status": "completed"},
            )
        if tool_name == "colony_task_snooze":
            hours = args.get("hours", 24)
            if not self.goals.snooze_task(
                args["task_id"], hours, args.get("reason", "")
            ):
                raise RuntimeError("task snooze failed")
            effect_id = _effect_id("task", args["task_id"])
            return self._effect(
                effect_id,
                "snoozed",
                {"task_id": effect_id, "status": "snoozed", "hours": hours},
            )
        if tool_name == "colony_task_dismiss":
            reason = args.get("reason", "stale")
            if not self.goals.dismiss_task(args["task_id"], reason):
                raise RuntimeError("task dismissal failed")
            effect_id = _effect_id("task", args["task_id"])
            return self._effect(
                effect_id,
                "dismissed",
                {"task_id": effect_id, "status": "dismissed", "reason": reason},
            )
        raise RuntimeError("unknown governed Colony action")

    @staticmethod
    def _effect(effect_id: str, outcome: str, verification: Mapping[str, Any]):
        return {
            "schema": "ColonyGovernedActionEffectV1",
            "version": 1,
            "effect_id": _effect_id("effect", effect_id),
            "outcome": outcome,
            "verification": dict(verification),
        }


__all__ = (
    "ACTION_TOOL_NAMES",
    "ColonySubsystemActionExecutor",
    "GOVERNED_ACTION_PRINCIPAL",
    "GOVERNED_ACTION_REQUEST_MAX_BYTES",
    "GOVERNED_ACTION_SCOPES",
    "GOVERNED_COMMITMENT_DESCRIPTION_MAX_CHARS",
    "GOVERNED_COMMITMENT_DUE_AT_MAX_CHARS",
    "GOVERNED_COMMITMENT_PRIORITY_DEFAULT",
    "GOVERNED_DETAILS_INTEGER_MAX",
    "GOVERNED_DETAILS_KEY_MAX_CHARS",
    "GOVERNED_DETAILS_MAX_DEPTH",
    "GOVERNED_DETAILS_MAX_NODES",
    "GOVERNED_DETAILS_STRING_MAX_CHARS",
    "GOVERNED_FREEFORM_REASON_MAX_CHARS",
    "GOVERNED_IDENTIFIER_MAX_CHARS",
    "GOVERNED_IDENTIFIER_PATTERN",
    "GOVERNED_INSIGHT_CONTENT_MAX_CHARS",
    "GOVERNED_RESEARCH_TOPIC_MAX_CHARS",
    "GovernedActionConflict",
    "GovernedActionLedger",
    "GovernedActionNotFound",
    "GovernedActionService",
    "GovernedActionValidationError",
    "PrivateGovernedActionLedgerError",
    "canonical_json",
    "parse_execution_request",
    "sha256_json",
)
