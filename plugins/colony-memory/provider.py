"""Colony memory provider for Hermes.

Implements Hermes's MemoryProvider ABC to inject Colony's cognitive context
(commitments, affect, facts, patterns, world model) into Hermes conversations
and sync turns back for extraction.

Plugin directory: ~/.hermes/plugins/memory/colony/
Config key: memory.provider = "colony"
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Import the ABC if available (Hermes SDK installed).
try:
    from agent.memory_provider import MemoryProvider as _MemoryProviderABC
except ImportError:
    _MemoryProviderABC = object  # type: ignore[misc, assignment]  # fallback for standalone testing


# ---------------------------------------------------------------------------
# Colony tool schemas — what the LLM sees
# ---------------------------------------------------------------------------

_COLONY_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "colony_check_commitments",
        "description": (
            "Check active commitments for the current contact. "
            "Returns pending and overdue commitments with due dates."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contact_id": {
                    "type": "string",
                    "description": "Optional contact ID override",
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "overdue", "fulfilled", "all"],
                    "description": "Filter by status (default: pending)",
                    "default": "pending",
                },
            },
            "required": [],
        },
    },
    {
        "name": "colony_get_affect",
        "description": (
            "Get the current affect state (valence/arousal) for a contact. "
            "Returns mood trend and recent emotional events."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contact_id": {
                    "type": "string",
                    "description": "Optional contact ID override",
                },
            },
            "required": [],
        },
    },
    {
        "name": "colony_get_facts",
        "description": (
            "Retrieve shared facts about a contact. "
            "Returns known facts with confidence scores."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contact_id": {
                    "type": "string",
                    "description": "Optional contact ID override",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max facts to return (default: 10)",
                    "default": 10,
                },
            },
            "required": [],
        },
    },
    {
        "name": "colony_get_patterns",
        "description": (
            "Get detected behavioral patterns for a contact. "
            "Returns recurring patterns with frequency and confidence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "contact_id": {
                    "type": "string",
                    "description": "Optional contact ID override",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max patterns to return (default: 10)",
                    "default": 10,
                },
            },
            "required": [],
        },
    },
    {
        "name": "colony_write_memory",
        "description": (
            "Write a fact, preference, or insight to Colony's persistent memory. "
            "Use when you learn something worth remembering across sessions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The memory text to persist",
                },
                "kind": {
                    "type": "string",
                    "enum": ["preference", "fact", "goal", "insight", "commitment"],
                    "description": "Memory category",
                    "default": "fact",
                },
                "person_id": {
                    "type": "string",
                    "description": "Optional person this relates to",
                },
                "entities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Related entities",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags for categorization",
                },
                "confidence": {
                    "type": "number",
                    "description": "Confidence 0-1 (default: 0.8)",
                    "default": 0.8,
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "colony_list_goals",
        "description": (
            "List the user's goals with their status and progress. "
            "Can filter by status (active/completed/blocked)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
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
        "name": "colony_record_affect",
        "description": (
            "Record an affect event (emotional state) for a contact. "
            "Use when the user expresses emotion that should be tracked."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "valence": {
                    "type": "number",
                    "description": "Emotional valence -1 (negative) to +1 (positive)",
                    "minimum": -1,
                    "maximum": 1,
                },
                "arousal": {
                    "type": "number",
                    "description": "Arousal level 0 (calm) to 1 (excited)",
                    "minimum": 0,
                    "maximum": 1,
                },
                "source": {
                    "type": "string",
                    "description": "What triggered this affect (e.g. 'user_message', 'tool_result')",
                },
                "trigger": {
                    "type": "string",
                    "description": "Optional description of what triggered the emotion",
                },
            },
            "required": ["valence", "arousal"],
        },
    },
    {
        "name": "colony_search_memory",
        "description": (
            "Search Colony's memory graph for relevant context. "
            "Returns ranked memories with relevance scores."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default: 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    # v0.13.0 — Task queue tools
    {
        "name": "colony_list_pending_tasks",
        "description": (
            "List pending AGENT_ACTION jobs in the Colony task queue. "
            "Returns jobs waiting to be claimed or blocked awaiting approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max jobs to return (default: 10)",
                    "default": 10,
                },
            },
            "required": [],
        },
    },
    {
        "name": "colony_claim_task",
        "description": (
            "Claim an AGENT_ACTION job from the Colony task queue. "
            "Returns the job payload to execute, or empty if none available."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "colony_complete_task",
        "description": (
            "Report a completed job to Colony. "
            "Call after successfully executing a claimed task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "ID of the job to complete",
                },
                "output": {
                    "type": "object",
                    "description": "Result payload (arbitrary JSON)",
                },
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "colony_fail_task",
        "description": (
            "Report a failed job to Colony. "
            "Call when a claimed task cannot be completed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "ID of the job that failed",
                },
                "error": {
                    "type": "string",
                    "description": "Error message or reason",
                },
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "colony_heartbeat_task",
        "description": (
            "Send a progress heartbeat for a running job. "
            "Call periodically during long-running tasks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "ID of the running job",
                },
                "progress": {
                    "type": "number",
                    "description": "Progress 0.0—1.0",
                    "minimum": 0,
                    "maximum": 1,
                },
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "colony_approve_initiative",
        "description": (
            "Approve a blocked AGENT_ACTION initiative. "
            "Only the owner can call this. Transitions the linked job from BLOCKED to QUEUED."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "initiative_id": {
                    "type": "string",
                    "description": "ID of the initiative to approve",
                },
            },
            "required": ["initiative_id"],
        },
    },
    {
        "name": "colony_initiative_feedback",
        "description": (
            "Provide feedback on an initiative: acknowledge, dismiss, or snooze. "
            "Stops the initiative from being re-injected into context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "initiative_id": {
                    "type": "string",
                    "description": "ID of the initiative",
                },
                "action": {
                    "type": "string",
                    "enum": ["acknowledged", "dismissed", "snoozed"],
                    "description": "Feedback action",
                },
                "details": {
                    "type": "object",
                    "description": "Optional extra context (e.g. snooze duration)",
                },
            },
            "required": ["initiative_id", "action"],
        },
    },
]


class ColonyMemoryProvider(_MemoryProviderABC):
    """Colony memory provider for Hermes.

    Reads cognitive context from Colony's sidecar via /v1/host/context/assemble
    and injects it as prefetched memory. Syncs turns back to Colony for
    extraction of commitments, affect, and facts.

    Config (from ~/.hermes/config.yaml memory.config):
        url: Colony sidecar URL (default http://127.0.0.1:7777)
        api_key: Colony API key (or set COLONY_API_KEY env var)
        contact_id: Contact ID for context assembly (or set COLONY_MCP_CONTACT_ID)
    """

    def __init__(self, config: dict[str, Any] | None = None):
        if config is None:
            # Load from Hermes config.yaml if no config passed
            try:
                from hermes_cli.config import load_config, cfg_get
                hermes_config = load_config()
                config = cfg_get(hermes_config, "memory", "config", default={}) or {}
            except Exception:
                config = {}
        self.sidecar_url = config.get("url", os.environ.get("COLONY_URL", "http://127.0.0.1:7777"))
        raw_key = config.get("api_key", os.environ.get("COLONY_API_KEY", ""))
        # Resolve unexpanded env-var placeholders like ${COLONY_API_KEY}
        if raw_key and raw_key.startswith("${") and raw_key.endswith("}"):
            env_name = raw_key[2:-1]
            raw_key = os.environ.get(env_name, "")
        self._api_key = raw_key
        self._contact_id = config.get("contact_id", os.environ.get("COLONY_MCP_CONTACT_ID", "default"))
        self._session_id = ""
        self._cached_context: str = ""
        self._prefetch_ready = asyncio.Event()
        self._prefetch_ready.set()
        self._platform = "cli"
        self._async_client: Optional[httpx.AsyncClient] = None
        self._hermes_home = ""
        self._sync_thread: Optional[threading.Thread] = None
        # Phase 4: circuit breaker and diagnostics
        self._circuit_open_until: Optional[float] = None
        self._connection_failures = 0
        self._last_sync_attempt: Optional[str] = None
        self._last_sync_error: Optional[str] = None

    @property
    def name(self) -> str:
        return "colony"

    # -- Diagnostics ------------------------------------------------------------

    def get_diagnostics(self) -> dict:
        """Return provider health diagnostics for external monitoring."""
        return {
            "provider": "colony",
            "sidecar_url": self.sidecar_url,
            "contact_id": self._contact_id,
            "session_id": self._session_id,
            "last_sync_attempt": self._last_sync_attempt,
            "last_sync_error": self._last_sync_error,
            "circuit_open": self._is_circuit_open(),
            "connection_failures": self._connection_failures,
        }

    def _is_circuit_open(self) -> bool:
        if self._circuit_open_until is None:
            return False
        if datetime.now(timezone.utc).timestamp() > self._circuit_open_until:
            self._circuit_open_until = None
            self._connection_failures = 0
            return False
        return True

    def _record_connection_failure(self) -> None:
        self._connection_failures += 1
        if self._connection_failures >= 3:
            self._circuit_open_until = (datetime.now(timezone.utc) + timedelta(seconds=60)).timestamp()
            logger.warning("Colony: circuit breaker opened for 60s after %d failures", self._connection_failures)

    def _record_connection_success(self) -> None:
        if self._connection_failures > 0:
            logger.info("Colony: connection recovered, resetting failure count")
            self._connection_failures = 0
            self._circuit_open_until = None

    # -- Config schema (for hermes memory setup) --------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """Return config fields for the interactive setup wizard."""
        return [
            {
                "key": "url",
                "description": "Colony sidecar URL",
                "default": "http://127.0.0.1:7777",
            },
            {
                "key": "api_key",
                "description": "Colony API key (sk-colony-...)",
                "secret": True,
                "env_var": "COLONY_API_KEY",
            },
            {
                "key": "contact_id",
                "description": "Default contact ID for context assembly",
                "default": "default",
            },
        ]

    def save_config(self, values: dict, hermes_home: str) -> None:
        """Write non-secret config to the plugin's native location."""
        import json
        from pathlib import Path
        config_path = Path(hermes_home) / "colony-memory.json"
        config_path.write_text(json.dumps(values, indent=2))

    # -- Core lifecycle --------------------------------------------------------

    def is_available(self) -> bool:
        """Check if the Colony sidecar is reachable (sync, for startup checks)."""
        try:
            headers = self._headers()
            resp = httpx.get(f"{self.sidecar_url}/v1/host/health", headers=headers, timeout=3)
            return resp.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize for a session."""
        self._session_id = session_id
        self._platform = kwargs.get("platform", "cli")
        self._hermes_home = kwargs.get("hermes_home", "")
        if not self._api_key:
            logger.warning("Colony: COLONY_API_KEY not set — requests will fail if sidecar requires auth")
        logger.info("Colony memory provider initialized (session=%s, platform=%s, home=%s)",
                     session_id, self._platform, self._hermes_home)

    def system_prompt_block(self) -> str:
        """Return static context about Colony for the system prompt."""
        return (
            "Colony cognitive infrastructure is active. You have access to commitments, "
            "affect state, shared facts, patterns, and world model through Colony tools. "
            "Use colony_check_commitments, colony_list_goals, and colony_search_memory to stay informed. "
            "Use colony_write_memory to persist insights across sessions."
            "\n\n"
            "When evaluating temporal claims, always prefer the real current time (provided by the host) "
            "over any timestamps from Colony data. Colony stores event times; the host provides the "
            "reference frame. If data appears stale, say so — do not fabricate a narrative to make it seem current."
        )

    # -- Prefetch (context injection) ------------------------------------------

    async def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant context from Colony for the upcoming turn."""
        if self._cached_context:
            return self._cached_context

        try:
            client = self._get_async_client()
            headers = self._headers()
            resp = await client.post(
                f"{self.sidecar_url}/v1/host/context/assemble",
                headers=headers,
                json={
                    "identity": {"host_id": "hermes"},
                    "context": {
                        "session_id": session_id or self._session_id,
                        "contact_id": self._contact_id,
                    },
                    "incoming_message": {"role": "user", "content": query},
                    "include_initiatives": True,  # v0.13.0
                },
                timeout=10,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (401, 403):
                logger.warning("Colony prefetch auth failed (HTTP %d) — check COLONY_API_KEY", code)
            else:
                logger.debug("Colony prefetch failed: %s", exc)
            return ""
        except (httpx.HTTPError, OSError) as exc:
            logger.debug("Colony prefetch failed: %s", exc)
            return ""

        data = resp.json()
        sections = data.get("sections", [])
        if not sections:
            return ""

        return self._format_sections(sections)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Start a background prefetch for the next turn."""
        self._cached_context = ""
        self._prefetch_ready.clear()

        async def _fetch():
            try:
                self._cached_context = await self.prefetch(query, session_id=session_id)
            finally:
                self._prefetch_ready.set()

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_fetch())
        except RuntimeError:
            self._cached_context = asyncio.get_event_loop().run_until_complete(
                self.prefetch(query, session_id=session_id)
            )
            self._prefetch_ready.set()

    # -- Turn sync -------------------------------------------------------------

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Persist a completed turn to Colony for extraction.

        NON-BLOCKING: runs in a daemon thread per Hermes threading contract.
        """
        sid = session_id or self._session_id
        contact_id = self._contact_id
        url = self.sidecar_url
        headers = self._headers()
        self._last_sync_attempt = datetime.now(timezone.utc).isoformat()
        self._last_sync_error = None

        def _sync():
            if self._is_circuit_open():
                logger.warning("Colony turn sync skipped — circuit breaker open")
                return
            for attempt in range(3):
                try:
                    with httpx.Client(timeout=8) as client:
                        resp = client.post(
                            f"{url}/v1/host/turns/sync",
                            headers=headers,
                            json={
                                "identity": {"host_id": "hermes"},
                                "context": {
                                    "session_id": sid,
                                    "contact_id": contact_id,
                                },
                                "user_message": {"role": "user", "content": user_content},
                                "assistant_message": {"role": "assistant", "content": assistant_content},
                            },
                        )
                        resp.raise_for_status()
                        self._record_connection_success()
                        return
                except (httpx.ConnectError, OSError) as exc:
                    self._record_connection_failure()
                    self._last_sync_error = str(exc)
                    if self._is_circuit_open():
                        logger.warning("Colony turn sync circuit opened after connection failure")
                        return
                    if attempt < 2:
                        # Note: time.sleep blocks async event loop if called from async context.
                        # When refactoring to async, use await asyncio.sleep(0.5) instead.
                        import time
                        time.sleep(0.5)
                except httpx.HTTPStatusError as exc:
                    code = exc.response.status_code
                    if code in (401, 403):
                        logger.warning("Colony turn sync auth failed (HTTP %d)", code)
                    else:
                        logger.debug("Colony turn sync HTTP error: %s", exc)
                    return  # Don't retry or count toward breaker
                except Exception as exc:
                    self._last_sync_error = str(exc)
                    logger.debug("Colony turn sync unexpected error: %s", exc)
                    return  # Don't retry or count toward breaker

        # Join previous sync if still running (prevents pile-up)
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)
        self._sync_thread = threading.Thread(target=_sync, daemon=True)
        self._sync_thread.start()

    # -- Tool schemas ----------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return Colony tool schemas for the model."""
        return list(_COLONY_TOOL_SCHEMAS)

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """Handle a Colony tool call from the agent."""
        handler = getattr(self, f"_tool_{tool_name}", None)
        if handler is None:
            return json.dumps({"error": f"Unknown Colony tool: {tool_name}"})
        try:
            return handler(args)
        except Exception as exc:
            logger.warning("Colony tool %s failed: %s", tool_name, exc)
            return json.dumps({"error": f"Tool failed: {exc}"})

    # -- Tool handlers ---------------------------------------------------------

    def _tool_colony_check_commitments(self, args: dict) -> str:
        status = args.get("status", "pending")
        contact_id = args.get("contact_id", self._contact_id)
        try:
            with httpx.Client(timeout=5) as client:
                params = {"status_filter": status, "person_id": contact_id}
                resp = client.get(
                    f"{self.sidecar_url}/v1/host/commitments",
                    headers=self._headers(),
                    params=params,
                    timeout=5,
                )
                resp.raise_for_status()
                data = resp.json()
                return json.dumps(data)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_colony_get_affect(self, args: dict) -> str:
        contact_id = args.get("contact_id", self._contact_id)
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(
                    f"{self.sidecar_url}/v1/host/affect/{contact_id}",
                    headers=self._headers(),
                    timeout=5,
                )
                if resp.status_code == 404:
                    return json.dumps({"contact_id": contact_id, "current_valence": 0, "current_arousal": 0, "trend": "neutral", "event_count": 0})
                resp.raise_for_status()
                return json.dumps(resp.json())
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_colony_get_facts(self, args: dict) -> str:
        contact_id = args.get("contact_id", self._contact_id)
        limit = args.get("limit", 10)
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(
                    f"{self.sidecar_url}/v1/host/mind/facts/{contact_id}",
                    headers=self._headers(),
                    params={"limit": limit},
                    timeout=5,
                )
                resp.raise_for_status()
                return json.dumps(resp.json())
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_colony_get_patterns(self, args: dict) -> str:
        contact_id = args.get("contact_id", self._contact_id)
        limit = args.get("limit", 10)
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(
                    f"{self.sidecar_url}/v1/host/patterns/{contact_id}",
                    headers=self._headers(),
                    params={"limit": limit},
                    timeout=5,
                )
                resp.raise_for_status()
                return json.dumps(resp.json())
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_colony_write_memory(self, args: dict) -> str:
        try:
            with httpx.Client(timeout=5) as client:
                payload = {
                    "identity": {"host_id": "hermes"},
                    "context": {
                        "session_id": self._session_id,
                        "contact_id": self._contact_id,
                    },
                    "content": args["content"],
                    "type": args.get("kind", "fact"),
                    "person_id": args.get("person_id", self._contact_id),
                    "entities": args.get("entities", []),
                    "tags": args.get("tags", []),
                    "strength": args.get("confidence", 0.8),
                }
                resp = client.post(
                    f"{self.sidecar_url}/v1/host/memory/write",
                    headers=self._headers(),
                    json=payload,
                    timeout=5,
                )
                resp.raise_for_status()
                return json.dumps(resp.json())
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_colony_list_goals(self, args: dict) -> str:
        status = args.get("status", "active")
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(
                    f"{self.sidecar_url}/v1/host/goals",
                    headers=self._headers(),
                    params={"status_filter": status},
                    timeout=5,
                )
                resp.raise_for_status()
                return json.dumps(resp.json())
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_colony_record_affect(self, args: dict) -> str:
        try:
            with httpx.Client(timeout=5) as client:
                payload = {
                    "contact_id": args.get("contact_id", self._contact_id),
                    "valence": args["valence"],
                    "arousal": args["arousal"],
                    "source": args.get("source", "user_message"),
                    "trigger": args.get("trigger", ""),
                }
                resp = client.post(
                    f"{self.sidecar_url}/v1/host/affect/events",
                    headers=self._headers(),
                    json=payload,
                    timeout=5,
                )
                resp.raise_for_status()
                return json.dumps({"success": True})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_colony_search_memory(self, args: dict) -> str:
        try:
            with httpx.Client(timeout=5) as client:
                payload = {
                    "identity": {"host_id": "hermes"},
                    "query": args["query"],
                    "limit": args.get("limit", 5),
                }
                resp = client.post(
                    f"{self.sidecar_url}/v1/host/memory/search",
                    headers=self._headers(),
                    json=payload,
                    timeout=5,
                )
                resp.raise_for_status()
                return json.dumps(resp.json())
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # -- v0.13.0 task queue tool handlers --------------------------------------

    def _tool_colony_list_pending_tasks(self, args: dict) -> str:
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(
                    f"{self.sidecar_url}/v1/host/queue/jobs/pending",
                    headers=self._headers(),
                    params={"limit": args.get("limit", 10)},
                    timeout=5,
                )
                resp.raise_for_status()
                return json.dumps(resp.json())
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_colony_claim_task(self, args: dict) -> str:
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.post(
                    f"{self.sidecar_url}/v1/host/queue/jobs/claim",
                    headers=self._headers(),
                    json={},
                    timeout=5,
                )
                resp.raise_for_status()
                return json.dumps(resp.json())
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_colony_complete_task(self, args: dict) -> str:
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.post(
                    f"{self.sidecar_url}/v1/host/queue/jobs/{args['job_id']}/complete",
                    headers=self._headers(),
                    json={"output": args.get("output", {})},
                    timeout=5,
                )
                resp.raise_for_status()
                return json.dumps({"success": True})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_colony_fail_task(self, args: dict) -> str:
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.post(
                    f"{self.sidecar_url}/v1/host/queue/jobs/{args['job_id']}/fail",
                    headers=self._headers(),
                    json={"error": args.get("error", "unknown error")},
                    timeout=5,
                )
                resp.raise_for_status()
                return json.dumps({"success": True})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_colony_heartbeat_task(self, args: dict) -> str:
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.post(
                    f"{self.sidecar_url}/v1/host/queue/jobs/{args['job_id']}/heartbeat",
                    headers=self._headers(),
                    json={"progress": args.get("progress")},
                    timeout=5,
                )
                resp.raise_for_status()
                return json.dumps({"success": True})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_colony_approve_initiative(self, args: dict) -> str:
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.post(
                    f"{self.sidecar_url}/v1/host/initiatives/{args['initiative_id']}/respond",
                    headers=self._headers(),
                    json={"action": "approved"},
                    timeout=5,
                )
                resp.raise_for_status()
                return json.dumps({"success": True, "status": "approved"})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_colony_initiative_feedback(self, args: dict) -> str:
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.post(
                    f"{self.sidecar_url}/v1/host/initiatives/{args['initiative_id']}/respond",
                    headers=self._headers(),
                    json={
                        "action": args["action"],
                        "details": args.get("details"),
                    },
                    timeout=5,
                )
                resp.raise_for_status()
                return json.dumps({"success": True, "action": args["action"]})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # -- Optional hooks --------------------------------------------------------

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs,
    ) -> None:
        """Handle session rotation (/resume, /branch, /reset, /new, compression)."""
        if reset:
            self._cached_context = ""
            self._prefetch_ready.set()
        self._session_id = new_session_id
        logger.debug("Colony memory provider switched to session=%s (reset=%s)", new_session_id, reset)

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Called at the start of each turn."""
        logger.debug("Colony: turn %d started (session=%s)", turn_number, self._session_id)

    def on_memory_write(self, action: str, target: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Mirror built-in memory writes back to Colony."""
        metadata = metadata or {}
        kind = metadata.get("kind", "fact")
        try:
            with httpx.Client(timeout=3) as client:
                payload = {
                    "identity": {"host_id": "hermes"},
                    "context": {
                        "session_id": self._session_id,
                        "contact_id": self._contact_id,
                    },
                    "content": content,
                    "type": kind,
                    "person_id": self._contact_id,
                    "tags": ["hermes-memory-write", action, target],
                }
                client.post(
                    f"{self.sidecar_url}/v1/host/memory/write",
                    headers=self._headers(),
                    json=payload,
                    timeout=3,
                )
        except Exception as exc:
            logger.debug("Colony on_memory_write mirror failed: %s", exc)

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Extract insights before context compression discards old messages."""
        # Best-effort: fire a compressed turn sync so Colony sees the full history
        # before Hermes drops it. This ensures commitments/facts from early turns
        # are not lost.
        if len(messages) >= 4:
            try:
                user_msgs = [m for m in messages if m.get("role") == "user"]
                asst_msgs = [m for m in messages if m.get("role") == "assistant"]
                if user_msgs and asst_msgs:
                    summary = f"Compression summary: {len(messages)} messages"
                    # Fire lightweight signal ingest instead of full turn sync
                    with httpx.Client(timeout=3) as client:
                        payload = {
                            "identity": {"host_id": "hermes"},
                            "context": {
                                "session_id": self._session_id,
                                "contact_id": self._contact_id,
                            },
                            "signals": [
                                {
                                    "type": "compression",
                                    "data": {"message_count": len(messages), "summary": summary},
                                    "source": "hermes",
                                }
                            ],
                        }
                        client.post(
                            f"{self.sidecar_url}/v1/host/signals/ingest",
                            headers=self._headers(),
                            json=payload,
                            timeout=3,
                        )
            except Exception as exc:
                logger.debug("Colony on_pre_compress signal failed: %s", exc)
        return ""

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Flush any pending context at session end."""
        self._cached_context = ""
        # Best-effort final sync of the last exchange
        if messages:
            try:
                last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
                last_asst = next((m for m in reversed(messages) if m.get("role") == "assistant"), None)
                if last_user and last_asst:
                    self.sync_turn(
                        last_user.get("content", ""),
                        last_asst.get("content", ""),
                        session_id=self._session_id,
                    )
            except Exception:
                pass

    async def shutdown(self) -> None:
        """Clean up."""
        self._cached_context = ""
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=3.0)
        if self._async_client:
            await self._async_client.aclose()
            self._async_client = None

    # -- Internals -------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _get_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None or self._async_client.is_closed:
            self._async_client = httpx.AsyncClient()
        return self._async_client

    def _format_sections(self, sections: list[dict[str, Any]]) -> str:
        """Format Colony sections into a memory-context block."""
        parts = []
        for section in sections:
            header = section.get("title", section.get("id", "colony-context"))
            body = section.get("body", "")
            priority = section.get("priority", 50)
            parts.append(f"## {header} [priority {priority}]\n{body}")
        return "<memory-context>\n[Colony Cognitive Context]\n\n" + "\n\n".join(parts) + "\n</memory-context>"
