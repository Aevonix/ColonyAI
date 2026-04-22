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
]

# All tools combined
COLONY_TOOLS: list[dict[str, Any]] = COLONY_CORE_TOOLS + COLONY_EXTENDED_TOOLS


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
