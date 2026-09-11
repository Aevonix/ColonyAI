"""Validation of the immutable ``HermesToolActionIntentV1`` wire document.

The schema string on the wire is exactly ``"HermesToolActionIntentV1"`` — it
is already public and must never be renamed.  Neither the intent body nor its
diagnostic context is authority: this module only proves that the document is
internally consistent (exact fields, catalog-valid args, and all four
ASCII-convention digests recomputed and matched) and produces a bounded
owner-facing approval description.  It performs no effects.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from .catalog import ACTION_TOOL_NAMES, TOOL_CATALOG, validate_tool_args
from .contract import (
    CALL_IDENTITY_SCHEMA,
    CONTEXT_FIELDS,
    GovernedContractError,
    INTENT_FIELDS,
    INTENT_ID_RE,
    INTENT_SCHEMA,
    SHA256_RE,
    bounded_text,
    canonical_json_ascii,
    exact_mapping,
    sha256_json_ascii,
)


class HermesActionIntentError(GovernedContractError):
    """The submitted intent is not the exact governed contract."""


def _validate_context(raw: Any) -> dict[str, str]:
    context = exact_mapping(
        raw,
        "context",
        allowed=CONTEXT_FIELDS,
        required=CONTEXT_FIELDS,
    )
    for key, value in context.items():
        bounded_text(value, "context.%s" % key, 512, allow_empty=True)
    return context


def _call_identity_digest(tool_name: str, context: Mapping[str, str]) -> str:
    return sha256_json_ascii(
        {
            "schema": CALL_IDENTITY_SCHEMA,
            "tool_name": tool_name,
            "api_request_id": context.get("api_request_id", ""),
            "session_id": context.get("session_id", ""),
            "task_id": context.get("task_id", ""),
            "tool_call_id": context.get("tool_call_id", ""),
            "turn_id": context.get("turn_id", ""),
        }
    )


@dataclass(frozen=True, slots=True)
class HermesToolActionIntentV1:
    """Immutable, digest-verified governed action intent."""

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
        cls,
        *,
        tool_name: str,
        args: Mapping[str, Any],
        context: Mapping[str, str],
    ) -> "HermesToolActionIntentV1":
        """Construct a new intent exactly as the Hermes plugin does."""

        validated_args = validate_tool_args(tool_name, args)
        validated_context = _validate_context(context)
        args_json = canonical_json_ascii(validated_args)
        context_json = canonical_json_ascii(validated_context)
        args_sha = sha256_json_ascii(validated_args)
        context_sha = sha256_json_ascii(validated_context)
        idempotency_key = _call_identity_digest(tool_name, validated_context)
        intent_id = "hti_" + idempotency_key[:32]
        unsigned = {
            "schema": INTENT_SCHEMA,
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
            intent_digest=sha256_json_ascii(unsigned),
            _args_json=args_json,
            _context_json=context_json,
        )

    @classmethod
    def from_mapping(cls, value: Any) -> "HermesToolActionIntentV1":
        try:
            document = exact_mapping(
                value,
                "intent",
                allowed=INTENT_FIELDS,
                required=INTENT_FIELDS,
            )
            if document["schema"] != INTENT_SCHEMA:
                raise HermesActionIntentError("intent schema is invalid")
            if isinstance(document["version"], bool) or document["version"] != 1:
                raise HermesActionIntentError("intent version is invalid")
            tool_name = document["tool_name"]
            if not isinstance(tool_name, str) or tool_name not in ACTION_TOOL_NAMES:
                raise HermesActionIntentError("tool is not a governed action intent")
            args = validate_tool_args(tool_name, document["args"])
            context = _validate_context(document["context"])
        except HermesActionIntentError:
            raise
        except GovernedContractError as error:
            raise HermesActionIntentError(str(error)) from error
        args_sha256 = document["args_sha256"]
        context_sha256 = document["context_sha256"]
        idempotency_key = document["idempotency_key"]
        intent_id = document["intent_id"]
        intent_digest = document["intent_digest"]
        for name, digest in (
            ("args_sha256", args_sha256),
            ("context_sha256", context_sha256),
            ("idempotency_key", idempotency_key),
            ("intent_digest", intent_digest),
        ):
            if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                raise HermesActionIntentError("%s is invalid" % name)
        if not isinstance(intent_id, str) or not INTENT_ID_RE.fullmatch(intent_id):
            raise HermesActionIntentError("intent_id is invalid")
        if args_sha256 != sha256_json_ascii(args):
            raise HermesActionIntentError("args digest does not match")
        if context_sha256 != sha256_json_ascii(context):
            raise HermesActionIntentError("context digest does not match")
        if idempotency_key != _call_identity_digest(tool_name, context):
            raise HermesActionIntentError(
                "idempotency key does not match call identity"
            )
        if intent_id != "hti_" + idempotency_key[:32]:
            raise HermesActionIntentError("intent ID does not match call identity")
        unsigned = {
            "schema": INTENT_SCHEMA,
            "version": 1,
            "intent_id": intent_id,
            "idempotency_key": idempotency_key,
            "tool_name": tool_name,
            "args": args,
            "args_sha256": args_sha256,
            "context": context,
            "context_sha256": context_sha256,
        }
        if intent_digest != sha256_json_ascii(unsigned):
            raise HermesActionIntentError("intent digest does not match")
        try:
            args_json = canonical_json_ascii(args)
            context_json = canonical_json_ascii(context)
        except GovernedContractError as error:
            raise HermesActionIntentError(str(error)) from error
        return cls(
            intent_id=intent_id,
            idempotency_key=idempotency_key,
            tool_name=tool_name,
            args_sha256=args_sha256,
            context_sha256=context_sha256,
            intent_digest=intent_digest,
            _args_json=args_json,
            _context_json=context_json,
        )

    @property
    def args(self) -> dict[str, Any]:
        return json.loads(self._args_json)

    @property
    def context(self) -> dict[str, str]:
        return json.loads(self._context_json)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": INTENT_SCHEMA,
            "version": 1,
            "intent_id": self.intent_id,
            "idempotency_key": self.idempotency_key,
            "tool_name": self.tool_name,
            "args": self.args,
            "args_sha256": self.args_sha256,
            "context": self.context,
            "context_sha256": self.context_sha256,
            "intent_digest": self.intent_digest,
        }

    def approval_display(self) -> dict[str, str]:
        return TOOL_CATALOG[self.tool_name].approval_display(self.args)


__all__ = (
    "HermesActionIntentError",
    "HermesToolActionIntentV1",
)
