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

        # GoalEngine is sync and exposes list_goals(status, limit, offset)
        # returning Goal objects — there is no `list` method and no person_id
        # filter (the old call raised AttributeError on every invocation).
        goal_list = goals.list_goals(status=status, limit=50)

        return json.dumps({
            "count": len(goal_list or []),
            "goals": [
                {
                    "id": getattr(g, "goal_id", None),
                    "title": getattr(g, "title", ""),
                    "status": getattr(getattr(g, "status", None), "value",
                                      str(getattr(g, "status", ""))),
                    "progress": float(getattr(g, "progress_pct", 0.0) or 0.0),
                }
                for g in (goal_list or [])
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

        # The ResearchPipeline runs its stages inline and returns a finished
        # artifact (the previous `.start()` method never existed -> API drift).
        run = await research.run(goal=topic)
        artifact = getattr(run, "artifact", None)
        content = getattr(artifact, "content", "") if artifact else ""
        status = getattr(run, "status", "completed")
        status = getattr(status, "value", None) or str(status)
        return json.dumps({
            "status": status,
            "run_id": getattr(run, "id", None),
            "topic": topic,
            "finding": (content or "")[:1500],
            "word_count": getattr(artifact, "word_count", 0) if artifact else 0,
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
        # The real accessor is connection_discoverer (registry.synthesis never
        # existed -> API drift), and the method is discover_connections().
        discoverer = registry.connection_discoverer
        if discoverer is None:
            return json.dumps({"error": "Connection discoverer not wired", "status": "unavailable"})

        connections = await discoverer.discover_connections(
            person_id=entity_id,
            min_confidence=float(min_novelty),
        )

        def _f(c, *names, default=None):
            for n in names:
                v = c.get(n) if isinstance(c, dict) else getattr(c, n, None)
                if v is not None:
                    return v
            return default

        return json.dumps({
            "count": len(connections),
            "connections": [
                {
                    "from": _f(c, "source", "from", "source_id"),
                    "to": _f(c, "target", "to", "target_id"),
                    "type": _f(c, "connection_type", "type", "relationship_type"),
                    "confidence": _f(c, "confidence", "novelty", default=0),
                }
                for c in connections[:20]
            ],
        }, default=str)
    except Exception as e:
        logger.error("colony_discover_connections failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


def _iso_ts(epoch: Any) -> str:
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()
    except Exception:
        return str(epoch)


async def handle_list_boundaries(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """List the owner's active standing directives / boundaries."""
    try:
        dm = registry.directives
        if dm is None:
            return json.dumps({"error": "Directives not wired", "status": "unavailable"})
        active = dm.active()
        return json.dumps({
            "count": len(active),
            "boundaries": [
                {
                    "id": d.id,
                    "polarity": d.polarity.value,   # prohibit | require | prefer
                    "subject": d.subject,
                    "stated": d.raw_text or d.subject,
                    "since": _iso_ts(d.created_at),
                }
                for d in active
            ],
        }, default=str)
    except Exception as e:
        logger.error("colony_list_boundaries failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_recent_boundary_blocks(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """What autonomous actions were recently refused, and which boundary refused them."""
    limit = _as_int(args.get("limit", 10), 10)
    try:
        dm = registry.directives
        if dm is None:
            return json.dumps({"error": "Directives not wired", "status": "unavailable"})
        blocks = dm.guard.recent_blocks(limit=limit)
        return json.dumps({
            "count": len(blocks),
            "blocks": [
                {
                    "when": _iso_ts(b.get("ts")),
                    "action_kind": b.get("action_kind"),
                    "action": b.get("action_summary"),
                    "refused_because": b.get("directives"),
                    "directive_ids": b.get("directive_ids"),
                }
                for b in blocks
            ],
        }, default=str)
    except Exception as e:
        logger.error("colony_recent_boundary_blocks failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_flag_boundary_concern(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """Surface a CRITICAL finding about a boundaried subject (once, guarded).

    Use ONLY when reflection over a subject the owner told you to leave alone
    reveals something critical (security vulnerability, data loss, financial
    risk). It is delivered at most once per boundary, clearly marked as
    boundary-respecting.
    """
    subject = args.get("subject", "")
    finding = args.get("finding", "")
    try:
        severity = float(args.get("severity", 0.9))
    except (TypeError, ValueError):
        severity = 0.9
    try:
        dm = registry.directives
        if dm is None:
            return json.dumps({"error": "Directives not wired", "status": "unavailable"})
        if not subject or not finding:
            return json.dumps({"error": "subject and finding required", "status": "error"})
        out = await dm.flag_critical(subject, finding, severity=severity)
        return json.dumps(out, default=str)
    except Exception as e:
        logger.error("colony_flag_boundary_concern failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_repo_list_files(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """List files in an owner-designated read-only repo mirror."""
    try:
        mirrors = registry.repo_mirrors
        if mirrors is None:
            return json.dumps({"error": "No repo mirrors configured", "status": "unavailable"})
        return json.dumps(mirrors.list_files(
            args.get("repo", ""), args.get("path", ""),
            limit=_as_int(args.get("limit", 200), 200)), default=str)
    except Exception as e:
        logger.error("repo_list_files failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_repo_read_file(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """Read a file from an owner-designated read-only repo mirror."""
    try:
        mirrors = registry.repo_mirrors
        if mirrors is None:
            return json.dumps({"error": "No repo mirrors configured", "status": "unavailable"})
        return json.dumps(mirrors.read_file(
            args.get("repo", ""), args.get("path", "")), default=str)
    except Exception as e:
        logger.error("repo_read_file failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_repo_search(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """Search an owner-designated read-only repo mirror (git grep)."""
    try:
        mirrors = registry.repo_mirrors
        if mirrors is None:
            return json.dumps({"error": "No repo mirrors configured", "status": "unavailable"})
        return json.dumps(mirrors.search(
            args.get("repo", ""), args.get("query", ""),
            glob=args.get("glob", "")), default=str)
    except Exception as e:
        logger.error("repo_search failed: %s", e)
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
    """Record feedback on an initiative (also drives per-type priority feedback)."""
    initiative_id = args.get("initiative_id", "")
    action = args.get("action", "acknowledged")
    details = args.get("details", {})
    try:
        # Outcome-driven feedback (item 3b): nudge the initiative's TYPE
        # multiplier so classes the owner acts on rise and dismissed ones decay.
        try:
            fb = getattr(registry, "feedback_store", None)
            store = getattr(registry, "initiative_store", None)
            itype = args.get("initiative_type") or details.get("type")
            if not itype and store is not None:
                init = store.get(initiative_id) if hasattr(store, "get") else None
                itype = getattr(init, "type", None)
            if fb is not None and itype:
                fb.record(itype, action)
        except Exception:
            logger.debug("type feedback record failed", exc_info=True)
        return json.dumps({
            "status": "recorded",
            "initiative_id": initiative_id,
            "action": action,
        })
    except Exception as e:
        logger.error("colony_initiative_feedback failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


# --- Cognition program handlers (items 1/3/4/7 + Amendment 1) ---

async def handle_list_projects(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    status = args.get("status", "all")
    try:
        engine = registry.project_engine
        if engine is None:
            return json.dumps({"error": "Projects not wired", "status": "unavailable"})
        items = engine.store.list_projects(
            status=None if status in ("", "all") else status, limit=30)
        return json.dumps({
            "count": len(items),
            "projects": [
                {"id": p.id, "title": p.title, "status": p.status,
                 "source": p.source, "reason": p.reason}
                for p in items
            ],
        }, default=str)
    except Exception as e:
        logger.error("list_projects failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_project_status(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    try:
        engine = registry.project_engine
        if engine is None:
            return json.dumps({"error": "Projects not wired", "status": "unavailable"})
        out = engine.project_status(args.get("project_id", ""))
        return json.dumps(out or {"error": "not_found"}, default=str)
    except Exception as e:
        logger.error("project_status failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_create_project(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    try:
        engine = registry.project_engine
        if engine is None:
            return json.dumps({"error": "Projects not wired", "status": "unavailable"})
        project, reason = engine.create_project(
            args.get("objective", ""), title=args.get("title", ""),
            source="owner")
        if project is None:
            return json.dumps({"created": False, "reason": reason})
        return json.dumps({"created": True, "project_id": project.id,
                           "title": project.title,
                           "note": "planned and pursued on the autonomy loop"})
    except Exception as e:
        logger.error("create_project failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_abandon_project(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    try:
        engine = registry.project_engine
        if engine is None:
            return json.dumps({"error": "Projects not wired", "status": "unavailable"})
        project = engine.abandon(args.get("project_id", ""),
                                 reason=args.get("reason", "owner_request"))
        return json.dumps({"abandoned": project is not None,
                           "status": getattr(project, "status", None)})
    except Exception as e:
        logger.error("abandon_project failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_recall_skills(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    try:
        store = registry.skill_store
        if store is None:
            return json.dumps({"error": "Skills memory not wired", "status": "unavailable"})
        from colony_sidecar.skills_memory import relevant_skills
        skills = relevant_skills(store, args.get("situation", ""), k=3)
        return json.dumps({
            "count": len(skills),
            "skills": [
                {"title": s.title, "situation": s.situation,
                 "steps": s.steps, "gotchas": s.gotchas, "domain": s.domain}
                for s in skills
            ],
        }, default=str)
    except Exception as e:
        logger.error("recall_skills failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_self_status(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    try:
        sm = registry.self_model
        if sm is None:
            return json.dumps({"error": "Self-model not wired", "status": "unavailable"})
        out = sm.status()
        out["brief"] = sm.brief()
        return json.dumps(out, default=str)
    except Exception as e:
        logger.error("self_status failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_action_journal(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    try:
        sm = registry.self_model
        journal = getattr(sm, "journal", None) if sm is not None else None
        if journal is None:
            return json.dumps({"error": "Action journal not wired", "status": "unavailable"})
        entries = journal.recent(limit=_as_int(args.get("limit", 20), 20),
                                 domain=args.get("domain") or None)
        return json.dumps({
            "count": len(entries),
            "entries": [
                {"when": _iso_ts(e.get("ts")), "domain": e.get("domain"),
                 "action": e.get("description"), "decision": e.get("decision"),
                 "reasoning": e.get("reasoning"),
                 "confidence": e.get("confidence"),
                 "outcome": e.get("outcome")}
                for e in entries
            ],
        }, default=str)
    except Exception as e:
        logger.error("action_journal failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_belief_conflicts(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    try:
        engine = registry.belief_engine
        if engine is None:
            return json.dumps({"error": "Belief engine not wired", "status": "unavailable"})
        status = args.get("status", "all")
        items = engine.conflicts(status=None if status in ("", "all") else status,
                                 limit=30)
        return json.dumps({"count": len(items), "conflicts": items}, default=str)
    except Exception as e:
        logger.error("belief_conflicts failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def _resolve_one_contact(store, who: str):
    """Resolve who -> a single contact_id, or return (None, error_json)."""
    who = (who or "").strip()
    if who.startswith("cid-"):
        return who, None
    matches = await store.find_by_name(who, threshold=0.6)
    if not matches:
        return None, json.dumps({"error": f"no contact matching {who!r}",
                                 "status": "not_found"})
    if len(matches) > 1:
        return None, json.dumps({
            "status": "ambiguous",
            "candidates": [{"contact_id": m.contact_id,
                            "display_name": m.display_name}
                           for m in matches[:5]]})
    return matches[0].contact_id, None


async def handle_link_contact(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """Attach a channel handle to a person (owner curation)."""
    try:
        from colony_sidecar.api.routers.host import _contacts_store
        if _contacts_store is None:
            return json.dumps({"error": "contact store not wired",
                               "status": "unavailable"})
        cid, err = await _resolve_one_contact(_contacts_store, args.get("who", ""))
        if err:
            return err
        gw = str(args.get("gateway", "")).strip().lower()
        addr = str(args.get("address", "")).strip()
        if not gw or not addr:
            return json.dumps({"error": "gateway and address required",
                               "status": "error"})
        await _contacts_store.add_handle(cid, gw, addr, verified=True,
                                         source="owner")
        return json.dumps({"linked": True, "contact_id": cid,
                           "gateway": gw, "address": addr})
    except ValueError as e:
        return json.dumps({"error": str(e), "status": "conflict"})
    except Exception as e:
        logger.error("link_contact failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_merge_contacts(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """Merge two contacts that are the same person (owner curation)."""
    try:
        from colony_sidecar.api.routers.host import _contacts_store
        if _contacts_store is None:
            return json.dumps({"error": "contact store not wired",
                               "status": "unavailable"})
        keep, err = await _resolve_one_contact(_contacts_store, args.get("keep", ""))
        if err:
            return err
        merge, err = await _resolve_one_contact(_contacts_store, args.get("merge", ""))
        if err:
            return err
        kept = await _contacts_store.merge_contacts(keep, merge, performed_by="owner")
        return json.dumps({"merged": True, "kept_contact_id": keep,
                           "merged_contact_id": merge,
                           "interaction_count": getattr(kept, "interaction_count", None)},
                          default=str)
    except ValueError as e:
        return json.dumps({"error": str(e), "status": "error"})
    except Exception as e:
        logger.error("merge_contacts failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_pending_contact_proposals(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """List handle-link proposals awaiting owner review."""
    try:
        from colony_sidecar.api.routers.host import _contacts_store
        if _contacts_store is None:
            return json.dumps({"error": "contact store not wired",
                               "status": "unavailable"})
        props = await _contacts_store.list_handle_proposals(limit=50)
        return json.dumps({"count": len(props), "proposals": props}, default=str)
    except Exception as e:
        logger.error("pending_contact_proposals failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_relationship_brief(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    """Standing + psyche + approach brief for a person (docs/RELATIONSHIPS.md)."""
    try:
        from colony_sidecar.api.routers.host import (
            _contacts_store, _relationship_profiler,
        )
        if _relationship_profiler is None:
            return json.dumps({"error": "relationship profiler not wired",
                               "status": "unavailable"})
        who = str(args.get("who", "")).strip()
        if not who:
            return json.dumps({"error": "who is required", "status": "error"})
        contact_id = who
        if not who.startswith("cid-") and _contacts_store is not None:
            matches = await _contacts_store.find_by_name(who, threshold=0.6)
            if not matches:
                return json.dumps({"error": f"no contact matching {who!r}",
                                   "status": "not_found"})
            if len(matches) > 1:
                return json.dumps({
                    "status": "ambiguous",
                    "candidates": [
                        {"contact_id": m.contact_id,
                         "display_name": m.display_name}
                        for m in matches[:5]],
                })
            contact_id = matches[0].contact_id
        brief = (None if args.get("refresh")
                 else _relationship_profiler.cached(contact_id))
        if brief is None:
            brief = await _relationship_profiler.profile(contact_id)
        if brief is None:
            return json.dumps({"error": f"no profile for {contact_id!r}",
                               "status": "not_found"})
        return json.dumps({"brief": brief.to_dict(),
                           "rendered": brief.render()}, default=str)
    except Exception as e:
        logger.error("relationship_brief failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_sandbox_run(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    try:
        sandbox = registry.sandbox
        if sandbox is None:
            return json.dumps({"error": "Sandbox not wired", "status": "unavailable"})
        # Agent-invoked runs are NOT owner-directed: they are flagged for owner
        # approval (the owner auto-runs via the authenticated API). The agent
        # cannot self-grant owner authority here.
        out = sandbox.run(
            args.get("script", ""),
            lang=args.get("lang", "python"),
            purpose=args.get("purpose", ""),
            owner_directed=False,
        )
        return json.dumps(out, default=str)
    except Exception as e:
        logger.error("sandbox_run failed: %s", e)
        return json.dumps({"error": str(e), "status": "error"})


async def handle_sandbox_status(
    args: dict[str, Any],
    registry: SubsystemRegistry,
) -> str:
    try:
        sandbox = registry.sandbox
        if sandbox is None:
            return json.dumps({"error": "Sandbox not wired", "status": "unavailable"})
        return json.dumps(sandbox.status(), default=str)
    except Exception as e:
        logger.error("sandbox_status failed: %s", e)
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
    "colony_list_boundaries": handle_list_boundaries,
    "colony_recent_boundary_blocks": handle_recent_boundary_blocks,
    "repo_list_files": handle_repo_list_files,
    "repo_read_file": handle_repo_read_file,
    "repo_search": handle_repo_search,
    "colony_flag_boundary_concern": handle_flag_boundary_concern,
    "list_projects": handle_list_projects,
    "project_status": handle_project_status,
    "create_project": handle_create_project,
    "abandon_project": handle_abandon_project,
    "recall_skills": handle_recall_skills,
    "self_status": handle_self_status,
    "action_journal": handle_action_journal,
    "belief_conflicts": handle_belief_conflicts,
    "link_contact": handle_link_contact,
    "merge_contacts": handle_merge_contacts,
    "pending_contact_proposals": handle_pending_contact_proposals,
    "relationship_brief": handle_relationship_brief,
    "sandbox_run": handle_sandbox_run,
    "sandbox_status": handle_sandbox_status,
}
