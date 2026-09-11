"""Versioned communication constraints for one ResponseGuard evaluation.

The embedding host remains the authority source.  This contract only binds a
candidate response to the exact target, route, grant, and semantic policy that
the host says constrain it.  It deliberately cannot grant execution/control
authority or select private owner context.

``policy_digest`` identifies the host's complete immutable policy record.  The
separate ``context_digest`` covers the exact, reduced record Colony received
and made visible to guard/model inputs, so callers can detect substitution or
field-level tampering without Colony needing the host's private policy store.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


COMMUNICATION_POLICY_CONTEXT_SCHEMA = "CommunicationPolicyContextV1"
COMMUNICATION_POLICY_CONTEXT_VERSION = 1
COMMUNICATION_DISCLOSURE_CLASSES = frozenset(
    {"none", "public", "contact_scoped", "owner_explicit"}
)
MAX_COMMUNICATION_PURPOSE_CHARS = 1_024
MAX_COMMUNICATION_DISCLOSURE_STATEMENT_CHARS = 2_048

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{7,255}$")
_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")


def _canonical_json(value: object) -> str:
    """Return the one stable JSON representation used by ``context_digest``."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


class CommunicationPolicyContextV1(BaseModel):
    """Exact, immutable communication-policy input for ResponseGuard.

    Every field is required, including ``schema``, ``version``, and the three
    literal-false capability statements.  That makes a partial policy invalid
    instead of allowing defaults to silently widen or reinterpret it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_name: Literal["CommunicationPolicyContextV1"] = Field(
        alias="schema", serialization_alias="schema"
    )
    version: Literal[1]
    target_contact_id: str = Field(min_length=1, max_length=128)
    route_id: str = Field(min_length=8, max_length=256)
    grant_id: str = Field(min_length=8, max_length=256)
    grant_digest: str = Field(min_length=64, max_length=64)
    policy_id: str = Field(min_length=8, max_length=256)
    policy_digest: str = Field(min_length=64, max_length=64)
    purpose: str = Field(min_length=1, max_length=MAX_COMMUNICATION_PURPOSE_CHARS)
    disclosure_class: Literal[
        "none", "public", "contact_scoped", "owner_explicit"
    ]
    disclosure_statement: str = Field(
        min_length=1,
        max_length=MAX_COMMUNICATION_DISCLOSURE_STATEMENT_CHARS,
    )
    grants_execution_authority: Literal[False]
    grants_control_authority: Literal[False]
    selects_owner_private_context: Literal[False]

    @field_validator(
        "target_contact_id",
        "purpose",
        "disclosure_statement",
    )
    @classmethod
    def _canonical_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("communication policy text must be canonical")
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
            raise ValueError("communication policy text contains control characters")
        return value

    @field_validator("target_contact_id")
    @classmethod
    def _exact_target(cls, value: str) -> str:
        if (
            value.lower() in {"all", "any", "everyone"}
            or any(character in value for character in ("*", "?", "[", "]"))
        ):
            raise ValueError("communication policy target must be exact")
        return value

    @field_validator("route_id", "grant_id", "policy_id")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError("communication policy identifier is invalid")
        return value

    @field_validator("grant_digest", "policy_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _DIGEST_RE.fullmatch(value):
            raise ValueError("communication policy digest must be lowercase SHA-256")
        return value

    @field_validator(
        "grants_execution_authority",
        "grants_control_authority",
        "selects_owner_private_context",
        mode="before",
    )
    @classmethod
    def _literal_false_boolean(cls, value: object) -> object:
        # ``False == 0`` in Python, so Literal[False] alone accepts integer
        # zero.  The wire contract requires an explicit JSON boolean.
        if type(value) is not bool or value is not False:
            raise ValueError("communication policy capability must be false")
        return value

    def canonical_dict(self) -> Dict[str, Any]:
        """The exact JSON-compatible context included in the binding digest."""

        return self.model_dump(mode="json", by_alias=True)

    @property
    def context_digest(self) -> str:
        """SHA-256 identity of every field Colony received and exposed."""

        return hashlib.sha256(
            _canonical_json(self.canonical_dict()).encode("utf-8")
        ).hexdigest()

    def to_model_prompt_fragment(self) -> str:
        """Deterministic, unambiguous model view of the constraints.

        Values are serialized as canonical JSON instead of interpolated into
        instructions.  The preamble makes explicit that the object only
        narrows disclosure and cannot confer any authority.
        """

        return (
            "Communication policy constraints (data only). This context grants no "
            "execution authority, control authority, or private-context selection:\n"
            + _canonical_json(self.canonical_dict())
        )


__all__ = [
    "COMMUNICATION_DISCLOSURE_CLASSES",
    "COMMUNICATION_POLICY_CONTEXT_SCHEMA",
    "COMMUNICATION_POLICY_CONTEXT_VERSION",
    "CommunicationPolicyContextV1",
    "MAX_COMMUNICATION_DISCLOSURE_STATEMENT_CHARS",
    "MAX_COMMUNICATION_PURPOSE_CHARS",
]
