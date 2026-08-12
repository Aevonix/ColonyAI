"""Single source of truth for the governed Colony tool catalog.

Each governed tool carries exactly four things: its wire name, its strict
argument validator, its owner-facing approval-display metadata, and whether a
standing bounded grant may ever authorize it (``non_grantable``).

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

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .contract import (
    GovernedContractError,
    RESEARCH_TOPIC_MAX_CHARS,
    bounded_integer,
    bounded_json_value,
    bounded_number,
    bounded_text,
    enum_text,
    exact_mapping,
)


class ToolCatalogError(GovernedContractError):
    """The tool is not part of the governed catalog."""


def _preview(value: Any, maximum: int = 300) -> str:
    text = " ".join(str(value).split())
    if len(text) > maximum:
        text = text[: maximum - 1] + "…"
    return json.dumps(text, ensure_ascii=True)


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
    bounded_text(args["description"], "description", 8000)
    if "due_at" in args:
        bounded_text(args["due_at"], "due_at", 256, allow_empty=True)
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
    bounded_text(args["initiative_id"], "initiative_id", 256, identifier=True)
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
    bounded_text(args["content"], "content", 16000)
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
    bounded_text(args["commitment_id"], "commitment_id", 256, identifier=True)
    if "outcome" in args:
        enum_text(
            args["outcome"],
            "outcome",
            frozenset({"done", "invalid", "duplicate", "wont_do", "obsolete"}),
        )
    if "reason" in args:
        bounded_text(args["reason"], "reason", 8000, allow_empty=True)
    return args


def _args_task_complete(raw: Any) -> dict[str, Any]:
    args = exact_mapping(raw, "args", allowed={"task_id"}, required={"task_id"})
    bounded_text(args["task_id"], "task_id", 256, identifier=True)
    return args


def _args_task_dismiss(raw: Any) -> dict[str, Any]:
    args = exact_mapping(
        raw, "args", allowed={"reason", "task_id"}, required={"task_id"},
    )
    bounded_text(args["task_id"], "task_id", 256, identifier=True)
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
    bounded_text(args["task_id"], "task_id", 256, identifier=True)
    if "hours" in args:
        bounded_integer(args["hours"], "hours", 1, 168)
    if "reason" in args:
        bounded_text(args["reason"], "reason", 8000, allow_empty=True)
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
    """One governed tool: name, strict validator, display, grant posture."""

    name: str
    validate_args: Callable[[Any], dict[str, Any]]
    approval_display: Callable[[Mapping[str, Any]], dict[str, str]]
    # True → this tool must always require a per-message owner approval and
    # can never be authorized by a standing bounded grant (owner decision).
    non_grantable: bool = False


TOOL_CATALOG: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            name="colony_autonomy_disable",
            validate_args=_args_autonomy,
            approval_display=_display_autonomy_disable,
            non_grantable=True,
        ),
        ToolSpec(
            name="colony_autonomy_enable",
            validate_args=_args_autonomy,
            approval_display=_display_autonomy_enable,
            non_grantable=True,
        ),
        ToolSpec(
            name="colony_create_commitment",
            validate_args=_args_create_commitment,
            approval_display=_display_create_commitment,
        ),
        ToolSpec(
            name="colony_initiative_feedback",
            validate_args=_args_initiative_feedback,
            approval_display=_display_initiative_feedback,
        ),
        ToolSpec(
            name="colony_record_insight",
            validate_args=_args_record_insight,
            approval_display=_display_record_insight,
        ),
        ToolSpec(
            name="colony_research",
            validate_args=_args_research,
            approval_display=_display_research,
        ),
        ToolSpec(
            name="colony_resolve_commitment",
            validate_args=_args_resolve_commitment,
            approval_display=_display_resolve_commitment,
        ),
        ToolSpec(
            name="colony_task_complete",
            validate_args=_args_task_complete,
            approval_display=_display_task_complete,
        ),
        ToolSpec(
            name="colony_task_dismiss",
            validate_args=_args_task_dismiss,
            approval_display=_display_task_dismiss,
        ),
        ToolSpec(
            name="colony_task_snooze",
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


def validate_tool_args(tool_name: Any, raw: Any) -> dict[str, Any]:
    """Validate one tool call against the catalog's exact bounded contract."""

    spec = TOOL_CATALOG.get(tool_name) if isinstance(tool_name, str) else None
    if spec is None:
        raise ToolCatalogError("tool is not a governed Colony action")
    return spec.validate_args(raw)


__all__ = (
    "ACTION_TOOL_NAMES",
    "GRANT_AUTHORIZABLE_TOOL_NAMES",
    "NON_GRANTABLE_TOOL_NAMES",
    "TOOL_CATALOG",
    "ToolCatalogError",
    "ToolSpec",
    "validate_tool_args",
)
