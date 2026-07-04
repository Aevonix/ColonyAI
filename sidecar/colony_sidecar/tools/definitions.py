"""Colony-native tool definitions for server-side execution.

These tools are advertised to the LLM via the ReasoningLoop and executed
by the ToolExecutor. They provide direct access to Colony's intelligence
systems without going through the host plugin.

Each tool definition follows the OpenAI function-calling format.
"""

from __future__ import annotations

from typing import Any

# Core Colony tools — memory, relationships, goals
COLONY_CORE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "colony_memory_search",
        "description": (
            "Search Colony's memory graph for relevant context about a person, topic, or past conversation. "
            "Returns ranked memories with timestamps and relevance scores."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query — can be a topic, person name, or question",
                },
                "person_id": {
                    "type": "string",
                    "description": "Optional person ID to scope the search to conversations with that person",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default: 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "colony_get_relationship",
        "description": (
            "Get the relationship score and trust tier for a contact. "
            "Returns: score (0-100), tier (stranger/acquaintance/friend/close/confidant), "
            "and interaction history summary."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contact_id": {
                    "type": "string",
                    "description": "The contact's ID (usually their session key or phone/email)",
                },
            },
            "required": ["contact_id"],
        },
    },
    {
        "name": "colony_list_goals",
        "description": (
            "List the user's goals with their status and progress. "
            "Can filter by status (active/completed/blocked) or person."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "person_id": {
                    "type": "string",
                    "description": "Optional person ID to filter goals related to that person",
                },
                "status": {
                    "type": "string",
                    "enum": ["active", "completed", "blocked", "all"],
                    "description": "Filter by goal status (default: active)",
                    "default": "active",
                },
            },
            "required": [],
        },
    },
    {
        "name": "colony_get_briefing",
        "description": (
            "Get a briefing for a person — summary of relationship, recent topics, "
            "goals, and suggested conversation starters."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contact_id": {
                    "type": "string",
                    "description": "The contact's ID",
                },
            },
            "required": ["contact_id"],
        },
    },
    {
        "name": "colony_record_insight",
        "description": (
            "Record an insight discovered during conversation — a connection, "
            "preference, or important fact worth remembering."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "insight_type": {
                    "type": "string",
                    "enum": ["preference", "connection", "fact", "goal_hint", "relationship_update"],
                    "description": "The type of insight",
                },
                "content": {
                    "type": "string",
                    "description": "The insight content",
                },
                "confidence": {
                    "type": "number",
                    "description": "Confidence level 0-1 (default: 0.7)",
                    "default": 0.7,
                },
                "person_id": {
                    "type": "string",
                    "description": "Optional person this insight relates to",
                },
            },
            "required": ["insight_type", "content"],
        },
    },
]

# Extended tools — world model, research, synthesis
COLONY_EXTENDED_TOOLS: list[dict[str, Any]] = [
    {
        "name": "colony_query_entities",
        "description": (
            "Query Colony's world model for entities (people, places, organizations, concepts). "
            "Returns matching entities with their relationships and attributes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query for entities",
                },
                "entity_type": {
                    "type": "string",
                    "enum": ["person", "place", "organization", "concept", "all"],
                    "description": "Filter by entity type (default: all)",
                    "default": "all",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results (default: 10)",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "colony_start_research",
        "description": (
            "Start a background research task on a topic. "
            "Research runs asynchronously and results are available via the research endpoint."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "The topic to research",
                },
                "depth": {
                    "type": "string",
                    "enum": ["quick", "standard", "deep"],
                    "description": "Research depth (default: standard)",
                    "default": "standard",
                },
            },
            "required": ["topic"],
        },
    },
    {
        "name": "colony_discover_connections",
        "description": (
            "Discover non-obvious connections between entities, topics, or people. "
            "Returns novelty-scored connections that might be interesting."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "Optional entity to find connections for",
                },
                "min_novelty": {
                    "type": "number",
                    "description": "Minimum novelty score 0-1 (default: 0.3)",
                    "default": 0.3,
                },
            },
            "required": [],
        },
    },
    # --- Task Management Tools (v0.7.10) ---
    {
        "name": "colony_task_complete",
        "description": (
            "Mark a task as completed. Use when you determine a task mentioned in an initiative is done."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task/goal identifier from the initiative context",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "colony_task_snooze",
        "description": (
            "Snooze a task - don't generate initiatives for it for N hours. "
            "Use when a task is valid but not actionable right now. Max 168 hours (1 week)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task/goal identifier",
                },
                "hours": {
                    "type": "integer",
                    "description": "Hours to snooze (1-168, default 24)",
                    "default": 24,
                },
                "reason": {
                    "type": "string",
                    "description": "Optional reason for snooze",
                    "default": "",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "colony_task_dismiss",
        "description": (
            "Dismiss a task as no longer relevant. Use when the task is stale, abandoned, or no longer needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task/goal identifier",
                },
                "reason": {
                    "type": "string",
                    "enum": ["stale", "completed", "abandoned", "not_applicable"],
                    "description": "Why the task is being dismissed (default: stale)",
                    "default": "stale",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "colony_initiative_feedback",
        "description": (
            "Provide feedback on how an initiative was handled. "
            "Action: acknowledged, actioned, dismissed, or snoozed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "initiative_id": {
                    "type": "string",
                    "description": "The initiative ID from the system message",
                },
                "action": {
                    "type": "string",
                    "enum": ["acknowledged", "actioned", "dismissed", "snoozed"],
                    "description": "How the initiative was handled",
                },
                "details": {
                    "type": "object",
                    "description": "Optional additional context",
                },
            },
            "required": ["initiative_id", "action"],
        },
    },
    {
        "name": "colony_list_boundaries",
        "description": (
            "List the owner's active standing directives / boundaries (things "
            "you must not do or must always do). Use when asked what your "
            "boundaries or standing instructions are."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "colony_recent_boundary_blocks",
        "description": (
            "List autonomous actions you recently refused and which boundary "
            "refused each. Use to explain WHY you did not do something (cite the "
            "directive and date)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max entries (default 10)", "default": 10},
            },
            "required": [],
        },
    },
    {
        "name": "colony_flag_boundary_concern",
        "description": (
            "Surface a CRITICAL finding (security vulnerability, data loss, "
            "financial risk) about a subject the owner told you to leave alone. "
            "Delivered at most once per boundary and clearly marked as "
            "boundary-respecting. Use sparingly; never for routine findings."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "The boundaried subject"},
                "finding": {"type": "string", "description": "The critical fact"},
                "severity": {"type": "number", "description": "0-1 (default 0.9)"},
            },
            "required": ["subject", "finding"],
        },
    },
    {
        "name": "repo_list_files",
        "description": (
            "List files in one of the owner's designated repositories "
            "(read-only local mirror). Use to explore repo structure."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Configured repo name"},
                "path": {"type": "string", "description": "Optional subdirectory"},
                "limit": {"type": "integer", "default": 200},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "repo_read_file",
        "description": (
            "Read a file from one of the owner's designated repositories "
            "(read-only local mirror)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Configured repo name"},
                "path": {"type": "string", "description": "File path within the repo"},
            },
            "required": ["repo", "path"],
        },
    },
    {
        "name": "repo_search",
        "description": (
            "Search (git grep) one of the owner's designated repositories "
            "(read-only local mirror) for a string or regex."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Configured repo name"},
                "query": {"type": "string", "description": "Search text/regex"},
                "glob": {"type": "string", "description": "Optional pathspec filter, e.g. *.py"},
            },
            "required": ["repo", "query"],
        },
    },
]

# Native server-side tools (calculate, web_search, file_ops)
NATIVE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "calculate",
        "description": "Evaluate a mathematical expression and return the result.",
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "The math expression to evaluate (e.g. '2 + 2', 'sqrt(144)')",
                },
            },
            "required": ["expression"],
        },
    },
    {
        "name": "web_search",
        "description": "Search the web for information on a topic. Returns relevant snippets.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file in the sandbox directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within the sandbox",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file in the sandbox directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within the sandbox",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and directories in a sandbox directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path within the sandbox (default: root)",
                    "default": ".",
                },
            },
            "required": [],
        },
    },
]

# All tools combined
COLONY_TOOLS: list[dict[str, Any]] = COLONY_CORE_TOOLS + COLONY_EXTENDED_TOOLS + NATIVE_TOOLS


def _wrap_openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Wrap a raw tool definition in OpenAI function-calling format.

    Raw format:  {"name": ..., "description": ..., "parameters": ...}
    OpenAI format: {"type": "function", "function": {"name": ..., ...}}

    If the tool already has a "type" key, it is returned unchanged.
    """
    if "type" in tool:
        return tool
    return {"type": "function", "function": tool}


def get_tool_definitions(
    include_extended: bool = True,
    tool_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Get tool definitions for the LLM call.

    Parameters
    ----------
    include_extended :
        Whether to include extended tools (default: True)
    tool_names :
        Optional list of specific tool names to include. If None, returns all.

    Returns
    -------
    List of OpenAI-format tool definitions.
    """
    tools = COLONY_CORE_TOOLS if not include_extended else COLONY_TOOLS

    if tool_names is not None:
        name_set = set(tool_names)
        tools = [t for t in tools if t["name"] in name_set]

    return [_wrap_openai_tool(t) for t in tools]
