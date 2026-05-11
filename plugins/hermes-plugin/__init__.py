"""Colony general plugin for Hermes.

Registers:
  - Native Colony tools (colony_memory_search, colony_list_goals, etc.)
  - WebSocket event subscriber with proactive event caching
  - pre_llm_call hook to inject cached proactive events
  - Slash commands (/colony status, /colony goals, /colony context)
  - CLI commands (hermes colony status, hermes colony goals, etc.)

Plugin directory: ~/.hermes/plugins/colony/
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from .client import ColonyClient
from .events import ColonyEventSubscriber
from .slash import SLASH_COMMANDS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "colony_memory_search",
        "description": (
            "Search Colony's memory graph for relevant context. "
            "Returns ranked memories with timestamps and relevance scores."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Max results (default: 5)", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "colony_list_goals",
        "description": "List user's goals with status and progress.",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["active", "completed", "blocked", "all"],
                    "description": "Filter by status (default: active)",
                    "default": "active",
                },
            },
            "required": [],
        },
    },
    {
        "name": "colony_get_briefing",
        "description": (
            "Get a briefing for a contact — relationship summary, "
            "recent topics, goals, and suggested conversation starters."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string", "description": "The contact's ID"},
            },
            "required": ["contact_id"],
        },
    },
    {
        "name": "colony_record_insight",
        "description": (
            "Record an insight discovered during conversation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "insight_type": {
                    "type": "string",
                    "enum": ["preference", "connection", "fact", "goal_hint", "relationship_update"],
                },
                "content": {"type": "string"},
                "confidence": {"type": "number", "default": 0.7},
                "person_id": {"type": "string"},
            },
            "required": ["insight_type", "content"],
        },
    },
    {
        "name": "colony_query_entities",
        "description": (
            "Query Colony's world model for entities (people, places, organizations, concepts)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "entity_type": {"type": "string", "enum": ["person", "place", "organization", "concept", "all"], "default": "all"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "colony_task_complete",
        "description": "Mark a task/goal as completed.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "colony_task_snooze",
        "description": "Snooze a task for N hours (max 168).",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "hours": {"type": "integer", "default": 24},
                "reason": {"type": "string", "default": ""},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "colony_task_dismiss",
        "description": "Dismiss a task as no longer relevant.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "reason": {"type": "string", "enum": ["stale", "completed", "abandoned", "not_applicable"], "default": "stale"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "colony_initiative_feedback",
        "description": "Provide feedback on how an initiative was handled.",
        "parameters": {
            "type": "object",
            "properties": {
                "initiative_id": {"type": "string"},
                "action": {"type": "string", "enum": ["acknowledged", "actioned", "dismissed", "snoozed"]},
                "details": {"type": "object"},
            },
            "required": ["initiative_id", "action"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

class _ToolDispatcher:
    def __init__(self, client: ColonyClient):
        self._client = client

    def dispatch(self, name: str, args: Dict[str, Any]) -> str:
        handler = getattr(self, f"_handle_{name}", None)
        if handler is None:
            return json.dumps({"error": f"Unknown Colony tool: {name}"})
        try:
            return handler(args)
        except Exception as exc:
            logger.warning("Colony tool %s failed: %s", name, exc)
            return json.dumps({"error": str(exc)})

    def _handle_colony_memory_search(self, args: dict) -> str:
        try:
            payload = {
                "identity": {"host_id": "hermes"},
                "query": args["query"],
                "limit": args.get("limit", 5),
            }
            resp = self._client.post("/v1/host/memory/search", json=payload, timeout=5)
            resp.raise_for_status()
            return json.dumps(resp.json())
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _handle_colony_list_goals(self, args: dict) -> str:
        goals = self._client.list_goals(status=args.get("status", "active"))
        return json.dumps({"goals": goals})

    def _handle_colony_get_briefing(self, args: dict) -> str:
        try:
            resp = self._client.get(f"/v1/host/briefings", timeout=5)
            resp.raise_for_status()
            briefings = resp.json().get("briefings", [])
            cid = args.get("contact_id", "")
            for b in briefings:
                if b.get("contact_id") == cid:
                    return json.dumps(b)
            return json.dumps({"error": f"No briefing found for {cid}"})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _handle_colony_record_insight(self, args: dict) -> str:
        try:
            payload = {
                "identity": {"host_id": "hermes"},
                "content": args["content"],
                "type": args.get("insight_type", "fact"),
                "person_id": args.get("person_id", "default"),
                "strength": args.get("confidence", 0.7),
            }
            resp = self._client.post("/v1/host/memory/write", json=payload, timeout=5)
            resp.raise_for_status()
            return json.dumps(resp.json())
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _handle_colony_query_entities(self, args: dict) -> str:
        try:
            resp = self._client.post(
                "/v1/host/world/entities/query",
                json={
                    "query": args["query"],
                    "entity_type": args.get("entity_type", "all"),
                    "limit": args.get("limit", 10),
                },
                timeout=5,
            )
            resp.raise_for_status()
            return json.dumps(resp.json())
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _handle_colony_task_complete(self, args: dict) -> str:
        try:
            resp = self._client.post(
                f"/v1/host/goals/{args['task_id']}/complete",
                json={},
                timeout=5,
            )
            resp.raise_for_status()
            return json.dumps({"success": True})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _handle_colony_task_snooze(self, args: dict) -> str:
        try:
            resp = self._client.post(
                f"/v1/host/goals/{args['task_id']}/snooze",
                json={"hours": args.get("hours", 24), "reason": args.get("reason", "")},
                timeout=5,
            )
            resp.raise_for_status()
            return json.dumps({"success": True})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _handle_colony_task_dismiss(self, args: dict) -> str:
        try:
            resp = self._client.post(
                f"/v1/host/goals/{args['task_id']}/dismiss",
                json={"reason": args.get("reason", "stale")},
                timeout=5,
            )
            resp.raise_for_status()
            return json.dumps({"success": True})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _handle_colony_initiative_feedback(self, args: dict) -> str:
        try:
            resp = self._client.post(
                "/v1/host/initiatives/feedback",
                json={
                    "initiative_id": args["initiative_id"],
                    "action": args["action"],
                    "details": args.get("details", {}),
                },
                timeout=5,
            )
            resp.raise_for_status()
            return json.dumps({"success": True})
        except Exception as exc:
            return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

_colony_client: Optional[ColonyClient] = None
_event_subscriber: Optional[ColonyEventSubscriber] = None
_tool_dispatcher: Optional[_ToolDispatcher] = None


def register(ctx):
    """Register the Colony general plugin with Hermes."""
    global _colony_client, _event_subscriber, _tool_dispatcher

    config = ctx.config.get("plugins", {}).get("colony", {})
    url = config.get("url", os.environ.get("COLONY_URL", "http://127.0.0.1:7777"))
    api_key = config.get("api_key", os.environ.get("COLONY_API_KEY", ""))
    contact_id = config.get("contact_id", os.environ.get("COLONY_MCP_CONTACT_ID", "default"))

    _colony_client = ColonyClient(url=url, api_key=api_key)
    _tool_dispatcher = _ToolDispatcher(_colony_client)

    # 1. Register native Colony tools
    for schema in _TOOL_SCHEMAS:
        ctx.register_tool(
            name=schema["name"],
            schema=schema,
            handler=lambda name, args, _client=_colony_client: _tool_dispatcher.dispatch(name, args),
        )

    # 2. Register slash commands
    for cmd_name, handler in SLASH_COMMANDS.items():
        ctx.register_slash_command(
            f"colony {cmd_name}",
            lambda args, h=handler, c=_colony_client: h(c, args),
        )

    # 3. Register pre_llm_call hook for proactive events
    async def _pre_llm_call(messages: list, **kwargs) -> list:
        if _event_subscriber is None:
            return messages
        events = await _event_subscriber.cache.get_all()
        if not events:
            return messages
        # Inject events as a system message before the user turn
        lines = ["🔔 Colony proactive events:"]
        for ev in events[:5]:
            lines.append(f"  [{ev.type}] {json.dumps(ev.payload, default=str)[:200]}")
        event_msg = {"role": "system", "content": "\n".join(lines)}
        # Insert before last user message
        result = list(messages)
        result.insert(-1, event_msg)
        return result

    ctx.register_hook("pre_llm_call", _pre_llm_call)

    # 4. Register on_session_end hook
    def _on_session_end(messages: list) -> None:
        if _event_subscriber is not None:
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(_event_subscriber.stop())
            except RuntimeError:
                pass

    ctx.register_hook("on_session_end", _on_session_end)

    # 5. Start WebSocket event subscriber (best-effort)
    try:
        _event_subscriber = ColonyEventSubscriber(
            url=url,
            api_key=api_key,
            contact_id=contact_id,
        )
        _event_subscriber.start()
        logger.info("Colony event subscriber started")
    except Exception as exc:
        logger.warning("Colony event subscriber failed to start: %s", exc)

    logger.info("Colony general plugin registered (url=%s)", url)
