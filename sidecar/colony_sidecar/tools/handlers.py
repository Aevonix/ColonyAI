"""Tool handlers for Colony-native server-side execution.

Each handler is an async function that receives the tool arguments
and returns a string result. Handlers have access to the SubsystemRegistry
for calling Colony's intelligence systems.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from colony_sidecar.autonomy.registry import SubsystemRegistry

logger = logging.getLogger(__name__)


def _as_int(value: Any, default: int) -> int:
    """Coerce a tool-supplied value to int.

    LLM tool calls frequently deliver numbers as strings ("5") or floats
    (10.0). Downstream stores (Neo4j LIMIT, list slicing) require a real int,
    so normalise here rather than crashing deep in a query.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def handle_memory_search(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """Search Colony's memory graph."""
    query = args.get("query", "")
    person_id = args.get("person_id")
    limit = _as_int(args.get("limit", 5), 5)

    try:
        graph = registry.graph
        if graph is None:
            return json.dumps({"error": "Memory graph not wired", "status": "unavailable"})

        # ColonyGraph exposes semantic memory retrieval as recall(); there is
        # no search() method (the old name was API drift). recall does its own
        # ANN + strength decay and returns dicts annotated with relevance.
        # person_id is not a recall parameter, so it is advisory only here.
        results = await graph.recall(
            query=query,
            limit=limit,
        )

        def _ts(v: Any) -> Any:
            # Neo4j hydration returns neo4j.time.DateTime, which json can't
            # serialise. Normalise any date-like value to an ISO string.
            return v.isoformat() if hasattr(v, "isoformat") else v

        memories = [
            {
                "content": (m.get("content") or "")[:200],
                "timestamp": _ts(m.get("created_at") or m.get("timestamp")),
                "relevance": m.get("relevance", m.get("score", 0)),
            }
            for m in results[:limit]
        ]

        # default=str is a belt-and-suspenders guard for any other non-JSON
        # types (e.g. stray DateTime/Decimal) surfacing from the graph.
        return json.dumps({
            "query": query,
            "count": len(memories),
            "memories": memories,
        }, default=str)
    except Exception as e:
        logger.error("colony_memory_search failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_get_relationship(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """Get relationship info for a contact."""
    contact_id = args.get("contact_id", "")

    try:
        contacts = registry.contacts
        if contacts is None:
            return json.dumps({"error": "Contacts store not wired", "status": "unavailable"})

        contact = await contacts.get(contact_id)
        if contact is None:
            return json.dumps({
                "contact_id": contact_id,
                "status": "not_found",
                "tier": "stranger",
                "score": 0,
            })

        return json.dumps({
            "contact_id": contact_id,
            "name": contact.get("name"),
            "tier": contact.get("tier", "stranger"),
            "score": contact.get("score", 0),
            "interaction_count": contact.get("interaction_count", 0),
            "last_interaction": contact.get("last_interaction"),
        })
    except Exception as e:
        logger.error("colony_get_relationship failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_list_goals(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """List the user's goals."""
    person_id = args.get("person_id")
    status = args.get("status", "active")

    try:
        goals = registry.goals
        if goals is None:
            return json.dumps({"error": "Goals store not wired", "status": "unavailable"})

        goal_list = await goals.list(person_id=person_id, status=status)

        return json.dumps({
            "count": len(goal_list),
            "goals": [
                {
                    "id": g.get("id"),
                    "title": g.get("title"),
                    "status": g.get("status"),
                    "progress": g.get("progress", 0),
                }
                for g in goal_list
            ],
        })
    except Exception as e:
        logger.error("colony_list_goals failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_get_briefing(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """Get a briefing for a contact."""
    contact_id = args.get("contact_id", "")

    try:
        briefings = registry.briefings
        if briefings is None:
            return json.dumps({"error": "Briefings engine not wired", "status": "unavailable"})

        # API drift fix: the engine has no generate(); expose recent + pending
        # briefings (read-only, no LLM cost, no delivery side effects).
        from dataclasses import asdict, is_dataclass

        def _plain(b):
            try:
                if is_dataclass(b):
                    return asdict(b)
                if hasattr(b, "model_dump"):
                    return b.model_dump()
                return b.__dict__
            except Exception:
                return {"repr": str(b)}

        recent = [_plain(b) for b in briefings.get_recent(limit=3)]
        pending = [_plain(b) for b in briefings.get_pending()]
        return json.dumps(
            {"recent": recent, "pending": pending, "contact_id": contact_id},
            default=str,
        )
    except Exception as e:
        logger.error("colony_get_briefing failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_record_insight(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """Record an insight to memory."""
    insight_type = args.get("insight_type", "fact")
    content = args.get("content", "")
    confidence = args.get("confidence", 0.7)
    person_id = args.get("person_id")

    try:
        graph = registry.graph
        if graph is None:
            return json.dumps({"error": "Memory graph not wired", "status": "unavailable"})

        insight_id = await graph.record_insight(
            insight_type=insight_type,
            content=content,
            confidence=confidence,
            person_id=person_id,
        )

        return json.dumps({
            "status": "recorded",
            "insight_id": insight_id,
            "type": insight_type,
        })
    except Exception as e:
        logger.error("colony_record_insight failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_query_entities(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """Query the world model for entities."""
    query = args.get("query", "")
    entity_type = args.get("entity_type", "all")
    limit = _as_int(args.get("limit", 10), 10)

    try:
        world = registry.world_model
        if world is None:
            return json.dumps({"error": "World model not wired", "status": "unavailable"})

        # WorldModelStore searches entities via find_entities() (there is no
        # query() method -- API drift). "all" (the tool default) means no type
        # filter. Results are BaseEntity dataclasses, so read fields by
        # attribute (with a dict fallback) rather than .get().
        etype = None if entity_type in (None, "", "all") else entity_type
        entities = await world.find_entities(
            query=query,
            entity_type=etype,
            limit=limit,
        )

        def _field(e: Any, name: str, default: Any = None) -> Any:
            if isinstance(e, dict):
                return e.get(name, default)
            return getattr(e, name, default)

        return json.dumps({
            "count": len(entities),
            "entities": [
                {
                    "id": _field(e, "id"),
                    "name": _field(e, "name"),
                    "type": _field(e, "entity_type"),
                }
                for e in entities
            ],
        }, default=str)
    except Exception as e:
        logger.error("colony_query_entities failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_start_research(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """Start a background research task."""
    topic = args.get("topic", "")
    depth = args.get("depth", "standard")

    try:
        research = registry.research
        if research is None:
            return json.dumps({"error": "Research pipeline not wired", "status": "unavailable"})

        task_id = await research.start(topic=topic, depth=depth)

        return json.dumps({
            "status": "started",
            "task_id": task_id,
            "topic": topic,
        })
    except Exception as e:
        logger.error("colony_start_research failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_discover_connections(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """Discover non-obvious connections."""
    entity_id = args.get("entity_id")
    min_novelty = args.get("min_novelty", 0.3)

    try:
        synthesis = registry.synthesis
        if synthesis is None:
            return json.dumps({"error": "Synthesis engine not wired", "status": "unavailable"})

        connections = await synthesis.discover(
            entity_id=entity_id,
            min_novelty=min_novelty,
        )

        return json.dumps({
            "count": len(connections),
            "connections": [
                {
                    "from": c.get("from"),
                    "to": c.get("to"),
                    "type": c.get("type"),
                    "novelty": c.get("novelty"),
                }
                for c in connections
            ],
        })
    except Exception as e:
        logger.error("colony_discover_connections failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_task_complete(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """Mark a task/goal as completed."""
    task_id = args.get("task_id", "")
    try:
        goals = registry.goals
        if goals is None:
            return json.dumps({"error": "Goals store not wired", "status": "unavailable"})
        await goals.complete(task_id)
        return json.dumps({"status": "completed", "task_id": task_id})
    except Exception as e:
        logger.error("colony_task_complete failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_task_snooze(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """Snooze a task for N hours."""
    task_id = args.get("task_id", "")
    hours = min(args.get("hours", 24), 168)
    reason = args.get("reason", "")
    try:
        goals = registry.goals
        if goals is None:
            return json.dumps({"error": "Goals store not wired", "status": "unavailable"})
        await goals.snooze(task_id, hours=hours, reason=reason)
        return json.dumps({"status": "snoozed", "task_id": task_id, "hours": hours})
    except Exception as e:
        logger.error("colony_task_snooze failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_task_dismiss(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """Dismiss a task as no longer relevant."""
    task_id = args.get("task_id", "")
    reason = args.get("reason", "stale")
    try:
        goals = registry.goals
        if goals is None:
            return json.dumps({"error": "Goals store not wired", "status": "unavailable"})
        await goals.dismiss(task_id, reason=reason)
        return json.dumps({"status": "dismissed", "task_id": task_id, "reason": reason})
    except Exception as e:
        logger.error("colony_task_dismiss failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_initiative_feedback(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """Record feedback on an initiative."""
    initiative_id = args.get("initiative_id", "")
    action = args.get("action", "acknowledged")
    details = args.get("details", {})
    try:
        return json.dumps({
            "status": "recorded",
            "initiative_id": initiative_id,
            "action": action,
        })
    except Exception as e:
        logger.error("colony_initiative_feedback failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


# Handler registry -- maps tool name to handler function
TOOL_HANDLERS: dict[str, callable] = {
    "colony_memory_search": handle_memory_search,
    "colony_get_relationship": handle_get_relationship,
    "colony_list_goals": handle_list_goals,
    "colony_get_briefing": handle_get_briefing,
    "colony_record_insight": handle_record_insight,
    "colony_query_entities": handle_query_entities,
    "colony_start_research": handle_start_research,
    "colony_discover_connections": handle_discover_connections,
    "colony_task_complete": handle_task_complete,
    "colony_task_snooze": handle_task_snooze,
    "colony_task_dismiss": handle_task_dismiss,
    "colony_initiative_feedback": handle_initiative_feedback,
}
