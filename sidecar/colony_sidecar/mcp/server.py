"""Colony MCP Server implementation.

Thin adapter that translates MCP tool calls into Colony sidecar HTTP requests.
All cognitive state lives in the sidecar — this server has no direct DB access.
"""

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_url() -> str:
    return os.environ.get("COLONY_URL", "http://127.0.0.1:7777")


def _api_key() -> str:
    return os.environ.get("COLONY_API_KEY", "")


def _headers() -> dict[str, str]:
    key = _api_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _source() -> str | None:
    return os.environ.get("COLONY_MCP_SOURCE")


def _contact_id(override: str | None = None) -> str | None:
    return override or os.environ.get("COLONY_MCP_CONTACT_ID")


def _require_contact(override: str | None = None) -> tuple[str, dict[str, str]]:
    """Return (contact_id, error_dict) — if contact_id is None, error_dict has the error."""
    cid = _contact_id(override)
    if cid:
        return cid, {}
    return "", {
        "error": "contact_id_required",
        "message": "No contact_id provided and COLONY_MCP_CONTACT_ID is not set",
        "suggestion": "Set COLONY_MCP_CONTACT_ID in your MCP config or pass contact_id explicitly",
    }


def _sidecar_error(exc: Exception) -> dict[str, str]:
    """Format a sidecar connection error."""
    return {
        "error": "sidecar_unreachable",
        "message": f"Colony sidecar not reachable at {_base_url()}",
        "suggestion": "Start with: colony start",
    }


async def _get(path: str, params: dict | None = None) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{_base_url()}{path}", headers=_headers(), params=params)
            if r.status_code == 200:
                return r.json()
            return {"error": f"http_{r.status_code}", "message": r.text[:1000]}
    except httpx.ConnectError as exc:
        return _sidecar_error(exc)
    except Exception as exc:
        return {"error": "request_failed", "message": str(exc)[:1000]}


async def _post(path: str, data: dict) -> dict | None:
    try:
        # Inject provenance from COLONY_MCP_SOURCE into metadata so it
        # survives Pydantic validation (top-level 'provenance' is not in any schema).
        src = _source()
        if src:
            data.setdefault("metadata", {})["provenance"] = src
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{_base_url()}{path}", headers={**_headers(), "Content-Type": "application/json"}, json=data)
            if r.status_code in (200, 201):
                return r.json()
            return {"error": f"http_{r.status_code}", "message": r.text[:1000]}
    except httpx.ConnectError as exc:
        return _sidecar_error(exc)
    except Exception as exc:
        return {"error": "request_failed", "message": str(exc)[:1000]}


async def _patch(path: str, data: dict) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.patch(f"{_base_url()}{path}", headers={**_headers(), "Content-Type": "application/json"}, json=data)
            if r.status_code == 200:
                return r.json()
            return {"error": f"http_{r.status_code}", "message": r.text[:1000]}
    except httpx.ConnectError as exc:
        return _sidecar_error(exc)
    except Exception as exc:
        return {"error": "request_failed", "message": str(exc)[:1000]}


async def _delete(path: str) -> tuple[int, str]:
    """Delete a resource. Returns (status_code, error_message_or_empty)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.delete(f"{_base_url()}{path}", headers=_headers())
            return r.status_code, ""
    except httpx.ConnectError as exc:
        return -1, str(exc)[:200]
    except Exception as exc:
        return -1, str(exc)[:200]


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def create_server() -> FastMCP:
    """Create the Colony MCP server with all tools registered."""

    mcp = FastMCP(
        "colony",
        instructions=(
            "Colony provides a cognitive substrate for AI agents: commitments, facts, "
            "affect tracking, world model, patterns, and surprises. Use these tools to "
            "give your agent memory, awareness, and continuity across sessions."
        ),
    )

    # --- Read-only tools (safe for auto-call) ---

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def colony_health() -> dict:
        """Check if Colony sidecar is running and healthy. Call at session start or when other tools fail."""
        return await _get("/v1/host/health")

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def colony_get_context(
        contact_id: str | None = None,
        message: str | None = None,
    ) -> dict:
        """Get assembled context for a contact. Returns commitments, affect, facts, and more — the same sections the OpenClaw plugin gets. Call at the start of a task or when the user asks what to work on."""
        cid, err = _require_contact(contact_id)
        if err:
            return err

        payload: dict[str, Any] = {
            "identity": {"host_id": "mcp"},
            "context": {"session_id": "mcp", "contact_id": cid},
            "incoming_message": {"role": "user", "content": message or ""},
        }

        return await _post("/v1/host/context/assemble", payload)

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def colony_check_commitments(
        status: str | None = None,
        person_id: str | None = None,
        limit: int = 10,
    ) -> dict:
        """List or search commitments. Call before starting work, when a deadline is mentioned, or when planning a sprint. If person_id is not provided, returns all commitments."""
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        cid = _contact_id(person_id)
        if cid:
            params["person_id"] = cid
        return await _get("/v1/host/commitments", params=params)

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def colony_lookup_facts(
        contact_id: str | None = None,
        source: str | None = None,
        min_confidence: float | None = None,
        limit: int = 10,
    ) -> dict:
        """Retrieve facts about a contact. Call when starting a conversation, making design decisions, or personalizing output. Filter by source (told_by_contact, told_to_contact, shared_context, inferred) or minimum confidence."""
        cid, err = _require_contact(contact_id)
        if err:
            return err
        params: dict[str, Any] = {"contact_id": cid, "limit": limit}
        if source:
            params["source"] = source
        if min_confidence is not None:
            params["min_confidence"] = min_confidence
        return await _get("/v1/host/mind/facts", params=params)

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def colony_check_affect(
        contact_id: str | None = None,
    ) -> dict:
        """Get current affect state for a contact. Call before delivering bad news or when deciding how to frame feedback."""
        cid, err = _require_contact(contact_id)
        if err:
            return err
        return await _get(f"/v1/host/affect/state/{cid}")

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def colony_search_world(
        query: str,
        entity_type: str | None = None,
        limit: int = 5,
    ) -> dict:
        """Search the world model for entities or relationships. Call when exploring a codebase, understanding dependencies, or planning changes."""
        data: dict[str, Any] = {
            "identity": {"host_id": "mcp"},
            "query": query,
            "limit": limit,
        }
        if entity_type:
            data["entity_type"] = entity_type
        return await _post("/v1/host/world/entities/query", data)

    @mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
    async def colony_get_patterns(
        pattern_type: str | None = None,
        min_frequency: int | None = None,
        active_only: bool = False,
        limit: int = 5,
    ) -> dict:
        """Retrieve learned patterns. Call when suggesting workflows, onboarding to a project, or planning work. Filter by pattern_type, minimum frequency, or active status."""
        params: dict[str, Any] = {"limit": limit}
        if pattern_type:
            params["pattern_type"] = pattern_type
        if min_frequency is not None:
            params["min_frequency"] = min_frequency
        if active_only:
            params["active_only"] = True
        return await _get("/v1/host/patterns", params=params)

    # --- Mutating tools ---

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": False})
    async def colony_create_commitment(
        description: str,
        person_id: str | None = None,
        due_at: str | None = None,
        priority: int = 50,
    ) -> dict:
        """Create a new commitment. Call when the user promises something, agrees to a deadline, or a task has a clear due date. Priority is 0-100, default 50."""
        cid, err = _require_contact(person_id)
        if err:
            return err
        data: dict[str, Any] = {
            "person_id": cid,
            "description": description,
            "priority": priority,
        }
        if due_at:
            data["due_at"] = due_at
        return await _post("/v1/host/commitments", data)

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    async def colony_fulfill_commitment(
        commitment_id: str,
    ) -> dict:
        """Mark a commitment as fulfilled. Call when a task is completed or a promise is kept."""
        return await _patch(f"/v1/host/commitments/{commitment_id}", {"status": "fulfilled"})

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    async def colony_cancel_commitment(
        commitment_id: str,
        reason: str | None = None,
    ) -> dict:
        """Cancel a commitment that's no longer relevant. Not the same as fulfilled — cancelled means it won't be done."""
        data: dict[str, Any] = {"status": "cancelled"}
        if reason:
            data.setdefault("metadata", {})["cancellation_reason"] = reason
        return await _patch(f"/v1/host/commitments/{commitment_id}", data)

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": False})
    async def colony_remember_fact(
        fact: str,
        contact_id: str | None = None,
        source: str | None = None,
        confidence: float = 0.8,
    ) -> dict:
        """Store a fact about a person, project, or concept. Call when the user states a preference, makes a decision, or reveals context worth remembering. Source can be: told_by_contact, told_to_contact, shared_context, inferred."""
        cid, err = _require_contact(contact_id)
        if err:
            return err
        data: dict[str, Any] = {
            "contact_id": cid,
            "fact": fact,
            "confidence": confidence,
        }
        if source:
            data["source"] = source
        return await _post("/v1/host/mind/facts", data)

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    async def colony_forget_fact(
        fact_id: str,
    ) -> dict:
        """Remove an outdated or incorrect fact. Call when you learn a fact was wrong, a preference changes, or context is stale."""
        status, err_msg = await _delete(f"/v1/host/mind/facts/{fact_id}")
        if status == 204 or status == 200:
            return {"deleted": True}
        return {"error": "delete_failed", "message": f"Status {status}: {err_msg}"}

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": False})
    async def colony_record_affect(
        valence: float,
        trigger: str,
        contact_id: str | None = None,
        arousal: float = 0.5,
    ) -> dict:
        """Record an emotional state or mood observation. Call when the user expresses frustration or satisfaction, or after successes/failures."""
        cid, err = _require_contact(contact_id)
        if err:
            return err
        data = {
            "contact_id": cid,
            "valence": valence,
            "arousal": arousal,
            "trigger": trigger,
        }
        return await _post("/v1/host/affect/events", data)

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": False})
    async def colony_record_surprise(
        observation: str,
        expected: str | None = None,
        surprise_score: float | None = None,
        pattern_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict:
        """Record something unexpected. Call when something doesn't behave as expected, a bug is weirder than anticipated, or assumptions are violated. Observation should include what actually happened vs what was expected."""
        data: dict[str, Any] = {
            "observation": observation,
        }
        if expected is not None:
            data["expected"] = expected
        if surprise_score is not None:
            data["surprise_score"] = surprise_score
        if pattern_id:
            data["pattern_id"] = pattern_id
        if context is not None:
            data["context"] = context
        return await _post("/v1/host/surprises", data)

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    async def colony_task_complete(
        task_id: str,
    ) -> dict:
        """Mark a task as completed. Call when an initiative mentions a task that's done."""
        return await _post(f"/v1/host/tasks/{task_id}/complete", {})

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    async def colony_task_snooze(
        task_id: str,
        hours: int = 24,
        reason: str = "",
    ) -> dict:
        """Snooze a task — don't generate initiatives for it for N hours (1-168). Call when a task is valid but not actionable right now."""
        return await _post(f"/v1/host/tasks/{task_id}/snooze", {"hours": hours, "reason": reason})

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    async def colony_task_dismiss(
        task_id: str,
        reason: str = "stale",
    ) -> dict:
        """Dismiss a task as no longer relevant. Reason can be: stale, completed, abandoned, not_applicable. Call when a task is clearly outdated or irrelevant."""
        return await _post(f"/v1/host/tasks/{task_id}/dismiss", {"reason": reason})

    @mcp.tool(annotations={"readOnlyHint": False, "idempotentHint": True})
    async def colony_initiative_feedback(
        initiative_id: str,
        action: str,
        details: dict[str, Any] | None = None,
    ) -> dict:
        """Provide feedback on how an initiative was handled. Action can be: acknowledged, actioned, dismissed, snoozed. Call after handling a colony_initiative."""
        data: dict[str, Any] = {"action": action}
        if details:
            data["details"] = details
        return await _post(f"/v1/host/initiatives/{initiative_id}/respond", data)

    # --- Resources ---

    @mcp.resource("colony://status")
    async def status_resource() -> dict:
        """Current Colony system status."""
        return await _get("/v1/host/health")

    @mcp.resource("colony://commitments")
    async def commitments_resource() -> dict:
        """All active commitments (pending + overdue)."""
        return await _get("/v1/host/commitments", params={"status": "pending"})

    @mcp.resource("colony://affect/{contact_id}")
    async def affect_resource(contact_id: str) -> dict:
        """Current affect state for a contact."""
        return await _get(f"/v1/host/affect/state/{contact_id}")

    @mcp.resource("colony://facts/{contact_id}")
    async def facts_resource(contact_id: str) -> dict:
        """Known facts for a contact."""
        return await _get("/v1/host/mind/facts", params={"contact_id": contact_id})

    @mcp.resource("colony://world/entities")
    async def world_resource() -> dict:
        """Top entities in the world model."""
        return await _post("/v1/host/world/entities/query", {"identity": {"host_id": "mcp"}, "query": "", "limit": 10})

    @mcp.resource("colony://surprises/unresolved")
    async def surprises_resource() -> dict:
        """Current unresolved surprises."""
        return await _get("/v1/host/surprises", params={"resolved": False})

    # --- Prompts ---

    @mcp.prompt()
    async def daily_briefing() -> str:
        """Review commitments, affect state, and surprises. Prioritize what to work on today."""
        cid = _contact_id()
        if not cid:
            return "Set COLONY_MCP_CONTACT_ID to get your daily briefing."
        return (
            f"Review the following for {cid}:\n"
            "1. Check colony_check_commitments for pending and overdue items\n"
            "2. Check colony_check_affect for current mood\n"
            "3. Check colony_get_patterns for workflow patterns\n"
            "4. Check colony://surprises/unresolved for anything unexpected\n"
            "Then prioritize what to work on today based on deadlines, mood, and surprises."
        )

    @mcp.prompt()
    async def pre_task() -> str:
        """Before starting a task, check commitments and facts about relevant people and components."""
        cid = _contact_id()
        if not cid:
            return "Set COLONY_MCP_CONTACT_ID to use pre-task context."
        return (
            f"Before starting this task:\n"
            f"1. Call colony_check_commitments to see what {cid} has pending\n"
            f"2. Call colony_lookup_facts to recall relevant context\n"
            f"3. Call colony_check_affect to gauge current mood\n"
            "Use this information to prioritize and tailor your approach."
        )

    @mcp.prompt()
    async def post_task() -> str:
        """After completing a task, record what happened and check off commitments."""
        cid = _contact_id()
        if not cid:
            return "Set COLONY_MCP_CONTACT_ID to use post-task recording."
        return (
            f"I just completed a task. For {cid}:\n"
            "1. If there was a commitment for this task, call colony_fulfill_commitment\n"
            "2. If anything unexpected happened, call colony_record_surprise\n"
            "3. If I learned something worth remembering, call colony_remember_fact\n"
            "4. If the user's mood shifted, call colony_record_affect"
        )

    return mcp


def run_stdio() -> None:
    """Run the MCP server using stdio transport."""
    server = create_server()
    server.run(transport="stdio")


def run_http(host: str = "127.0.0.1", port: int = 7778) -> None:
    """Run the MCP server using streamable HTTP transport."""
    server = create_server()
    server.run(transport="streamable-http", host=host, port=port)


if __name__ == "__main__":
    run_stdio()
