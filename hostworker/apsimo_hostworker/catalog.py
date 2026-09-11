"""Single source of truth for the governed Colony tool catalog.

Each governed tool carries exactly five things: its wire name, its model-visible
argument schema, its strict argument validator, its owner-facing approval-display
metadata, and whether a standing bounded grant may ever authorize it
(``non_grantable``).

``colony_autonomy_enable`` and ``colony_autonomy_disable`` are marked
``non_grantable`` by owner decision: autonomy posture must always be a
per-message owner approval and can never ride on a standing grant, however the
grant was issued.  The gate core (:mod:`colony_hostworker.gate`) fails closed
on the grant path for any non-grantable — or unknown — tool regardless of host
configuration.

The validators here are behavior-identical ports of the two existing
independent validators (ColonyAI's endpoint ``_validate_args`` and the private
worker's intent ``_validate_args``); the repo-internal agreement test pins
that equivalence against ColonyAI's endpoint, which deliberately keeps its own
copy (see the design rule in :mod:`colony_hostworker.contract`).
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .contract import (
    BOUNDED_JSON_INTEGER_MAX,
    BOUNDED_JSON_KEY_MAX_CHARS,
    BOUNDED_JSON_MAX_DEPTH,
    BOUNDED_JSON_MAX_NODES,
    BOUNDED_JSON_STRING_MAX_CHARS,
    GovernedContractError,
    IDENTIFIER_MAX_CHARS,
    IDENTIFIER_RE,
    RESEARCH_TOPIC_MAX_CHARS,
    bounded_integer,
    bounded_json_value,
    bounded_number,
    bounded_text,
    enum_text,
    exact_mapping,
)


COMMITMENT_PRIORITY_DEFAULT = 60
COMMITMENT_DESCRIPTION_MAX_CHARS = 8000
COMMITMENT_DUE_AT_MAX_CHARS = 256
INSIGHT_CONTENT_MAX_CHARS = 16000
FREEFORM_REASON_MAX_CHARS = 8000

# ``bounded_text`` rejects NUL and lone UTF-16 surrogates for every text field,
# and rejects blank text unless ``allow_empty=True``.  These zero-width,
# ECMAScript-compatible patterns project the same rules into JSON Schema while
# still permitting ordinary newlines.  The explicit class is Python's
# ``str.strip`` whitespace set; ECMAScript's shorter ``\s`` set differs.
_MODEL_TEXT_PATTERN = r"^(?![\s\S]*[\u0000\uD800-\uDFFF])"
_MODEL_NONBLANK_TEXT_PATTERN = (
    _MODEL_TEXT_PATTERN
    + r"(?=[\s\S]*[^\u0009-\u000D\u001C-\u0020\u0085\u00A0"
    r"\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000])"
)


class ToolCatalogError(GovernedContractError):
    """The tool is not part of the governed catalog."""


def _preview(value: Any, maximum: int = 300) -> str:
    text = " ".join(str(value).split())
    if len(text) > maximum:
        text = text[: maximum - 1] + "…"
    return json.dumps(text, ensure_ascii=True)


def _parameters(
    properties: Mapping[str, Any],
    required=(),
    *,
    definitions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }
    if definitions:
        schema["$defs"] = dict(definitions)
    return schema


def _text_model_schema(
    maximum: int, *, allow_empty: bool = False,
) -> dict[str, Any]:
    schema = {
        "type": "string",
        "maxLength": maximum,
        "pattern": (
            _MODEL_TEXT_PATTERN if allow_empty else _MODEL_NONBLANK_TEXT_PATTERN
        ),
    }
    if not allow_empty:
        schema["minLength"] = 1
    return schema


def identifier_model_schema() -> dict[str, Any]:
    """Return the exact model-visible governed identifier grammar."""

    return {
        "type": "string",
        "description": (
            "Canonical identifier: starts with an ASCII letter or digit; "
            "remaining characters are ASCII letters, digits, '.', '_', ':', "
            "'/', or '-'."
        ),
        "maxLength": IDENTIFIER_MAX_CHARS,
        "pattern": IDENTIFIER_RE.pattern,
    }


def _details_definitions() -> dict[str, Any]:
    """Build a finite JSON Schema projection of the bounded details tree.

    Standard JSON Schema has no aggregate descendant-node counter.  The exact
    whole-tree budget is therefore stated in the portable field description
    and enforced by both strict runtime validators.  Per-container limits here
    enforce every locally possible overflow without rejecting valid branched
    values merely to approximate that aggregate budget.
    """

    definitions: dict[str, Any] = {}
    key_schema = {
        "type": "string",
        "maxLength": BOUNDED_JSON_KEY_MAX_CHARS,
        "pattern": IDENTIFIER_RE.pattern,
    }
    scalar = [
        {"type": "null"},
        {"type": "boolean"},
        # JSON Schema's mathematical number model cannot distinguish a Python
        # integer from an integral float.  Bounding the numeric branch is the
        # portable safe subset: every advertised number passes both validators.
        {
            "type": "number",
            "minimum": -BOUNDED_JSON_INTEGER_MAX,
            "maximum": BOUNDED_JSON_INTEGER_MAX,
        },
        _text_model_schema(
            BOUNDED_JSON_STRING_MAX_CHARS, allow_empty=True,
        ),
    ]
    for depth in range(BOUNDED_JSON_MAX_DEPTH, 0, -1):
        if depth == BOUNDED_JSON_MAX_DEPTH:
            containers = [
                {"type": "array", "maxItems": 0},
                {
                    "type": "object",
                    "propertyNames": key_schema,
                    "maxProperties": 0,
                    "additionalProperties": False,
                },
            ]
        else:
            child = {"$ref": f"#/$defs/detailsValue{depth + 1}"}
            # The root and this container's ancestors have already consumed
            # ``depth + 1`` nodes.  This is the largest local collection that
            # can still fit the runtime's whole-tree budget.
            container_maximum = BOUNDED_JSON_MAX_NODES - depth - 1
            containers = [
                {
                    "type": "array",
                    "maxItems": container_maximum,
                    "items": child,
                },
                {
                    "type": "object",
                    "propertyNames": key_schema,
                    "maxProperties": container_maximum,
                    "additionalProperties": child,
                },
            ]
        definitions[f"detailsValue{depth}"] = {"anyOf": scalar + containers}
    return definitions


_DETAILS_DEFINITIONS = _details_definitions()
_DETAILS_DESCRIPTION = (
    f"Bounded JSON object: at most {BOUNDED_JSON_MAX_NODES} total values "
    f"including this object; maximum nesting depth {BOUNDED_JSON_MAX_DEPTH}; "
    f"strings at most {BOUNDED_JSON_STRING_MAX_CHARS} characters; object keys "
    f"at most {BOUNDED_JSON_KEY_MAX_CHARS} characters, starting with an ASCII "
    "letter or digit and then using only ASCII letters, digits, '.', '_', ':', "
    "'/', or '-'. Numeric values must be finite and within the signed 63-bit "
    "range."
)


def details_model_schema() -> dict[str, Any]:
    """Return the portable advertised bounded-details root schema."""

    return {
        "type": "object",
        "description": _DETAILS_DESCRIPTION,
        "propertyNames": {
            "type": "string",
            "maxLength": BOUNDED_JSON_KEY_MAX_CHARS,
            "pattern": IDENTIFIER_RE.pattern,
        },
        "maxProperties": BOUNDED_JSON_MAX_NODES - 1,
        "additionalProperties": {"$ref": "#/$defs/detailsValue1"},
    }


# ---------------------------------------------------------------------------
# Argument validators (one per tool, exact bounded contracts)
# ---------------------------------------------------------------------------


def _args_autonomy(raw: Any) -> dict[str, Any]:
    return exact_mapping(raw, "args", allowed=frozenset())


def _args_create_commitment(raw: Any) -> dict[str, Any]:
    args = exact_mapping(
        raw,
        "args",
        allowed={"description", "due_at", "priority"},
        required={"description"},
    )
    bounded_text(
        args["description"], "description", COMMITMENT_DESCRIPTION_MAX_CHARS,
    )
    if "due_at" in args:
        bounded_text(
            args["due_at"], "due_at", COMMITMENT_DUE_AT_MAX_CHARS,
            allow_empty=True,
        )
    if "priority" in args:
        bounded_integer(args["priority"], "priority", 0, 100)
    return args


def _args_initiative_feedback(raw: Any) -> dict[str, Any]:
    args = exact_mapping(
        raw,
        "args",
        allowed={"initiative_id", "action", "details"},
        required={"initiative_id", "action"},
    )
    bounded_text(
        args["initiative_id"], "initiative_id", IDENTIFIER_MAX_CHARS,
        identifier=True,
    )
    enum_text(
        args["action"],
        "action",
        frozenset({"acknowledged", "actioned", "dismissed", "snoozed"}),
    )
    if "details" in args:
        if not isinstance(args["details"], Mapping):
            raise GovernedContractError("details must be an object")
        args["details"] = bounded_json_value(args["details"], "details")
    return args


def _args_record_insight(raw: Any) -> dict[str, Any]:
    args = exact_mapping(
        raw,
        "args",
        allowed={"confidence", "content", "insight_type"},
        required={"content", "insight_type"},
    )
    bounded_text(args["content"], "content", INSIGHT_CONTENT_MAX_CHARS)
    enum_text(
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
        bounded_number(args["confidence"], "confidence", 0.0, 1.0)
    return args


def _args_research(raw: Any) -> dict[str, Any]:
    args = exact_mapping(
        raw, "args", allowed={"depth", "topic"}, required={"topic"},
    )
    bounded_text(args["topic"], "topic", RESEARCH_TOPIC_MAX_CHARS)
    if "depth" in args:
        enum_text(args["depth"], "depth", frozenset({"quick", "standard", "deep"}))
    return args


def _args_resolve_commitment(raw: Any) -> dict[str, Any]:
    args = exact_mapping(
        raw,
        "args",
        allowed={"commitment_id", "outcome", "reason"},
        required={"commitment_id"},
    )
    bounded_text(
        args["commitment_id"], "commitment_id", IDENTIFIER_MAX_CHARS,
        identifier=True,
    )
    if "outcome" in args:
        enum_text(
            args["outcome"],
            "outcome",
            frozenset({"done", "invalid", "duplicate", "wont_do", "obsolete"}),
        )
    if "reason" in args:
        bounded_text(
            args["reason"], "reason", FREEFORM_REASON_MAX_CHARS,
            allow_empty=True,
        )
    return args


def _args_task_complete(raw: Any) -> dict[str, Any]:
    args = exact_mapping(raw, "args", allowed={"task_id"}, required={"task_id"})
    bounded_text(
        args["task_id"], "task_id", IDENTIFIER_MAX_CHARS, identifier=True,
    )
    return args


def _args_task_dismiss(raw: Any) -> dict[str, Any]:
    args = exact_mapping(
        raw, "args", allowed={"reason", "task_id"}, required={"task_id"},
    )
    bounded_text(
        args["task_id"], "task_id", IDENTIFIER_MAX_CHARS, identifier=True,
    )
    if "reason" in args:
        enum_text(
            args["reason"],
            "reason",
            frozenset({"stale", "completed", "abandoned", "not_applicable"}),
        )
    return args


def _args_task_snooze(raw: Any) -> dict[str, Any]:
    args = exact_mapping(
        raw,
        "args",
        allowed={"hours", "reason", "task_id"},
        required={"task_id"},
    )
    bounded_text(
        args["task_id"], "task_id", IDENTIFIER_MAX_CHARS, identifier=True,
    )
    if "hours" in args:
        bounded_integer(args["hours"], "hours", 1, 168)
    if "reason" in args:
        bounded_text(
            args["reason"], "reason", FREEFORM_REASON_MAX_CHARS,
            allow_empty=True,
        )
    return args


# ---------------------------------------------------------------------------
# Approval-display metadata (owner-facing summary/target/risk)
# ---------------------------------------------------------------------------


def _display_autonomy_disable(_args: Mapping[str, Any]) -> dict[str, str]:
    return {
        "summary": "Disable Colony autonomous scheduling",
        "target": "Colony autonomy scheduler",
        "risk": "Autonomous work will stop until it is explicitly enabled again",
    }


def _display_autonomy_enable(_args: Mapping[str, Any]) -> dict[str, str]:
    return {
        "summary": "Enable Colony autonomous scheduling",
        "target": "Colony autonomy scheduler",
        "risk": (
            "Colony may begin bounded autonomous work under its configured policies"
        ),
    }


def _display_create_commitment(args: Mapping[str, Any]) -> dict[str, str]:
    return {
        "summary": "Create Colony commitment %s" % _preview(args["description"]),
        "target": "Private Colony commitment ledger",
        "risk": (
            "Adds one durable personal commitment; "
            "no external communication is sent"
        ),
    }


def _display_initiative_feedback(args: Mapping[str, Any]) -> dict[str, str]:
    return {
        "summary": "Mark initiative %s as %s"
        % (_preview(args["initiative_id"]), args["action"]),
        "target": "Private Colony initiative ledger",
        "risk": "Changes Colony's internal initiative state and learning evidence",
    }


def _display_record_insight(args: Mapping[str, Any]) -> dict[str, str]:
    return {
        "summary": "Record %s insight %s"
        % (args["insight_type"], _preview(args["content"])),
        "target": "Private Colony memory and learning store",
        "risk": (
            "Persists model-derived personal context that can influence "
            "future reasoning"
        ),
    }


def _display_research(args: Mapping[str, Any]) -> dict[str, str]:
    return {
        "summary": "Start %s Colony research on %s"
        % (args.get("depth", "quick"), _preview(args["topic"])),
        "target": "Colony research queue",
        "risk": (
            "May consume configured research resources; "
            "external publishing is not authorized"
        ),
    }


def _display_resolve_commitment(args: Mapping[str, Any]) -> dict[str, str]:
    return {
        "summary": "Resolve commitment %s as %s"
        % (_preview(args["commitment_id"]), args.get("outcome", "done")),
        "target": "Private Colony commitment ledger",
        "risk": "Changes one durable commitment's terminal state",
    }


def _display_task_complete(args: Mapping[str, Any]) -> dict[str, str]:
    return {
        "summary": "Complete Colony task %s" % _preview(args["task_id"]),
        "target": "Private Colony task or initiative ledger",
        "risk": "Marks one internal task or initiative complete",
    }


def _display_task_dismiss(args: Mapping[str, Any]) -> dict[str, str]:
    return {
        "summary": "Dismiss Colony task %s as %s"
        % (_preview(args["task_id"]), args.get("reason", "stale")),
        "target": "Private Colony task or initiative ledger",
        "risk": "Dismisses one internal task or initiative from active work",
    }


def _display_task_snooze(args: Mapping[str, Any]) -> dict[str, str]:
    return {
        "summary": "Snooze Colony task %s for %d hours"
        % (_preview(args["task_id"]), args.get("hours", 24)),
        "target": "Private Colony task or initiative ledger",
        "risk": (
            "Defers one internal task or initiative until the bounded "
            "snooze expires"
        ),
    }


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One governed tool and its complete model/validation contract."""

    name: str
    model_description: str
    model_parameters: Mapping[str, Any]
    validate_args: Callable[[Any], dict[str, Any]]
    approval_display: Callable[[Mapping[str, Any]], dict[str, str]]
    # True → this tool must always require a per-message owner approval and
    # can never be authorized by a standing bounded grant (owner decision).
    non_grantable: bool = False

    @property
    def model_schema(self) -> dict[str, Any]:
        """Return an isolated copy safe for a model-facing plugin catalog."""

        return {
            "name": self.name,
            "description": self.model_description,
            "parameters": copy.deepcopy(self.model_parameters),
        }


TOOL_CATALOG: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            name="colony_autonomy_disable",
            model_description=(
                "Submit a governed intent to disable autonomous work scheduling."
            ),
            model_parameters=_parameters({}),
            validate_args=_args_autonomy,
            approval_display=_display_autonomy_disable,
            non_grantable=True,
        ),
        ToolSpec(
            name="colony_autonomy_enable",
            model_description=(
                "Submit a governed intent to enable autonomous work scheduling."
            ),
            model_parameters=_parameters({}),
            validate_args=_args_autonomy,
            approval_display=_display_autonomy_enable,
            non_grantable=True,
        ),
        ToolSpec(
            name="colony_create_commitment",
            model_description=(
                "Submit a governed intent to create a commitment for this "
                "participant."
            ),
            model_parameters=_parameters(
                {
                    "description": _text_model_schema(
                        COMMITMENT_DESCRIPTION_MAX_CHARS,
                    ),
                    "due_at": _text_model_schema(
                        COMMITMENT_DUE_AT_MAX_CHARS, allow_empty=True,
                    ),
                    "priority": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "default": COMMITMENT_PRIORITY_DEFAULT,
                    },
                },
                ("description",),
            ),
            validate_args=_args_create_commitment,
            approval_display=_display_create_commitment,
        ),
        ToolSpec(
            name="colony_initiative_feedback",
            model_description=(
                "Submit a governed intent describing an initiative outcome."
            ),
            model_parameters=_parameters(
                {
                    "action": {
                        "type": "string",
                        "enum": [
                            "acknowledged", "actioned", "dismissed", "snoozed",
                        ],
                    },
                    "details": details_model_schema(),
                    "initiative_id": identifier_model_schema(),
                },
                ("initiative_id", "action"),
                definitions=_DETAILS_DEFINITIONS,
            ),
            validate_args=_args_initiative_feedback,
            approval_display=_display_initiative_feedback,
        ),
        ToolSpec(
            name="colony_record_insight",
            model_description=(
                "Submit a governed intent to record a conversational insight."
            ),
            model_parameters=_parameters(
                {
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "default": 0.7,
                    },
                    "content": _text_model_schema(INSIGHT_CONTENT_MAX_CHARS),
                    "insight_type": {
                        "type": "string",
                        "enum": [
                            "preference", "connection", "fact", "goal_hint",
                            "relationship_update",
                        ],
                    },
                },
                ("insight_type", "content"),
            ),
            validate_args=_args_record_insight,
            approval_display=_display_record_insight,
        ),
        ToolSpec(
            name="colony_research",
            model_description=(
                "Submit a governed intent to queue durable Colony research."
            ),
            model_parameters=_parameters(
                {
                    "depth": {
                        "type": "string",
                        "enum": ["quick", "standard", "deep"],
                        "default": "quick",
                    },
                    "topic": _text_model_schema(RESEARCH_TOPIC_MAX_CHARS),
                },
                ("topic",),
            ),
            validate_args=_args_research,
            approval_display=_display_research,
        ),
        ToolSpec(
            name="colony_resolve_commitment",
            model_description=(
                "Submit a governed intent to resolve a commitment."
            ),
            model_parameters=_parameters(
                {
                    "commitment_id": identifier_model_schema(),
                    "outcome": {
                        "type": "string",
                        "enum": [
                            "done", "invalid", "duplicate", "wont_do", "obsolete",
                        ],
                        "default": "done",
                    },
                    "reason": _text_model_schema(
                        FREEFORM_REASON_MAX_CHARS, allow_empty=True,
                    ),
                },
                ("commitment_id",),
            ),
            validate_args=_args_resolve_commitment,
            approval_display=_display_resolve_commitment,
        ),
        ToolSpec(
            name="colony_task_complete",
            model_description=(
                "Submit a governed intent to complete a task or initiative."
            ),
            model_parameters=_parameters(
                {"task_id": identifier_model_schema()}, ("task_id",),
            ),
            validate_args=_args_task_complete,
            approval_display=_display_task_complete,
        ),
        ToolSpec(
            name="colony_task_dismiss",
            model_description=(
                "Submit a governed intent to dismiss a task or initiative."
            ),
            model_parameters=_parameters(
                {
                    "reason": {
                        "type": "string",
                        "enum": [
                            "stale", "completed", "abandoned", "not_applicable",
                        ],
                        "default": "stale",
                    },
                    "task_id": identifier_model_schema(),
                },
                ("task_id",),
            ),
            validate_args=_args_task_dismiss,
            approval_display=_display_task_dismiss,
        ),
        ToolSpec(
            name="colony_task_snooze",
            model_description=(
                "Submit a governed intent to snooze a task or initiative."
            ),
            model_parameters=_parameters(
                {
                    "hours": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 168,
                        "default": 24,
                    },
                    "reason": {
                        **_text_model_schema(
                            FREEFORM_REASON_MAX_CHARS, allow_empty=True,
                        ),
                        "default": "",
                    },
                    "task_id": identifier_model_schema(),
                },
                ("task_id",),
            ),
            validate_args=_args_task_snooze,
            approval_display=_display_task_snooze,
        ),
    )
}

ACTION_TOOL_NAMES = frozenset(TOOL_CATALOG)
NON_GRANTABLE_TOOL_NAMES = frozenset(
    name for name, spec in TOOL_CATALOG.items() if spec.non_grantable
)
GRANT_AUTHORIZABLE_TOOL_NAMES = ACTION_TOOL_NAMES - NON_GRANTABLE_TOOL_NAMES
ACTION_MODEL_TOOL_SCHEMAS = tuple(
    TOOL_CATALOG[name].model_schema for name in sorted(ACTION_TOOL_NAMES)
)


def validate_tool_args(tool_name: Any, raw: Any) -> dict[str, Any]:
    """Validate one tool call against the catalog's exact bounded contract."""

    spec = TOOL_CATALOG.get(tool_name) if isinstance(tool_name, str) else None
    if spec is None:
        raise ToolCatalogError("tool is not a governed Colony action")
    return spec.validate_args(raw)


__all__ = (
    "ACTION_MODEL_TOOL_SCHEMAS",
    "ACTION_TOOL_NAMES",
    "BOUNDED_JSON_INTEGER_MAX",
    "BOUNDED_JSON_KEY_MAX_CHARS",
    "BOUNDED_JSON_MAX_DEPTH",
    "BOUNDED_JSON_MAX_NODES",
    "BOUNDED_JSON_STRING_MAX_CHARS",
    "COMMITMENT_DESCRIPTION_MAX_CHARS",
    "COMMITMENT_DUE_AT_MAX_CHARS",
    "COMMITMENT_PRIORITY_DEFAULT",
    "FREEFORM_REASON_MAX_CHARS",
    "GRANT_AUTHORIZABLE_TOOL_NAMES",
    "IDENTIFIER_MAX_CHARS",
    "IDENTIFIER_RE",
    "INSIGHT_CONTENT_MAX_CHARS",
    "NON_GRANTABLE_TOOL_NAMES",
    "RESEARCH_TOPIC_MAX_CHARS",
    "TOOL_CATALOG",
    "ToolCatalogError",
    "ToolSpec",
    "details_model_schema",
    "identifier_model_schema",
    "validate_tool_args",
)
