"""Stateless wire contract for governed Colony host-worker actions.

This module is the single written-down form of the governed-action wire
contract: schema names, field sets, identifier grammars, size bounds, and the
two canonical-JSON digest conventions.  Everything here is stdlib-only and
free of I/O so that any process — the private host worker, tooling, or tests —
can validate the contract without importing a server.

CRITICAL DESIGN RULE — INDEPENDENT VALIDATORS, DO NOT "UNIFY"
=============================================================
ColonyAI's endpoint (``sidecar/colony_sidecar/governed_actions.py``) MUST KEEP
ITS OWN INDEPENDENT VALIDATOR.  Do NOT refactor the endpoint to import
``colony_hostworker``.  The two implementations are deliberately separate and
cross-check each other; that redundancy has already caught a real
incompatibility (the ASCII/UTF-8 canonical-JSON digest split documented
below).  A repo-internal test (``sidecar/tests/test_hostworker_agreement.py``)
runs BOTH implementations against shared golden vectors and fails if they
disagree.  Anyone later "cleaning this up" by import-unifying them turns this
from a safety improvement into a safety loss: a single shared bug would then
validate itself on both sides of the trust boundary.

THE TWO CANONICAL-JSON CONVENTIONS
==================================
There are — deliberately and permanently — two canonical JSON serializations
on this wire, differing only in ``ensure_ascii``:

* ASCII-escaped (``canonical_json_ascii`` / ``sha256_json_ascii``): the
  historical convention introduced by the Hermes plugin.  It computes
  ``args_sha256``, ``context_sha256``, ``idempotency_key``, ``intent_digest``,
  and the ``intent_id`` derivation of ``HermesToolActionIntentV1``.

* UTF-8 (``canonical_json_utf8`` / ``sha256_json_utf8``): the host Action
  Plane convention.  It computes the outer ``execution_digest`` of
  ``ColonyGovernedActionExecutionV1``, the returned ``effect_digest``, the
  action ``payload_sha256`` (a.k.a. ``action_digest``), and every receipt
  ``evidence_sha256``.

The split exists because two codebases grew the two digests independently and
both are now pinned by durable ledgers and immutable receipts on both sides.
Re-serializing either family with the other convention changes every digest
on the wire.  The conventions are therefore named separately, pinned by golden
vectors, and must never be merged or "fixed".
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping


class GovernedContractError(ValueError):
    """A value is not the exact bounded governed-action contract."""


# --------------------------------------------------------------------------
# Schema names (public wire strings; never rename)
# --------------------------------------------------------------------------

INTENT_SCHEMA = "HermesToolActionIntentV1"
INTENT_ENVELOPE_SCHEMA = "HermesToolActionEnvelopeV1"
CALL_IDENTITY_SCHEMA = "HermesActionCallV1"
EXECUTION_REQUEST_SCHEMA = "ColonyGovernedActionExecutionV1"
APPROVAL_BINDING_SCHEMA = "ColonyOwnerApprovalExecutionBindingV1"
EXECUTION_RESULT_SCHEMA = "ColonyGovernedActionExecutionResultV1"
EFFECT_SCHEMA = "ColonyGovernedActionEffectV1"

# --------------------------------------------------------------------------
# Exact field sets
# --------------------------------------------------------------------------

INTENT_FIELDS = frozenset(
    {
        "schema",
        "version",
        "intent_id",
        "idempotency_key",
        "tool_name",
        "args",
        "args_sha256",
        "context",
        "context_sha256",
        "intent_digest",
    }
)
CONTEXT_FIELDS = frozenset(
    {
        "api_request_id",
        "authority_lane",
        "contact_id",
        "platform",
        "sender_id",
        "session_id",
        "task_id",
        "tool_call_id",
        "turn_id",
    }
)
EXECUTION_REQUEST_FIELDS = frozenset(
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
APPROVAL_BINDING_FIELDS = frozenset(
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
EXECUTION_RESULT_FIELDS = frozenset(
    {
        "schema",
        "version",
        "execution_digest",
        "action_id",
        "action_digest",
        "intent_id",
        "intent_digest",
        "tool_name",
        "status",
        "effect_state",
        "effect",
        "effect_digest",
        "observed_at",
    }
)
EFFECT_FIELDS = frozenset(
    {"schema", "version", "effect_id", "outcome", "verification"}
)

# --------------------------------------------------------------------------
# Identifier grammars
# --------------------------------------------------------------------------

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
INTENT_ID_RE = re.compile(r"^hti_[0-9a-f]{32}$")
# The approval-id form ColonyAI's endpoint already enforces on the wire.
APPROVAL_ID_RE = re.compile(r"^APR-[A-Z0-9]{12}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
ACTION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# --------------------------------------------------------------------------
# Size and time bounds
# --------------------------------------------------------------------------

EXECUTION_REQUEST_MAX_BYTES = 32 * 1024
EXECUTION_RESULT_MAX_BYTES = 16 * 1024
EFFECT_MAX_BYTES = 8 * 1024
RESEARCH_TOPIC_MAX_CHARS = 1400
APPROVAL_MAX_LIFETIME_SECONDS = 86_400
# The one skew allowance shared by every "decided in the future?" sanity
# check on both sides of the boundary today.
GATE_CLOCK_SKEW_SECONDS = 30.0

# --------------------------------------------------------------------------
# Canonical JSON — both conventions, separately named on purpose
# --------------------------------------------------------------------------


def canonical_json_ascii(value: Any) -> str:
    """Historical ASCII-escaped canonical JSON.

    Digest family: ``args_sha256``, ``context_sha256``, ``idempotency_key``,
    ``intent_digest`` (and the ``intent_id`` derived from the idempotency
    key).  See the module docstring for why this must never be merged with
    :func:`canonical_json_utf8`.
    """

    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise GovernedContractError("value is not canonical JSON") from error


def canonical_json_utf8(value: Any) -> str:
    """Host Action Plane canonical UTF-8 JSON.

    Digest family: ``execution_digest``, ``effect_digest``,
    ``payload_sha256``/``action_digest``, and receipt ``evidence_sha256``.
    See the module docstring for why this must never be merged with
    :func:`canonical_json_ascii`.
    """

    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise GovernedContractError("value is not canonical JSON") from error


def sha256_json_ascii(value: Any) -> str:
    return hashlib.sha256(canonical_json_ascii(value).encode("utf-8")).hexdigest()


def sha256_json_utf8(value: Any) -> str:
    return hashlib.sha256(canonical_json_utf8(value).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Shared bounded-scalar validators (used by the catalog and the intent)
# --------------------------------------------------------------------------


def exact_mapping(
    value: Any,
    name: str,
    *,
    allowed: frozenset[str] | set[str],
    required=(),
) -> dict[str, Any]:
    """Return ``dict(value)`` iff keys are strings within/covering the bounds."""

    if not isinstance(value, Mapping):
        raise GovernedContractError("%s must be an object" % name)
    keys = set(value)
    if any(not isinstance(key, str) for key in keys):
        raise GovernedContractError("%s keys must be strings" % name)
    if keys - set(allowed) or set(required) - keys:
        raise GovernedContractError("%s fields are invalid" % name)
    return dict(value)


def bounded_text(
    value: Any,
    name: str,
    maximum: int,
    *,
    allow_empty: bool = False,
    identifier: bool = False,
) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise GovernedContractError("%s must be a bounded string" % name)
    if any(
        ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise GovernedContractError("%s contains invalid characters" % name)
    if not allow_empty and not value.strip():
        raise GovernedContractError("%s cannot be empty" % name)
    if identifier and not IDENTIFIER_RE.fullmatch(value):
        raise GovernedContractError("%s is not a canonical identifier" % name)
    return value


def bounded_integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GovernedContractError("%s must be an integer" % name)
    if value < minimum or value > maximum:
        raise GovernedContractError("%s is outside its allowed range" % name)
    return value


def bounded_number(
    value: Any, name: str, minimum: float, maximum: float
) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GovernedContractError("%s must be a number" % name)
    try:
        finite = math.isfinite(float(value))
    except (OverflowError, ValueError):
        finite = False
    if not finite or value < minimum or value > maximum:
        raise GovernedContractError("%s is outside its allowed range" % name)
    return value


def enum_text(value: Any, name: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise GovernedContractError("%s is not an allowed value" % name)
    return value


def bounded_json_value(
    value: Any, name: str, *, depth: int = 0, counter=None
) -> Any:
    """Validate one small JSON value without coercion or exotic numerics."""

    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > 512 or depth > 8:
        raise GovernedContractError("%s is too complex" % name)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > (1 << 63) - 1:
            raise GovernedContractError("%s integer is too large" % name)
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GovernedContractError("%s number must be finite" % name)
        return value
    if isinstance(value, str):
        return bounded_text(value, name, 4096, allow_empty=True)
    if isinstance(value, list):
        return [
            bounded_json_value(item, name, depth=depth + 1, counter=counter)
            for item in value
        ]
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            key = bounded_text(key, "%s key" % name, 128, identifier=True)
            result[key] = bounded_json_value(
                item, name, depth=depth + 1, counter=counter
            )
        return result
    raise GovernedContractError("%s contains a non-JSON value" % name)


__all__ = (
    "ACTION_ID_RE",
    "APPROVAL_BINDING_FIELDS",
    "APPROVAL_BINDING_SCHEMA",
    "APPROVAL_ID_RE",
    "APPROVAL_MAX_LIFETIME_SECONDS",
    "CALL_IDENTITY_SCHEMA",
    "CONTEXT_FIELDS",
    "EFFECT_FIELDS",
    "EFFECT_MAX_BYTES",
    "EFFECT_SCHEMA",
    "EXECUTION_REQUEST_FIELDS",
    "EXECUTION_REQUEST_MAX_BYTES",
    "EXECUTION_REQUEST_SCHEMA",
    "EXECUTION_RESULT_FIELDS",
    "EXECUTION_RESULT_MAX_BYTES",
    "EXECUTION_RESULT_SCHEMA",
    "GATE_CLOCK_SKEW_SECONDS",
    "GovernedContractError",
    "IDENTIFIER_RE",
    "INTENT_ENVELOPE_SCHEMA",
    "INTENT_FIELDS",
    "INTENT_ID_RE",
    "INTENT_SCHEMA",
    "RESEARCH_TOPIC_MAX_CHARS",
    "SAFE_ID_RE",
    "SHA256_RE",
    "bounded_integer",
    "bounded_json_value",
    "bounded_number",
    "bounded_text",
    "canonical_json_ascii",
    "canonical_json_utf8",
    "enum_text",
    "exact_mapping",
    "sha256_json_ascii",
    "sha256_json_utf8",
)
