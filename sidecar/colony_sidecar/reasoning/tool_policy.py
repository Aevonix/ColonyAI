"""Typed authorization policy for sidecar-resident reasoning tools.

Model tool definitions are an ergonomics hint, not an enforcement boundary: a
model (or a direct API caller) can still name a tool that was not advertised.
This module gives every execution path the same small, conservative effect
classification and actor policy.  Unknown and dynamically supplied tools are
mutating until explicitly classified.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class ToolEffect(str, Enum):
    """The authority needed to execute a tool."""

    PUBLIC_READ = "public_read"
    PRIVATE_READ = "private_read"
    MUTATION = "mutation"


@dataclass(frozen=True)
class ToolActorPolicy:
    """Server-derived capabilities for one reasoning turn or direct call.

    ``None`` remains the compatibility representation for trusted in-process
    callers. HTTP handlers must always supply a concrete policy while P8 is
    attached; request-body identities never populate these fields.
    """

    principal_id: str
    viewer_person_id: str
    allow_private_read: bool = False
    allow_mutation: bool = False


# Pure/general information tools that do not expose Colony's private stores.
_PUBLIC_READ_TOOLS = frozenset({
    "calculate",
    "web_search",
})


# Tools that can expose owner, contact, repository, memory, or self-state.
# They are read-only, but are intentionally not guest-callable because most
# legacy handlers do not yet seal selectors in their argument dictionaries.
_PRIVATE_READ_TOOLS = frozenset({
    "action_journal",
    "belief_conflicts",
    "colony_discover_connections",
    "colony_get_briefing",
    "colony_get_relationship",
    "colony_list_boundaries",
    "colony_list_goals",
    "colony_memory_search",
    "colony_query_entities",
    "colony_recent_boundary_blocks",
    "list_directory",
    "list_projects",
    "pending_contact_proposals",
    "project_status",
    "read_file",
    "recall_skills",
    "relationship_brief",
    "repo_list_files",
    "repo_read_file",
    "repo_search",
    "sandbox_status",
    "self_status",
})


# Known state-changing tools. Unknown/dynamic tools also default to mutation.
_MUTATION_TOOLS = frozenset({
    "abandon_project",
    "colony_flag_boundary_concern",
    "colony_initiative_feedback",
    "colony_record_insight",
    "colony_start_research",
    "colony_task_complete",
    "colony_task_dismiss",
    "colony_task_snooze",
    "create_project",
    "link_contact",
    "merge_contacts",
    "sandbox_run",
    "write_file",
})


def classify_tool(name: str) -> ToolEffect:
    """Return the tool's conservative effect class.

    The explicit mutation set documents current first-party tools.  The final
    branch is deliberately mutation: newly graduated/toolsmith tools cannot
    silently inherit read authority merely because they are new.
    """

    normalized = str(name or "").strip()
    if normalized in _PUBLIC_READ_TOOLS:
        return ToolEffect.PUBLIC_READ
    if normalized in _PRIVATE_READ_TOOLS:
        return ToolEffect.PRIVATE_READ
    if normalized in _MUTATION_TOOLS:
        return ToolEffect.MUTATION
    return ToolEffect.MUTATION


def actor_allows_tool(
    name: str,
    actor: ToolActorPolicy | None,
) -> bool:
    """Whether actor capabilities permit this tool before boundary checks."""

    return actor_allows_effect(classify_tool(name), actor)


def actor_allows_effect(
    effect: ToolEffect,
    actor: ToolActorPolicy | None,
) -> bool:
    """Whether actor capabilities permit an already-resolved tool effect.

    Callers with handler provenance must use this form. In particular, every
    dynamic handler resolves to ``MUTATION`` even if its chosen name resembles
    a shipped public or private read.
    """

    if actor is None:  # trusted in-process compatibility caller
        return True
    if effect is ToolEffect.PUBLIC_READ:
        return True
    if effect is ToolEffect.PRIVATE_READ:
        return actor.allow_private_read
    return actor.allow_mutation


def filter_tools_for_actor(
    names: list[str] | tuple[str, ...] | set[str] | frozenset[str],
    actor: ToolActorPolicy | None,
) -> list[str]:
    """Preserve caller order while removing actor-inaccessible tool names."""

    return [name for name in names if actor_allows_tool(name, actor)]


def action_target(arguments: Mapping[str, Any]) -> str:
    """Extract a useful human/resource subject for DirectiveGuard matching."""

    for key in (
        "target",
        "target_path",
        "path",
        "repo",
        "url",
        "endpoint",
        "who",
        "contact_id",
        "person_id",
        "entity_id",
        "topic",
        "query",
        "purpose",
    ):
        value = arguments.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ""


__all__ = [
    "ToolActorPolicy",
    "ToolEffect",
    "action_target",
    "actor_allows_effect",
    "actor_allows_tool",
    "classify_tool",
    "filter_tools_for_actor",
]
