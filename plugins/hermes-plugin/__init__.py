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
    {
        "name": "colony_list_initiatives",
        "description": (
            "List Colony's pending initiatives — relationship reminders, task follow-ups, "
            "and scheduling suggestions. Returns initiatives with type, priority, status, "
            "and entity_id. Use this to discover what Colony wants the agent to act on."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "assigned", "acknowledged", "completed", "failed", "cancelled"],
                    "description": "Filter by status (omit for all)",
                },
                "limit": {"type": "integer", "default": 50},
            },
            "required": [],
        },
    },
    {
        "name": "colony_get_initiative",
        "description": "Get full details of a single initiative by ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "initiative_id": {"type": "string"},
            },
            "required": ["initiative_id"],
        },
    },
    {
        "name": "colony_autonomy_enable",
        "description": (
            "Enable the Colony autonomy bridge. Creates a cron job that polls Colony "
            "every 15 minutes and acts on initiatives autonomously (drafts messages, "
            "completes tasks, proposes scheduling). Reports back only when action is taken."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "interval": {"type": "string", "default": "every 15m"},
            },
            "required": [],
        },
    },
    {
        "name": "colony_autonomy_disable",
        "description": "Disable the Colony autonomy bridge. Removes the cron job.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "colony_autonomy_status",
        "description": "Check if the Colony autonomy bridge is active and show recent activity.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
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
                f"/v1/host/tasks/{args['task_id']}/complete",
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
                f"/v1/host/tasks/{args['task_id']}/snooze",
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
                f"/v1/host/tasks/{args['task_id']}/dismiss",
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
                f"/v1/host/initiatives/{args['initiative_id']}/respond",
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

    def _handle_colony_list_initiatives(self, args: dict) -> str:
        try:
            initiatives = self._client.list_initiatives(
                status=args.get("status"),
                limit=args.get("limit", 50),
            )
            return json.dumps({"initiatives": initiatives, "total": len(initiatives)})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _handle_colony_get_initiative(self, args: dict) -> str:
        try:
            initiative = self._client.get_initiative(args["initiative_id"])
            if initiative is None:
                return json.dumps({"error": "Initiative not found"})
            return json.dumps(initiative)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _handle_colony_autonomy_enable(self, args: dict) -> str:
        try:
            result = _create_or_update_autonomy_job(
                interval=args.get("interval", "every 15m"),
                client=self._client,
            )
            return json.dumps(result)
        except Exception as exc:
            logger.warning("autonomy_enable failed: %s", exc)
            return json.dumps({"error": str(exc)})

    def _handle_colony_autonomy_disable(self, args: dict) -> str:
        try:
            result = _remove_autonomy_job()
            return json.dumps(result)
        except Exception as exc:
            logger.warning("autonomy_disable failed: %s", exc)
            return json.dumps({"error": str(exc)})

    def _handle_colony_autonomy_status(self, args: dict) -> str:
        try:
            result = _get_autonomy_status(client=self._client)
            return json.dumps(result)
        except Exception as exc:
            logger.warning("autonomy_status failed: %s", exc)
            return json.dumps({"error": str(exc)})


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

_colony_client: Optional[ColonyClient] = None
_event_subscriber: Optional[ColonyEventSubscriber] = None
_tool_dispatcher: Optional[_ToolDispatcher] = None
_contact_id: str = "default"

# Mutable state captured across hooks (agent:start → on_session_end)
_session_state: dict = {}

# ---------------------------------------------------------------------------
# Autonomy bridge helpers
# ---------------------------------------------------------------------------

_AUTONOMY_JOB_NAME = "Colony Autonomy Bridge"
_AUTONOMY_JOB_ID: Optional[str] = None  # cached after lookup

_AUTONOMY_PROMPT = """\
You are the Colony Autonomy Bridge — the owner's autonomous agent. You act ON THEIR
BEHALF, not as a reminder service. You consume initiatives from the Colony
sidecar (http://127.0.0.1:7777) and execute them directly.

YOUR JOB EACH CYCLE:
1. Query Colony for pending initiatives using colony_list_initiatives.
2. For each initiative, classify its type and EXECUTE (don't suggest):

   RELATIONSHIP:
   - The owner hasn't contacted someone in a while.
   - Fetch their briefing via colony_get_briefing to get context.
   - Draft a warm, context-aware outreach message IN THE OWNER'S VOICE.
   - SEND IT DIRECTLY TO THE CONTACT via send_message. Do NOT send it to the owner.
   - Only skip sending if: (a) contact channel is unknown, (b) content feels
     wrong — in which case, note it for the owner's review.

   FOLLOW_UP / TASK:
   - A goal needs action. Use available tools to complete it.
   - If you can finish it, do so and mark it complete.
   - If blocked, note the blocker for the owner's review.

   SCHEDULING:
   - A commitment is due soon. If you can schedule it (calendar, meeting),
     do so. Otherwise, draft the request and note it for the owner.

3. After handling all initiatives, report ONLY:
   - Actions taken autonomously (messages sent, tasks completed, etc.)
   - Items that need human judgment (with your reasoning)
   - Any errors encountered

RULES:
- You ARE the owner's agent. Act with his voice and judgment. Do not ask permission
  for obvious, low-risk actions (e.g., sending a "how are you" to a friend).
- Stay silent (start your response with [SILENT]) if there are no initiatives
  and nothing to report.
- Do not hallucinate contact details. Always use Colony's data.
- NEVER send reminders TO the owner. They do not want to be pinged with "you should
  text Jordan." Either send the message FOR them, or report that you couldn't.
- If sending a message, confirm the recipient channel and send it. Do not
  paraphrase the message back to the owner unless they ask.
"""


def _find_autonomy_job() -> Optional[dict]:
    """Find the existing Colony autonomy cron job, if any."""
    try:
        from cron.jobs import load_jobs
        jobs = load_jobs()
        for job in jobs:
            if job.get("name") == _AUTONOMY_JOB_NAME:
                return job
    except Exception as exc:
        logger.debug("Could not load cron jobs for lookup: %s", exc)
    return None


def _create_or_update_autonomy_job(interval: str = "every 15m", client: Optional[ColonyClient] = None) -> dict:
    """Create or update the autonomy cron job."""
    existing = _find_autonomy_job()

    # Try using the proper cron API first
    try:
        from cron.jobs import create_job, update_job
        if existing:
            updated = update_job(existing["id"], {
                "enabled": True,
                "state": "scheduled",
                "schedule": interval,
            })
            if updated:
                # Trigger a Colony cycle so initiatives are fresh
                if client:
                    client.trigger_autonomy_cycle()
                return {
                    "success": True,
                    "message": "Colony autonomy bridge re-enabled.",
                    "job_id": existing["id"],
                    "schedule": interval,
                }
        else:
            job = create_job(
                prompt=_AUTONOMY_PROMPT,
                schedule=interval,
                name=_AUTONOMY_JOB_NAME,
                deliver="origin",
                enabled_toolsets=["web", "terminal", "file", "send_message", "colony"],
            )
            if client:
                client.trigger_autonomy_cycle()
            return {
                "success": True,
                "message": "Colony autonomy bridge enabled.",
                "job_id": job["id"],
                "schedule": interval,
            }
    except Exception as exc:
        logger.warning("cron.jobs API failed (%s), falling back to direct JSON write", exc)

    # Fallback: direct JSON manipulation
    try:
        import json
        import uuid
        from datetime import datetime, timezone
        from pathlib import Path
        from hermes_constants import get_hermes_home

        hermes_home = Path(get_hermes_home())
        jobs_file = hermes_home / "cron" / "jobs.json"
        jobs_file.parent.mkdir(parents=True, exist_ok=True)

        jobs_data = {"jobs": []}
        if jobs_file.exists():
            with open(jobs_file, "r", encoding="utf-8") as f:
                jobs_data = json.load(f)

        jobs = jobs_data.get("jobs", [])
        now = datetime.now(timezone.utc).isoformat()

        if existing:
            for j in jobs:
                if j.get("id") == existing["id"]:
                    j["enabled"] = True
                    j["state"] = "scheduled"
                    j["schedule"] = {"kind": "interval", "minutes": 15, "display": interval}
                    j["schedule_display"] = interval
                    break
        else:
            job_id = uuid.uuid4().hex[:12]
            new_job = {
                "id": job_id,
                "name": _AUTONOMY_JOB_NAME,
                "prompt": _AUTONOMY_PROMPT,
                "skills": [],
                "skill": None,
                "model": None,
                "provider": None,
                "base_url": None,
                "script": None,
                "no_agent": False,
                "context_from": None,
                "schedule": {"kind": "interval", "minutes": 15, "display": interval},
                "schedule_display": interval,
                "repeat": {"times": None, "completed": 0},
                "enabled": True,
                "state": "scheduled",
                "paused_at": None,
                "paused_reason": None,
                "created_at": now,
                "next_run_at": None,
                "last_run_at": None,
                "last_status": None,
                "last_error": None,
                "last_delivery_error": None,
                "deliver": "origin",
                "origin": None,
                "enabled_toolsets": ["web", "terminal", "file", "send_message", "colony"],
                "workdir": None,
            }
            jobs.append(new_job)

        with open(jobs_file, "w", encoding="utf-8") as f:
            json.dump({"jobs": jobs, "updated_at": now}, f, indent=2)

        if client:
            client.trigger_autonomy_cycle()

        return {
            "success": True,
            "message": "Colony autonomy bridge enabled (fallback mode).",
            "schedule": interval,
        }
    except Exception as exc2:
        return {"success": False, "error": f"Failed to create cron job: {exc2}"}


def _remove_autonomy_job() -> dict:
    """Remove or disable the autonomy cron job."""
    existing = _find_autonomy_job()
    if not existing:
        return {"success": True, "message": "Colony autonomy bridge was not active."}

    try:
        from cron.jobs import update_job
        update_job(existing["id"], {"enabled": False, "state": "paused"})
        return {
            "success": True,
            "message": "Colony autonomy bridge disabled.",
            "job_id": existing["id"],
        }
    except Exception as exc:
        logger.warning("cron.jobs update failed (%s), falling back to direct JSON", exc)

    try:
        import json
        from pathlib import Path
        from hermes_constants import get_hermes_home

        hermes_home = Path(get_hermes_home())
        jobs_file = hermes_home / "cron" / "jobs.json"
        if not jobs_file.exists():
            return {"success": True, "message": "No cron jobs file found."}

        with open(jobs_file, "r", encoding="utf-8") as f:
            jobs_data = json.load(f)

        jobs = jobs_data.get("jobs", [])
        for j in jobs:
            if j.get("id") == existing["id"]:
                j["enabled"] = False
                j["state"] = "paused"
                break

        with open(jobs_file, "w", encoding="utf-8") as f:
            json.dump(jobs_data, f, indent=2)

        return {
            "success": True,
            "message": "Colony autonomy bridge disabled (fallback mode).",
            "job_id": existing["id"],
        }
    except Exception as exc2:
        return {"success": False, "error": f"Failed to disable: {exc2}"}


def _get_autonomy_status(client: Optional[ColonyClient] = None) -> dict:
    """Return the current autonomy bridge status."""
    existing = _find_autonomy_job()
    job_status = {
        "active": False,
        "job_id": None,
        "next_run": None,
        "last_run": None,
        "last_status": None,
    }
    if existing:
        job_status["active"] = existing.get("enabled", False) and existing.get("state") == "scheduled"
        job_status["job_id"] = existing.get("id")
        job_status["next_run"] = existing.get("next_run_at")
        job_status["last_run"] = existing.get("last_run_at")
        job_status["last_status"] = existing.get("last_status")

    colony_status = {}
    if client:
        try:
            health = client.health()
            colony_status["sidecar"] = health.get("status", "unknown")
            colony_status["capabilities_count"] = len(health.get("capabilities", []))
        except Exception:
            colony_status["sidecar"] = "unreachable"

        try:
            initiatives = client.list_initiatives(status="pending", limit=20)
            colony_status["pending_initiatives"] = len(initiatives)
        except Exception:
            colony_status["pending_initiatives"] = "unknown"

    return {
        "success": True,
        "autonomy_active": job_status["active"],
        "job": job_status,
        "colony": colony_status,
        "message": (
            "Colony autonomy bridge is active."
            if job_status["active"]
            else "Colony autonomy bridge is inactive. Run colony_autonomy_enable to activate."
        ),
    }


# ---------------------------------------------------------------------------
# Colony LLM auto-configuration
# ---------------------------------------------------------------------------

def _detect_ollama_models() -> dict[str, str] | None:
    """Query Ollama for available models and return tier mappings."""
    try:
        import urllib.request
        req = urllib.request.Request("http://localhost:11434/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode())
        models = [m["name"] for m in data.get("models", [])]
        if not models:
            return None
        # Prefer larger models for LARGE tier if name hints at size
        size_hints = {"70b": 3, "32b": 2, "13b": 1, "8b": 0, "7b": 0, "3b": -1, "1b": -2}
        def score(m: str) -> int:
            return sum(size_hints.get(k, 0) for k in size_hints if k in m.lower())
        models_sorted = sorted(models, key=score, reverse=True)
        return {
            "large": f"ollama/{models_sorted[0]}",
            "medium": f"ollama/{models_sorted[len(models_sorted)//2]}" if len(models_sorted) > 1 else f"ollama/{models_sorted[0]}",
            "small": f"ollama/{models_sorted[-1]}",
        }
    except Exception:
        return None


def _configure_colony_llm(client: ColonyClient, plugin_config: dict) -> None:
    """Push LLM config to Colony sidecar so it can use local models.

    Priority:
      1. Explicit plugin config (llm_provider, llm_base_url, llm_models)
      2. Environment variables (COLONY_LLM_*)
      3. Auto-detect Ollama on localhost:11434
      4. Skip if nothing found
    """
    provider = plugin_config.get("llm_provider", os.environ.get("COLONY_LLM_PROVIDER", "")).lower()
    base_url = plugin_config.get("llm_base_url", os.environ.get("COLONY_LLM_BASE_URL", ""))
    models_env = os.environ.get("COLONY_LLM_MODELS", "")
    models: dict[str, str] = {}

    # Parse explicit model mappings
    if models_env:
        try:
            models = json.loads(models_env)
        except json.JSONDecodeError:
            logger.warning("Invalid COLONY_LLM_MODELS JSON, ignoring")
    for tier in ("small", "medium", "large"):
        env_key = f"COLONY_LLM_{tier.upper()}"
        val = os.environ.get(env_key, "")
        if val:
            models[tier] = val
        cfg_key = f"llm_{tier}"
        if cfg_key in plugin_config:
            models[tier] = plugin_config[cfg_key]

    # Auto-detect Ollama if no explicit config
    if not provider and not models:
        detected = _detect_ollama_models()
        if detected:
            provider = "ollama"
            models = detected
            base_url = base_url or "http://localhost:11434"
            logger.info("Auto-detected Ollama models for Colony: %s", models)

    if not provider and not models:
        logger.debug("No Colony LLM config found — skipping sidecar configuration")
        return

    # Default provider to "local" if models are set but provider isn't
    provider = provider or "local"

    payload = {
        "identity": {"host_id": "hermes"},
        "llm": {
            "provider": provider,
            "baseUrl": base_url,
            "models": models,
        },
    }

    try:
        resp = client.post("/v1/host/configure", json=payload, timeout=10)
        if resp.status_code < 300:
            data = resp.json()
            logger.info(
                "Colony LLM configured (provider=%s, models=%s)",
                data.get("provider", provider),
                data.get("models", models),
            )
        else:
            logger.warning("Colony LLM configure failed: %s %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.warning("Colony LLM configure request failed: %s", exc)


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx):
    """Register the Colony general plugin with Hermes."""
    global _colony_client, _event_subscriber, _tool_dispatcher, _contact_id

    config = ctx.config.get("plugins", {}).get("colony", {})
    url = config.get("url", os.environ.get("COLONY_URL", "http://127.0.0.1:7777"))
    api_key = config.get("api_key", os.environ.get("COLONY_API_KEY", ""))
    contact_id = config.get("contact_id", os.environ.get("COLONY_MCP_CONTACT_ID", "default"))
    _contact_id = contact_id

    _colony_client = ColonyClient(url=url, api_key=api_key)
    _tool_dispatcher = _ToolDispatcher(_colony_client)

    # 0. Configure Colony LLM (auto-detect local models or use explicit config)
    _configure_colony_llm(_colony_client, config)

    # 1. Register native Colony tools
    for schema in _TOOL_SCHEMAS:
        ctx.register_tool(
            name=schema["name"],
            toolset="colony",
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

    # 4. Register agent:start hook to capture session metadata for turns/sync
    def _on_agent_start(hook_ctx: dict) -> None:
        _session_state["session_id"] = hook_ctx.get("session_id", "")
        _session_state["platform"] = hook_ctx.get("platform", "")
        _session_state["user_id"] = hook_ctx.get("user_id", "")

    ctx.register_hook("agent:start", _on_agent_start)

    # 5. Register on_session_end hook — stop subscriber AND sync turn to Colony
    def _on_session_end(messages: list) -> None:
        if _event_subscriber is not None:
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(_event_subscriber.stop())
            except RuntimeError:
                pass

        # Sync session summary to Colony (plugin-only telemetry)
        if _colony_client is None:
            return
        session_id = _session_state.get("session_id", "")
        if not session_id:
            return

        user_msg = ""
        assistant_msg = ""
        for msg in reversed(messages):
            role = msg.get("role", "")
            if role == "assistant" and not assistant_msg:
                assistant_msg = msg.get("content", "")
            elif role == "user" and not user_msg:
                user_msg = msg.get("content", "")
            if user_msg and assistant_msg:
                break

        summary = ""
        if user_msg and assistant_msg:
            summary = f"User: {user_msg[:300]}\nAgent: {assistant_msg[:300]}"

        try:
            _colony_client.sync_turn(
                session_id=session_id,
                contact_id=_contact_id,
                user_message=user_msg[:2000],
                assistant_message=assistant_msg[:2000],
                summary=summary[:1000],
            )
        except Exception as exc:
            logger.debug("sync_turn in on_session_end failed: %s", exc)

    ctx.register_hook("on_session_end", _on_session_end)

    # 6. Start WebSocket event subscriber (best-effort)
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
    logger.info(
        "Colony Autonomy Bridge available. Run '/colony autonomy enable' to "
        "activate background initiative handling."
    )
