"""Colony memory provider for Hermes.

Implements Hermes's MemoryProvider ABC to inject Colony's cognitive context
(commitments, affect, facts, patterns, world model) into Hermes conversations
and sync turns back for extraction.

Plugin directory: ~/.hermes/plugins/memory/colony/
Config key: memory.provider = "colony"
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx
import re as _tre
import time as _ttime
from datetime import datetime as _tdt


def _humanize_secs(secs):
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"
    return f"{secs // 86400}d {(secs % 86400) // 3600}h"

logger = logging.getLogger(__name__)

# Import the ABC if available (Hermes SDK installed).
try:
    from agent.memory_provider import MemoryProvider as _MemoryProviderABC
except ImportError:
    _MemoryProviderABC = object  # type: ignore[misc, assignment]  # fallback for standalone testing


# ---------------------------------------------------------------------------
# Colony tool schemas — what the LLM sees
# ---------------------------------------------------------------------------

_READ_CONTEXT_TOOLS = frozenset({
    "colony_check_commitments",
    "colony_get_affect",
    "colony_get_facts",
    "colony_timeline",
})
_QUEUE_MUTATION_TOOLS = frozenset({
    "colony_claim_task",
    "colony_complete_task",
    "colony_fail_task",
    "colony_heartbeat_task",
})
_GENERAL_PLUGIN_MUTATION_TOOLS = frozenset({
    "colony_resolve_commitment",
    "colony_write_memory",
    "colony_record_affect",
    "colony_initiative_feedback",
}) | _QUEUE_MUTATION_TOOLS

_MEMORY_WORKER_SAFE_CAPABILITIES = frozenset({
    "agent_action",
    "agent_sync:v1",
    "filesystem:read",
    "hermes_run:v1",
    "memory:read",
    "reasoning",
    "web:read",
})
_MEMORY_WORKER_ROUTE_CAPABILITIES = frozenset({
    "agent_sync:v1", "hermes_run:v1",
})

# Public, stable names for deployment admission checks.  These describe the
# canonical coexistence posture; callers should prefer catalog_attestation()
# when they also need to bind the exact model-visible JSON schemas.
GENERAL_PLUGIN_READ_CONTEXT_TOOL_NAMES = tuple(sorted(_READ_CONTEXT_TOOLS))


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {
        "1", "true", "yes", "on", "enabled",
    }

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
        "name": "colony_resolve_commitment",
        "description": (
            "Resolve a commitment so reminders stop: mark it fulfilled (done), "
            "dismiss it as stale/no-longer-relevant (with a reason), or snooze "
            "it to a new due date. Use when the owner says something is done, "
            "stale, or should be ignored. Get the id from colony_check_commitments."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "commitment_id": {
                    "type": "string",
                    "description": "The commitment id to resolve",
                },
                "action": {
                    "type": "string",
                    "enum": ["fulfilled", "dismissed", "snoozed"],
                    "description": "fulfilled=done; dismissed=stale/ignore (give reason); snoozed=defer (give new_due_at)",
                },
                "reason": {
                    "type": "string",
                    "description": "Why (required for dismissed; recorded in metadata)",
                },
                "new_due_at": {
                    "type": "string",
                    "description": "ISO-8601 UTC datetime (required for snoozed)",
                },
            },
            "required": ["commitment_id", "action"],
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
                "claim_attempt_id": {
                    "type": "string",
                    "description": "Exact server claim attempt returned by colony_claim_task",
                },
            },
            "required": ["job_id", "claim_attempt_id"],
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
                "claim_attempt_id": {"type": "string"},
            },
            "required": ["job_id", "claim_attempt_id"],
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
                "claim_attempt_id": {"type": "string"},
            },
            "required": ["job_id", "claim_attempt_id"],
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
    # v0.21.0 — temporal timeline
    {
        "name": "colony_timeline",
        "description": (
            "Recall the agent's timeline of past events — conversations, outreach, "
            "initiatives, tasks — ordered by time. Use to answer 'what happened "
            "recently', 'what's been going on with <person>', 'what have I done "
            "since yesterday', or to ground yourself in recent history. Returns a "
            "human-readable digest plus structured events."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": "Window: relative ('6h','24h','7d','2w'), "
                                   "'today'/'yesterday', or an ISO date. Default '24h'.",
                    "default": "24h",
                },
                "contact_id": {
                    "type": "string",
                    "description": "Only events involving this contact (optional).",
                },
                "types": {
                    "type": "string",
                    "description": "Comma-separated event types to include, e.g. "
                                   "'conversation.turn,outreach.sent' (optional).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max events (default 50).",
                    "default": 50,
                },
            },
            "required": [],
        },
    },
]

GENERAL_PLUGIN_FORBIDDEN_TOOL_NAMES = tuple(sorted(
    {
        str(schema.get("name") or "")
        for schema in _COLONY_TOOL_SCHEMAS
        if str(schema.get("name") or "") not in _READ_CONTEXT_TOOLS
    }
    | {"colony_approve_initiative"}
))

_GENERAL_PLUGIN_SYSTEM_PROMPT = (
    "Colony cognitive context is active in read-only mode. The only Colony "
    "tools available on this surface are colony_check_commitments, "
    "colony_get_affect, colony_get_facts, and colony_timeline. Their person "
    "scope is bound to the current transport-attested participant; never ask "
    "for or invent a contact override. Direct tool calls are available only "
    "on the configured owner/system lane; guest turns use the scoped assembled "
    "context. Context may be withheld when participant "
    "or scoped-projection authority is unavailable. When evaluating temporal "
    "claims, prefer the host's current time over stored event timestamps, and "
    "state when data may be stale."
)


def _bound_read_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Remove model-selectable person authority from a read tool schema."""

    copied = json.loads(json.dumps(schema))
    parameters = copied.get("parameters")
    if isinstance(parameters, dict):
        properties = parameters.get("properties")
        if isinstance(properties, dict):
            properties.pop("contact_id", None)
            properties.pop("person_id", None)
        required = parameters.get("required")
        if isinstance(required, list):
            parameters["required"] = [
                item for item in required
                if item not in {"contact_id", "person_id"}
            ]
    return copied


def catalog_attestation() -> Dict[str, Any]:
    """Return the canonical general-plugin coexistence tool catalog."""

    visible_schemas = sorted(
        (
            _bound_read_schema(schema) for schema in _COLONY_TOOL_SCHEMAS
            if str(schema.get("name") or "") in _READ_CONTEXT_TOOLS
        ),
        key=lambda schema: str(schema.get("name") or ""),
    )
    encoded = json.dumps(
        visible_schemas,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return {
        "schema": "ColonyMemoryCatalogAttestationV1",
        "version": 1,
        "posture": {
            "COLONY_GENERAL_PLUGIN_ACTIVE": "1",
            "COLONY_MEMORY_WORKER_TOOLS": "0",
            "per_turn_sender_binding": "required",
            "query_session_sender_cache_binding": "required",
            "real_channel_resolution_failure": "empty_context",
            "default_contact_fallback": "attested_owner_system_lane_only",
            "guest_context_projection": (
                "server_attested_exact_viewer_and_p8_required"
            ),
            "guest_temporal_projection": "local_clock_only",
            "guest_model_read_tools": (
                "withheld_pending_scoped_tool_projections"
            ),
            "reply_thread_projection": (
                "disabled_pending_transport_attested_endpoint"
            ),
            "memory_write_hook": "disabled_in_general_plugin_mode",
            "pre_compress_write_hook": "disabled_in_general_plugin_mode",
        },
        "provider_governance_ready": True,
        "general_plugin_governance_ready": False,
        "general_plugin_follow_on_slice": "hermes-session-governance-v1",
        "general_plugin_known_blockers": [
            "global_proactive_event_injection",
            "shared_contact_fallback",
            "handler_context_dropped",
            "startup_llm_mutation",
        ],
        "model_visible_tool_names": list(
            GENERAL_PLUGIN_READ_CONTEXT_TOOL_NAMES
        ),
        "forbidden_tool_names": list(GENERAL_PLUGIN_FORBIDDEN_TOOL_NAMES),
        "model_visible_schema_sha256": hashlib.sha256(encoded).hexdigest(),
        "system_prompt_sha256": hashlib.sha256(
            _GENERAL_PLUGIN_SYSTEM_PROMPT.encode("utf-8")
        ).hexdigest(),
        "guest_context_runtime_prerequisite": {
            "readiness_endpoint": (
                "/v1/host/context/projection-readiness"
            ),
            "response_schema": "ContextProjectionAttestationV1",
            "response_version": 1,
            "viewer_person_id_must_match_turn_contact": True,
            "p8_modes": ["shadow", "live"],
            "assemble_response_attestation_required": True,
        },
    }


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
        # Prefetch-cache bookkeeping (COLONY_PREFETCH_QUERY_CHECK=1): remember
        # WHICH turn the background prefetch was for, so a cached context is
        # only consumed by the turn that queued it. Guarded by a Lock because
        # queue_prefetch's worker thread and prefetch() race on these fields.
        self._cache_lock = threading.Lock()
        self._cached_query: str = ""
        self._cached_session: str = ""
        self._cached_participant: str = ""
        self._cached_contact: str = ""
        self._stale_cache_misses = 0
        self._temporal_cache = (0.0, "")  # (monotonic ts, block)
        self._temporal_cache_contact = ""  # contact the cached block was fetched for
        self._handle_cache: dict[str, tuple] = {}  # "platform:sender" -> (monotonic ts, contact_id)
        self._handle_cache_lock = threading.Lock()
        self._handle_negative_cache: dict[
            str, tuple[float, str, int]
        ] = {}
        self._projection_lock = threading.Lock()
        self._projection_ready_contacts: set[str] = set()
        self._last_turn_started_at = 0.0
        self._turn_number = 0
        self._prev_turn_gap_secs = None
        self._prefetch_thread = None  # background sync prefetch (v0.3.0)
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
        self._turn_writer_mode = str(config.get(
            "turn_writer", os.environ.get("COLONY_MEMORY_TURN_WRITER", "auto")
        ) or "auto").strip().lower()
        self._turn_writer_skip_logged = False
        for binding_flag in (
            "COLONY_PREFETCH_TURN_CONTACT",
            "COLONY_PREFETCH_QUERY_CHECK",
        ):
            value = os.environ.get(binding_flag, "1").strip().lower()
            if value not in {"1", "true", "yes", "on", "enabled"}:
                raise RuntimeError(
                    f"{binding_flag} is mandatory and cannot be disabled"
                )

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
            "stale_cache_misses": self._stale_cache_misses,
            "turn_writer": "enabled" if self._turn_writer_enabled() else "read-only",
        }

    def _turn_writer_enabled(self) -> bool:
        """Use the fallback writer only when the general plugin is absent.

        ``COLONY_MEMORY_TURN_WRITER=enabled`` is an explicit standalone
        override; ``disabled`` forces read-only behavior. The default ``auto``
        follows the process-local marker set by the canonical general plugin.
        """
        if self._turn_writer_mode in ("enabled", "on", "true", "1"):
            return True
        if self._turn_writer_mode in ("disabled", "off", "false", "0"):
            return False
        return os.environ.get("COLONY_GENERAL_PLUGIN_ACTIVE", "") != "1"

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
        self._rw_touch_session(session_id)
        self._platform = kwargs.get("platform", "cli")
        self._hermes_home = kwargs.get("hermes_home", "")
        if not self._api_key:
            logger.warning("Colony: COLONY_API_KEY not set — requests will fail if sidecar requires auth")
        logger.info("Colony memory provider initialized (session=%s, platform=%s, home=%s)",
                     session_id, self._platform, self._hermes_home)

    def system_prompt_block(self) -> str:
        """Return static context about Colony for the system prompt."""
        return _GENERAL_PLUGIN_SYSTEM_PROMPT

    def _last_session_block(self) -> str:
        """Inject the rotating last-session handoff brief (where she left off before the overnight
        reset) so a daily session reset keeps continuity instead of amnesia. Fresh-only, fail-soft."""
        import time as _t
        p = os.path.expanduser("~/.hermes/.handoff_brief.md")
        try:
            if not os.path.exists(p) or _t.time() - os.path.getmtime(p) > 30 * 3600:
                return ""
            txt = open(p, encoding="utf-8").read().strip()
        except Exception:
            return ""
        if not txt:
            return ""
        if self._tool_available("colony_write_memory"):
            persistence = (
                "At the start of the session, fold anything still live (open commitments, "
                "threads, things you are waiting on) into durable memory with "
                "colony_write_memory so it persists. "
            )
        else:
            persistence = (
                "Conversation turns are persisted automatically by the canonical Colony "
                "turn writer. Use this brief to resume anything still live (open "
                "commitments, threads, things you are waiting on), and use only tools "
                "actually registered for this turn. "
            )
        return (
            "\n\n## Where you left off (last-session handoff)\n"
            "This is your rotating last-session store from before the overnight reset. Treat it as your "
            "own recent memory. " + persistence
            + "Do not re-announce it to the owner unprompted.\n\n" + txt
        )

    # -- Authoritative current time (pre_llm_call hook) ------------------------

    def _current_time_line(self) -> str:
        """Local, no-network current date/time in the agent's home timezone."""
        import json as _json
        from datetime import datetime, timezone as _tz
        try:
            from zoneinfo import ZoneInfo
        except Exception:
            ZoneInfo = None
        tz = os.environ.get("COLONY_AGENT_TIMEZONE", "")
        if not tz:
            # Read the agent timezone from the SAME file the sidecar writes. The
            # sidecar stores it at $COLONY_STATE_DIR/temporal.json (default
            # ~/.colony/data/temporal.json); the gateway process does not export
            # COLONY_STATE_DIR, so probe the known locations in order.
            _candidates = []
            _sd = os.environ.get("COLONY_STATE_DIR")
            if _sd:
                _candidates.append(os.path.join(_sd, "temporal.json"))
            _candidates += [
                os.path.expanduser("~/.colony/data/temporal.json"),
                os.path.expanduser("~/.colony/temporal.json"),
            ]
            for _c in _candidates:
                try:
                    tz = (_json.load(open(_c)).get("agent_timezone") or "")
                    if tz:
                        break
                except Exception:
                    continue
        now = datetime.now(_tz.utc)
        if tz and ZoneInfo is not None:
            try:
                now = now.astimezone(ZoneInfo(tz))
            except Exception:
                pass
        hm = now.strftime("%I:%M %p").lstrip("0")
        return f"{now.strftime('%A, %B %d, %Y')}, {hm} {now.strftime('%Z') or 'UTC'}"

    def inject_current_time(self, messages: list) -> list:
        """pre_llm_call hook: inject the authoritative current time as a system
        message so the model never anchors on the (cached, stale) session-start
        date in long-running sessions. Generic — any Colony agent."""
        try:
            line = self._current_time_line()
        except Exception:
            return messages
        note = {
            "role": "system",
            "content": (
                f"⏰ CURRENT DATE & TIME, right now: {line}. This is TODAY — greet and "
                "reason from THIS. Any 'Conversation started' date in your prompt is only "
                "when this long-running session began (often days ago), NOT today."
            ),
        }
        result = list(messages)
        if result and isinstance(result[-1], dict) and result[-1].get("role") == "user":
            result.insert(-1, note)
        else:
            result.append(note)
        return result

    def resolve_contact(self, platform: str, user_id: str) -> None:
        """Resolve the real Colony contact from the message sender so per-contact
        memory/affect/facts engage (instead of 'default'). Called from the
        pre_llm_call hook (the lifecycle hook that carries the sender). Cached per
        sender so it only hits the sidecar once per sender per session."""
        if not user_id:
            return
        cid = self._resolve_handle(platform, user_id)
        if cid:
            logger.debug(
                "Colony resolved turn contact %s for %s:%s",
                cid, platform, user_id,
            )

    # -- Prefetch (context injection) ------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant Colony context for the upcoming turn.

        SYNCHRONOUS by Hermes contract — MemoryManager.prefetch_all() calls this
        synchronously and expects a string. (This was previously an ``async def``,
        so prefetch_all received an un-awaited coroutine and silently dropped ALL
        injected context — memories, temporal, affect, facts. That is the
        "relevant info isn't injected live" bug.) If queue_prefetch() already
        fetched in the background for this turn, return that; otherwise fetch now.
        """
        contact_id = self._prefetch_contact()
        participant = self._turn_participant_key()
        effective_session = session_id or self._session_id
        if not contact_id:
            logger.warning(
                "Colony prefetch withheld: current turn has no attested "
                "participant binding"
            )
            return ""
        t = self._prefetch_thread
        if t is not None and t.is_alive():
            t.join(timeout=9.0)
        if self._prefetch_query_check_enabled():
            # Consume the cached context only when it was fetched for THIS
            # query+session; a leftover cache from an abandoned or concurrent
            # turn is stale and would inject the wrong turn's context.
            with self._cache_lock:
                cached = self._cached_context
                match = (cached
                         and self._cached_query == (query or "")
                         and self._cached_session == effective_session
                         and self._cached_participant == participant
                         and self._cached_contact == contact_id)
                if cached:
                    self._cached_context = ""  # one-shot per turn
                    self._cached_query = ""
                    self._cached_session = ""
                    self._cached_participant = ""
                    self._cached_contact = ""
                if cached and not match:
                    self._stale_cache_misses += 1
            if match:
                ctx = cached
            else:
                ctx = self._prefetch_sync(
                    query,
                    session_id=effective_session,
                    contact_id=contact_id,
                )
        elif self._cached_context:
            ctx = self._cached_context
            self._cached_context = ""  # one-shot per turn
        else:
            ctx = self._prefetch_sync(
                query, session_id=effective_session, contact_id=contact_id,
            )
        # Reply thread-window: when this turn replies to an earlier message,
        # inject the surrounding turns that are NOT already in live context.
        ctx = self._merge_context_block(
            ctx, self._reply_thread_window(
                query, session_id=effective_session,
            ))
        return self._with_fresh_temporal_sync(
            ctx, contact_id=contact_id,
        )

    # -- Per-turn temporal freshness (a returned context must never carry a ---
    # frozen "now": the cached/assembled Current Time section is stripped and
    # replaced with a live one every time context is handed to the host).
    _TEMPORAL_TTL_SECS = 15.0
    _TEMPORAL_SECTION_RE = _tre.compile(
        r"## Current Time \[priority \d+\]\n.*?(?=\n\n## |\n</memory-context>)",
        _tre.DOTALL,
    )

    def _local_temporal_block(self):
        now = _tdt.now().astimezone()
        lines = [f"Now: {now.strftime('%A %Y-%m-%d %H:%M %Z')} (host clock; sidecar temporal brief unavailable)."]
        gap = self._prev_turn_gap_secs
        if gap is not None and gap > 0:
            lines.append(f"Previous message in this conversation: {_humanize_secs(gap)} ago.")
        lines.append("^ This is the authoritative CURRENT date/time — this is NOW. Ignore any 'Conversation started' date in your system prompt.")
        return "## Current Time [priority 100]\n" + "\n".join(lines)

    @staticmethod
    def _prefetch_turn_contact_enabled() -> bool:
        """COLONY_PREFETCH_TURN_CONTACT=1 -> context prefetch resolves the
        contact PER TURN from Hermes' ContextVar sender (concurrency-safe),
        instead of the provider-wide self._contact_id that races across the
        sessions sharing one provider instance. Default 0 = legacy."""
        return True

    @staticmethod
    def _turn_sender_context() -> tuple[str, str, str]:
        try:
            from gateway.session_context import get_session_env

            return (
                (get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip().lower(),
                (get_session_env("HERMES_SESSION_USER_ID", "") or "").strip(),
                (get_session_env("HERMES_SESSION_CHAT_ID", "") or "").strip(),
            )
        except Exception:
            return "", "", ""

    def _turn_participant_key(self) -> str:
        platform, sender, chat = self._turn_sender_context()
        effective = platform or str(self._platform or "").strip().lower()
        return f"{effective}:{sender}:{chat}"

    def _default_contact_fallback_allowed(self, platform: str) -> bool:
        authority = os.environ.get(
            "COLONY_MEMORY_DEFAULT_CONTEXT_AUTHORITY", ""
        ).strip().lower()
        return bool(
            authority == "owner_system"
            and platform in {
                "cli", "internal", "system", "owner", "api", "worker", "cron",
            }
        )

    def _prefetch_contact(self) -> str:
        """Resolve one exact turn participant; never cross a real-channel miss."""
        platform, sender, chat = self._turn_sender_context()
        effective = platform or str(self._platform or "").strip().lower()
        internal_lane = effective in {
            "cli", "internal", "system", "owner", "api", "worker", "cron",
        }
        if sender or chat or not internal_lane:
            if not sender:
                return ""
            try:
                return self._resolve_handle(effective, sender) or ""
            except Exception as exc:
                logger.debug("Colony per-turn prefetch contact failed: %s", exc)
                return ""
        try:
            cid = self._turn_contact()
        except Exception as exc:
            logger.debug("Colony per-turn prefetch contact failed: %s", exc)
            cid = None
        if cid:
            return cid
        if self._default_contact_fallback_allowed(effective):
            return self._contact_id
        return ""

    def _internal_owner_lane(self, contact_id: str) -> bool:
        """Return whether the configured owner fallback is explicitly active."""

        platform, sender, chat = self._turn_sender_context()
        effective = platform or str(self._platform or "").strip().lower()
        return bool(
            contact_id
            and contact_id == self._contact_id
            and not sender
            and not chat
            and self._default_contact_fallback_allowed(effective)
        )

    @staticmethod
    def _projection_attestation_valid(
        value: Any,
        *,
        contact_id: str,
        require_scoped: bool,
        require_owner: bool = False,
    ) -> bool:
        if not isinstance(value, dict):
            return False
        if (
            value.get("schema") != "ContextProjectionAttestationV1"
            or value.get("version") != 1
            or value.get("viewer_attested") is not True
            or str(value.get("viewer_person_id") or "") != contact_id
        ):
            return False
        if require_owner and value.get("viewer_is_owner") is not True:
            return False
        if require_scoped and (
            value.get("scoped_projection_ready") is not True
            or value.get("p8_mode") not in {"shadow", "live"}
            or value.get("legacy_global_allowed") is not False
        ):
            return False
        return True

    def _projection_readiness_sync(self, contact_id: str) -> bool:
        """Preflight one exact guest viewer without querying context producers."""

        ready = False
        try:
            with httpx.Client(timeout=3) as client:
                response = client.get(
                    f"{self.sidecar_url}/v1/host/context/projection-readiness",
                    headers=self._headers(),
                    params={"contact_id": contact_id},
                    timeout=3,
                )
                response.raise_for_status()
                ready = self._projection_attestation_valid(
                    response.json(),
                    contact_id=contact_id,
                    require_scoped=True,
                )
        except Exception as exc:
            logger.debug("Colony scoped projection preflight failed: %s", exc)
        with self._projection_lock:
            if ready:
                self._projection_ready_contacts.add(contact_id)
            else:
                self._projection_ready_contacts.discard(contact_id)
        return ready

    def _fresh_temporal_block_sync(self, *, contact_id: Optional[str] = None):
        contact_id = contact_id or self._prefetch_contact()
        if not contact_id:
            return self._local_temporal_block()
        # Guest temporal context stays local-clock only. The assembled P8
        # projection may contain a scoped temporal section, but the legacy
        # temporal endpoint has no atomic request policy and must never be a
        # guest fallback.
        if contact_id != self._contact_id:
            return self._local_temporal_block()
        ts, cached = self._temporal_cache
        if cached and (_ttime.monotonic() - ts) < self._TEMPORAL_TTL_SECS and (
                not self._prefetch_turn_contact_enabled()
                or contact_id == self._temporal_cache_contact):
            return cached
        block = ""
        try:
            with httpx.Client(timeout=2.5) as client:
                resp = client.get(
                    f"{self.sidecar_url}/v1/host/context/temporal",
                    headers=self._headers(),
                    params={"contact_id": contact_id},
                )
                resp.raise_for_status()
                data = resp.json()
            body = data.get("body", "")
            if body:
                gap = self._prev_turn_gap_secs
                if gap is not None and gap > 0:
                    body += f"\nPrevious message in this conversation: {_humanize_secs(gap)} ago."
                block = f"## {data.get('title', 'Current Time')} [priority 100]\n{body}"
        except Exception as exc:
            logger.debug("Colony temporal brief fetch failed: %s", exc)
        if not block:
            block = self._local_temporal_block()
        self._temporal_cache = (_ttime.monotonic(), block)
        self._temporal_cache_contact = contact_id
        return block

    def _with_fresh_temporal_sync(
        self, context, *, contact_id: Optional[str] = None,
    ):
        fresh = self._fresh_temporal_block_sync(contact_id=contact_id)
        if not context:
            return ("<memory-context>\n[Colony Cognitive Context]\n\n" + fresh + "\n</memory-context>")
        stripped = self._TEMPORAL_SECTION_RE.sub("", context)
        marker = "[Colony Cognitive Context]\n"
        if marker in stripped:
            head, tail = stripped.split(marker, 1)
            return head + marker + "\n" + fresh + "\n\n" + tail.lstrip("\n")
        return fresh + "\n\n" + stripped

    # -- Reply thread-window (precise in-context predicate, heuristic fallback) --
    _RW_RECENT_HOURS = float(os.environ.get("COLONY_REPLY_WINDOW_RECENT_HOURS", "6"))
    _RW_MSGS = int(os.environ.get("COLONY_REPLY_WINDOW_MSGS", "5"))
    _RW_BUDGET = int(os.environ.get("COLONY_REPLY_WINDOW_BUDGET", "1200"))
    _RW_LOOKBACK = os.environ.get("COLONY_REPLY_WINDOW_LOOKBACK", "14d")
    _RW_STATE_MAX = 64

    def _rw_touch_session(self, session_id: str) -> None:
        """Record the start of a session's verbatim context window."""
        if not session_id:
            return
        import time as _time
        states = getattr(self, "_rw_state", None)
        if states is None:
            states = {}
            self._rw_state = states
        if session_id not in states:
            states[session_id] = {"start": _time.time(), "compress": 0.0}
            while len(states) > self._RW_STATE_MAX:
                states.pop(next(iter(states)))

    def _rw_mark_compressed(self, session_id: str) -> None:
        """Older turns just left the verbatim window: bump the cutoff."""
        import time as _time
        st = getattr(self, "_rw_state", {}).get(session_id or self._session_id)
        if st:
            st["compress"] = _time.time()

    def _reply_thread_window(self, query: str, session_id: str = "") -> str:
        """When the inbound turn replies to a message (bridge [[rc ...]] marker),
        fetch ~N timeline turns around the replied-to message and inject ONLY the
        ones not already in the live session context.

        IN-CONTEXT PREDICATE (per message): in-context iff it belongs to the
        CURRENT session's channel AND its timestamp > max(session_start_ts,
        last_compress_ts). Everything else (older than the window, pre-reset,
        compressed away, or from ANOTHER channel of the thread) is missing and
        gets injected, budget-capped, channel-labeled when cross-channel.

        FALLBACK: when session state is cold (provider restarted mid-session) or
        timestamps are unusable, degrade to the anchor-age heuristic (< 6h: skip;
        older: inject the whole window). Any failure returns "" (no block beats a
        broken turn)."""
        # Disabled until the sidecar exposes a transport-attested reply-event
        # projection. The legacy implementation searches a global timeline by
        # an attacker-controlled quote snippet and cannot be made guest-safe by
        # filtering its result after the query.
        return ""

        # Kept below for reference while the scoped endpoint is designed.
        import re as _re
        import time as _time
        from datetime import datetime as _dt

        def _ets(e):
            try:
                at = str(e.get("at") or "")
                return _dt.fromisoformat(at.replace("Z", "+00:00")).timestamp()
            except Exception:
                return None

        try:
            q = query or ""
            if "[[rc id=" not in q:
                return ""
            hm = _re.search(r'\[replying to [^:\]]+: "(.*?)"\]', q)
            snippet = (hm.group(1) if hm else "").strip().rstrip("\u2026").strip()
            if len(snippet) < 12:
                return ""  # media/very short quotes: too weak to match reliably
            channel = ""
            try:
                channel = self._resolve_channel_id()
            except Exception:
                pass
            with httpx.Client(timeout=5) as client:
                resp = client.get(
                    f"{self.sidecar_url}/v1/host/timeline",
                    headers=self._headers(),
                    params={"since": self._RW_LOOKBACK,
                            "types": "conversation.turn", "limit": 500},
                )
                resp.raise_for_status()
                events = (resp.json() or {}).get("events") or []

            def _norm(s):
                return _re.sub(r"\s+", " ", str(s or "")).casefold()

            needle = _norm(snippet)[:80]
            idx = None
            for i, e in enumerate(events):  # newest first
                if needle and needle in _norm((e.get("data") or {}).get("summary")):
                    idx = i
                    break
            if idx is None:
                return ""
            lo = max(0, idx - self._RW_MSGS)
            hi = min(len(events), idx + self._RW_MSGS + 1)
            window = list(reversed(events[lo:hi]))  # oldest first
            anchor = events[idx]

            st = getattr(self, "_rw_state", {}).get(session_id or self._session_id)
            if st and channel:
                # Precise predicate, per message.
                cutoff = max(st.get("start", 0.0), st.get("compress", 0.0))
                missing = []
                for e in window:
                    ts = _ets(e)
                    e_ch = ((e.get("data") or {}).get("channel_id") or "")
                    in_ctx = bool(ts and e_ch == channel and ts > cutoff)
                    if not in_ctx:
                        missing.append(e)
                mode = "predicate"
            else:
                # Cold state: anchor-age heuristic on the matched event.
                ts = _ets(anchor)
                if ts and (_time.time() - ts) < self._RW_RECENT_HOURS * 3600:
                    return ""  # recent anchor: assume it is in session context
                missing = window
                mode = "heuristic"
            if not missing:
                return ""
            lines, total = [], 0
            for e in missing:
                d = e.get("data") or {}
                s = _re.sub(r"\s+", " ", str(d.get("summary") or "")).strip()
                if not s:
                    continue
                if len(s) > 220:
                    s = s[:217] + "..."
                stamp = str(e.get("at") or "")[:16].replace("T", " ")
                e_ch = str(d.get("channel_id") or "")
                via = ""
                if e_ch and channel and e_ch != channel:
                    via = f" (via {e_ch.split(':', 1)[0]})"
                mark = "  <-- the replied-to message" if e is anchor else ""
                line = f"- [{stamp}]{via} {s}{mark}"
                if total + len(line) + 1 > self._RW_BUDGET:
                    break
                lines.append(line)
                total += len(line) + 1
            if not lines:
                return ""
            logger.info("reply thread-window injected (%d/%d turns, %s mode)",
                        len(lines), len(window), mode)
            return ("## Thread context around the replied-to message [priority 70]\n"
                    "This inbound message replies to an earlier message; conversation "
                    "turns NOT already in the live context:\n" + "\n".join(lines))
        except Exception as exc:
            logger.debug("reply thread-window skipped: %s", exc)
            return ""

    @staticmethod
    def _merge_context_block(ctx: str, block: str) -> str:
        if not block:
            return ctx
        if not ctx:
            return block
        if "</memory-context>" in ctx:
            return ctx.replace("</memory-context>", "\n\n" + block + "\n</memory-context>", 1)
        return ctx + "\n\n" + block

    def _prefetch_sync(
        self,
        query: str,
        *,
        session_id: str = "",
        contact_id: Optional[str] = None,
        internal_owner_lane: Optional[bool] = None,
    ) -> str:
        """Blocking /context/assemble call → formatted context string."""
        bound_contact = contact_id or self._prefetch_contact()
        if not bound_contact:
            return ""
        internal_owner = (
            self._internal_owner_lane(bound_contact)
            if internal_owner_lane is None else internal_owner_lane
        )
        guest = bound_contact != self._contact_id
        if guest and not self._projection_readiness_sync(bound_contact):
            logger.warning(
                "Colony guest context withheld: scoped P8 projection is not ready"
            )
            return ""
        try:
            with httpx.Client(timeout=10) as client:
                resp = client.post(
                    f"{self.sidecar_url}/v1/host/context/assemble",
                    headers=self._headers(),
                    json={
                        "identity": {"host_id": "hermes"},
                        "context": {
                            "session_id": session_id or self._session_id,
                            "contact_id": bound_contact,
                        },
                        "incoming_message": {"role": "user", "content": query},
                        "include_initiatives": True,
                        **({
                            "projection_policy": "scoped_viewer_required",
                        } if guest else {}),
                    },
                )
                resp.raise_for_status()
                data = resp.json()
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
        attestation = data.get("projection_attestation")
        if guest:
            attested = self._projection_attestation_valid(
                attestation,
                contact_id=bound_contact,
                require_scoped=True,
            )
        elif internal_owner:
            # Explicit owner/system fallback is the sole legacy carve-out.
            attested = True
        else:
            attested = self._projection_attestation_valid(
                attestation,
                contact_id=bound_contact,
                require_scoped=False,
                require_owner=True,
            )
        if not attested:
            logger.warning(
                "Colony context withheld: response viewer attestation is invalid"
            )
            return ""
        sections = data.get("sections", [])
        return self._format_sections(sections) if sections else ""

    @staticmethod
    def _prefetch_query_check_enabled() -> bool:
        """COLONY_PREFETCH_QUERY_CHECK=1 -> consume the prefetch cache only when
        it matches the current query+session (default 0 = legacy consume-any)."""
        return True

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Kick off a background (thread) prefetch for the upcoming turn so the
        synchronous prefetch() can return instantly with the cached result."""
        contact_id = self._prefetch_contact()
        participant = self._turn_participant_key()
        effective_session = session_id or self._session_id
        internal_owner_lane = self._internal_owner_lane(contact_id)
        with self._cache_lock:
            self._cached_context = ""
            self._cached_query = ""
            self._cached_session = ""
            self._cached_participant = ""
            self._cached_contact = ""

        if not contact_id:
            self._prefetch_thread = None
            return

        def _bg():
            try:
                ctx = self._prefetch_sync(
                    query,
                    session_id=effective_session,
                    contact_id=contact_id,
                    internal_owner_lane=internal_owner_lane,
                )
            except Exception:
                ctx = ""
            with self._cache_lock:
                self._cached_context = ctx
                self._cached_query = query or ""
                self._cached_session = effective_session
                self._cached_participant = participant
                self._cached_contact = contact_id

        t = threading.Thread(target=_bg, daemon=True)
        self._prefetch_thread = t
        t.start()

    # -- Turn sync -------------------------------------------------------------

    def _resolve_channel_id(self) -> str:
        """Current conversation key 'platform:chat_id' from Hermes' per-turn session context
        (ContextVar-backed -> concurrency-safe). Empty when not inside a platform turn."""
        plat, _sender, cid = self._turn_sender_context()
        if plat and cid:
            return "%s:%s" % (plat, cid)
        return ""

    _HANDLE_CACHE_TTL_SECS = 60.0
    _HANDLE_CACHE_MAX = 256

    def _resolve_handle(self, platform: str, sender: str) -> Optional[str]:
        """Resolve a gateway sender handle -> Colony contact_id (None if unknown),
        auto-provisioning unknown real senders (create=true). Positive results are
        TTL-cached (60s) so per-turn resolution (sync + prefetch) does not hit
        /contacts/resolve on every call; failures are never cached, so a
        transient outage retries on the next turn."""
        if not sender:
            return None
        key = f"{platform}:{sender}"
        now = _ttime.monotonic()
        with self._handle_cache_lock:
            hit = self._handle_cache.get(key)
            if hit is not None:
                ts, cid = hit
                if (now - ts) < self._HANDLE_CACHE_TTL_SECS:
                    return cid
                self._handle_cache.pop(key, None)
            miss = self._handle_negative_cache.get(key)
            if miss is not None:
                ts, session_id, turn_number = miss
                if (
                    (now - ts) < 5.0
                    and session_id == self._session_id
                    and turn_number == self._turn_number
                ):
                    return None
                self._handle_negative_cache.pop(key, None)
        resolved: Optional[str] = None
        try:
            with httpx.Client(timeout=4) as client:
                resp = client.get(
                    f"{self.sidecar_url}/v1/host/contacts/resolve",
                    headers=self._headers(),
                    params={"gateway": platform or "", "address": sender, "create": "true"},
                )
                if resp.status_code == 200:
                    cid = (resp.json() or {}).get("contact_id")
                    if cid:
                        resolved = str(cid)
        except Exception as exc:
            logger.debug("Colony resolve_handle failed: %s", exc)
        with self._handle_cache_lock:
            if resolved:
                while len(self._handle_cache) >= self._HANDLE_CACHE_MAX:
                    self._handle_cache.pop(next(iter(self._handle_cache)))
                self._handle_cache[key] = (_ttime.monotonic(), resolved)
                self._handle_negative_cache.pop(key, None)
            else:
                while len(self._handle_negative_cache) >= self._HANDLE_CACHE_MAX:
                    self._handle_negative_cache.pop(
                        next(iter(self._handle_negative_cache))
                    )
                self._handle_negative_cache[key] = (
                    _ttime.monotonic(), self._session_id, self._turn_number,
                )
        return resolved

    def _turn_contact(self) -> Optional[str]:
        """The REAL contact for THIS turn, resolved per-turn from Hermes' ContextVar sender
        (concurrency-safe — unlike the single shared self._contact_id, which races across the
        WhatsApp/iMessage/SMS/RCS/voice/worker sessions that share one provider instance). Returns a
        contact_id, or None when there is no resolvable human participant on this turn."""
        platform, sender, _chat = self._turn_sender_context()
        return self._resolve_handle(platform, sender) if sender else None

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        turn_id: str = "",
    ) -> None:
        """Persist a completed turn to Colony for extraction.

        NON-BLOCKING: runs in a daemon thread per Hermes threading contract.
        """
        if not self._turn_writer_enabled():
            if not self._turn_writer_skip_logged:
                logger.info(
                    "Colony memory provider is read/context-only; "
                    "the general Colony plugin owns turn ingestion"
                )
                self._turn_writer_skip_logged = True
            return
        sid = session_id or self._session_id
        turn_platform, turn_sender, turn_chat = self._turn_sender_context()
        channel_id = (
            f"{turn_platform}:{turn_chat}"
            if turn_platform and turn_chat else ""
        )
        contact_id = (
            self._resolve_handle(turn_platform, turn_sender)
            if turn_sender else None
        )
        if turn_sender and not contact_id:
            logger.warning(
                "Colony sync_turn withheld: real-channel sender did not "
                "resolve to an exact contact"
            )
            return
        if contact_id:
            pass
        elif turn_chat or turn_platform not in {
            "", "cli", "internal", "system", "owner", "api", "worker", "cron",
        }:
            logger.warning(
                "Colony sync_turn withheld: channel has no exact sender binding"
            )
            return
        elif self._default_contact_fallback_allowed(
            turn_platform or str(self._platform or "").strip().lower()
        ):
            contact_id = self._contact_id
        else:
            logger.debug("Colony sync_turn skipped: no participant + no conversation context (system/self turn)")
            return
        # Raw sender identity rides along so the sidecar's ParticipantResolver
        # is AUTHORITATIVE (docs/RELATIONSHIPS.md): group speakers attribute to
        # the real person server-side (shadow contacts for strangers) even
        # when the client-side handle resolve above missed.
        sender = None
        if turn_sender:
            sender = {
                "platform": turn_platform or "unknown",
                "user_id": turn_sender,
                "display_name": "",
                "group_id": (
                    turn_chat if turn_chat and turn_chat != turn_sender else ""
                ),
            }
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
                        payload = {
                            "identity": {"host_id": "hermes"},
                            "context": {
                                "session_id": sid,
                                "contact_id": contact_id,
                                "channel_id": channel_id,
                                **({"turn_id": turn_id} if turn_id else {}),
                            },
                            **({"sender": sender} if sender else {}),
                            "user_message": {"role": "user", "content": user_content},
                            "assistant_message": {"role": "assistant", "content": assistant_content},
                        }
                        if turn_id:
                            resp = client.put(
                                f"{url}/v2/host/turns/{quote(turn_id, safe='')}",
                                headers=headers,
                                json=payload,
                            )
                        else:
                            resp = client.post(
                                f"{url}/v1/host/turns/sync",
                                headers=headers,
                                json=payload,
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

    def _tool_available(self, tool_name: str) -> bool:
        """Return the model-visible/dispatchable posture for one tool.

        The general plugin owns all canonical writes.  When it is active this
        memory provider is intentionally a read/context adapter only.  Queue
        mutation is additionally opt-in for standalone installs, and owner
        approval is never a model authority on this surface.
        """
        if tool_name == "colony_approve_initiative":
            return False
        if tool_name == "colony_claim_task" and os.environ.get(
            "COLONY_AGENT_JOB_CLAIMS_ENABLED", "true"
        ).strip().lower() not in {"1", "true", "yes", "on"}:
            return False
        if _env_true("COLONY_GENERAL_PLUGIN_ACTIVE"):
            return tool_name in _READ_CONTEXT_TOOLS
        if tool_name in _QUEUE_MUTATION_TOOLS:
            return _env_true("COLONY_MEMORY_WORKER_TOOLS")
        return True

    def _mutation_denial(self, tool_name: str) -> Optional[str]:
        if tool_name == "colony_approve_initiative":
            return "initiative approval is operator-only"
        if tool_name == "colony_claim_task" and os.environ.get(
            "COLONY_AGENT_JOB_CLAIMS_ENABLED", "true"
        ).strip().lower() not in {"1", "true", "yes", "on"}:
            return "agent job claims are disabled"
        if tool_name in _QUEUE_MUTATION_TOOLS and not self._tool_available(tool_name):
            return "colony worker tools are disabled"
        if (
            tool_name in _GENERAL_PLUGIN_MUTATION_TOOLS
            and not self._tool_available(tool_name)
        ):
            return "Colony memory provider is read/context-only"
        return None

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return Colony tool schemas for the model."""
        schemas = []
        for schema in _COLONY_TOOL_SCHEMAS:
            name = str(schema.get("name") or "")
            if not self._tool_available(name):
                continue
            schemas.append(
                _bound_read_schema(schema)
                if name in _READ_CONTEXT_TOOLS else schema
            )
        return schemas

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """Handle a Colony tool call from the agent."""
        if not self._tool_available(tool_name):
            return json.dumps({
                "error": "Colony tool is not available in this mode",
            })
        if tool_name in _READ_CONTEXT_TOOLS:
            bound_contact = self._prefetch_contact()
            if not bound_contact:
                return json.dumps({
                    "error": (
                        "Colony read context withheld: no attested turn "
                        "participant binding"
                    ),
                })
            supplied = str(
                args.get("contact_id") or args.get("person_id") or ""
            ).strip()
            if supplied and supplied != bound_contact:
                return json.dumps({
                    "error": "contact override exceeds turn authority",
                })
            if bound_contact != self._contact_id:
                return json.dumps({
                    "error": (
                        "Colony direct read tool withheld: guest-scoped tool "
                        "projections are not available; use assembled context"
                    ),
                })
            args = dict(args)
            args["contact_id"] = bound_contact
            args["person_id"] = bound_contact
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

    def _tool_colony_resolve_commitment(self, args: dict) -> str:
        denial = self._mutation_denial("colony_resolve_commitment")
        if denial:
            return json.dumps({"error": denial})
        commitment_id = args.get("commitment_id", "")
        action = args.get("action", "")
        reason = args.get("reason", "")
        if not commitment_id or action not in ("fulfilled", "dismissed", "snoozed"):
            return json.dumps({"error": "commitment_id and a valid action are required"})
        body: dict = {}
        now_iso = datetime.now(timezone.utc).isoformat()
        if action == "fulfilled":
            body = {"status": "fulfilled", "fulfilled_at": now_iso,
                    "metadata": {"resolved_by": "agent", "resolved_at": now_iso,
                                 "note": reason or "marked done"}}
        elif action == "dismissed":
            if not reason:
                return json.dumps({"error": "reason is required to dismiss"})
            body = {"status": "fulfilled", "fulfilled_at": now_iso,
                    "metadata": {"resolved_by": "agent", "resolved_at": now_iso,
                                 "dismissed": True, "reason": reason}}
        elif action == "snoozed":
            new_due = args.get("new_due_at", "")
            if not new_due:
                return json.dumps({"error": "new_due_at is required to snooze"})
            body = {"due_at": new_due,
                    "metadata": {"snoozed_by": "agent", "snoozed_at": now_iso,
                                 "note": reason or ""}}
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.patch(
                    f"{self.sidecar_url}/v1/host/commitments/{commitment_id}",
                    headers=self._headers(),
                    json=body,
                    timeout=5,
                )
                resp.raise_for_status()
                return json.dumps({"ok": True, "action": action,
                                   "commitment": resp.json()})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_colony_get_affect(self, args: dict) -> str:
        contact_id = args.get("contact_id", self._contact_id)
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(
                    f"{self.sidecar_url}/v1/host/affect/state/{contact_id}",
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
                    f"{self.sidecar_url}/v1/host/mind/facts",
                    headers=self._headers(),
                    params={"contact_id": contact_id, "limit": limit},
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
                    f"{self.sidecar_url}/v1/host/patterns",
                    headers=self._headers(),
                    params={"limit": limit},
                    timeout=5,
                )
                resp.raise_for_status()
                return json.dumps(resp.json())
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_colony_write_memory(self, args: dict) -> str:
        denial = self._mutation_denial("colony_write_memory")
        if denial:
            return json.dumps({"error": denial})
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
        denial = self._mutation_denial("colony_record_affect")
        if denial:
            return json.dumps({"error": denial})
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

    def _tool_colony_timeline(self, args: dict) -> str:
        params = {"since": args.get("since", "24h"), "limit": args.get("limit", 50)}
        if args.get("contact_id"):
            params["contact_id"] = args["contact_id"]
        if args.get("types"):
            params["types"] = args["types"]
        try:
            with httpx.Client(timeout=8) as client:
                resp = client.get(
                    f"{self.sidecar_url}/v1/host/timeline",
                    headers=self._headers(),
                    params=params,
                    timeout=8,
                )
                resp.raise_for_status()
                data = resp.json()
                # Return the agent-friendly digest up front, plus structured events.
                return json.dumps({
                    "digest": data.get("digest", ""),
                    "count": data.get("count", 0),
                    "since": data.get("since"),
                    "events": data.get("events", []),
                })
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # -- v0.13.0 task queue tool handlers --------------------------------------

    def _tool_colony_list_pending_tasks(self, args: dict) -> str:
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.get(
                    f"{self.sidecar_url}/v1/host/queue/jobs/pending",
                    headers=self._headers(),
                    params={
                        "limit": args.get("limit", 10),
                        "task_type": "agent_action",
                    },
                    timeout=5,
                )
                resp.raise_for_status()
                return json.dumps(resp.json())
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_colony_claim_task(self, args: dict) -> str:
        denial = self._mutation_denial("colony_claim_task")
        if denial:
            return json.dumps({"error": denial})
        try:
            worker_id = (
                os.environ.get("COLONY_MEMORY_WORKER_NODE_ID", "").strip()
                or os.environ.get(
                    "COLONY_WORKER_NODE_ID", "colony-memory-worker"
                ).strip()
                or "colony-memory-worker"
            )
            capabilities = [
                item.strip()
                for item in os.environ.get(
                    "COLONY_MEMORY_WORKER_CAPABILITIES",
                    "agent_action,agent_sync:v1,memory:read,reasoning",
                ).split(",")
                if item.strip() in _MEMORY_WORKER_SAFE_CAPABILITIES
            ]
            if not (
                set(capabilities) & _MEMORY_WORKER_ROUTE_CAPABILITIES
            ):
                raise RuntimeError(
                    "COLONY_MEMORY_WORKER_CAPABILITIES must include a safe "
                    "agent_sync:v1 or hermes_run:v1 route"
                )
            with httpx.Client(timeout=5) as client:
                resp = client.post(
                    f"{self.sidecar_url}/v1/host/queue/jobs/claim",
                    headers=self._headers(),
                    json={
                        "node_id": worker_id,
                        "capabilities": capabilities,
                        "job_types": ["agent_action"],
                        "max_concurrent": 1,
                    },
                    timeout=5,
                )
                resp.raise_for_status()
                job = resp.json()
                if not job:
                    return json.dumps(job)
                if not isinstance(job, dict) or not (job.get("job_id") or job.get("id")):
                    raise RuntimeError("claim response is invalid")
                job_id = job.get("job_id") or job.get("id")
                claim_attempt_id = job.get("claim_attempt_id")
                try:
                    started = client.post(
                        f"{self.sidecar_url}/v1/host/queue/jobs/{job_id}/start",
                        headers=self._headers(),
                        json={"claim_attempt_id": claim_attempt_id},
                        timeout=5,
                    )
                    started.raise_for_status()
                    started_body = started.json()
                    if (
                        not isinstance(started_body, dict)
                        or started_body.get("success") is not True
                    ):
                        raise RuntimeError("start transition was not accepted")
                except Exception:
                    try:
                        client.post(
                            f"{self.sidecar_url}/v1/host/queue/jobs/{job_id}/release",
                            headers=self._headers(),
                            json={"claim_attempt_id": claim_attempt_id},
                            timeout=5,
                        )
                    except Exception:
                        pass
                    raise
                return json.dumps(job)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_colony_complete_task(self, args: dict) -> str:
        denial = self._mutation_denial("colony_complete_task")
        if denial:
            return json.dumps({"error": denial})
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.post(
                    f"{self.sidecar_url}/v1/host/queue/jobs/{args['job_id']}/complete",
                    headers=self._headers(),
                    json={
                        "output": args.get("output", {}),
                        "claim_attempt_id": args.get("claim_attempt_id"),
                    },
                    timeout=5,
                )
                resp.raise_for_status()
                result = resp.json()
                if not isinstance(result, dict):
                    raise RuntimeError("completion response is invalid")
                semantic_success = (
                    result.get("success") is True
                    and result.get("job_status") == "completed"
                    and result.get("governor_outcome") == "success"
                )
                if not semantic_success:
                    return json.dumps({
                        **result,
                        "success": False,
                        "error": "job did not reach verified completed state",
                    })
                return json.dumps(result)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_colony_fail_task(self, args: dict) -> str:
        denial = self._mutation_denial("colony_fail_task")
        if denial:
            return json.dumps({"error": denial})
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.post(
                    f"{self.sidecar_url}/v1/host/queue/jobs/{args['job_id']}/fail",
                    headers=self._headers(),
                    json={
                        "error": args.get("error", "unknown error"),
                        "claim_attempt_id": args.get("claim_attempt_id"),
                    },
                    timeout=5,
                )
                resp.raise_for_status()
                result = resp.json()
                return json.dumps(result if isinstance(result, dict) else {
                    "success": True,
                })
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_colony_heartbeat_task(self, args: dict) -> str:
        denial = self._mutation_denial("colony_heartbeat_task")
        if denial:
            return json.dumps({"error": denial})
        try:
            with httpx.Client(timeout=5) as client:
                resp = client.post(
                    f"{self.sidecar_url}/v1/host/queue/jobs/{args['job_id']}/heartbeat",
                    headers=self._headers(),
                    json={
                        "progress": args.get("progress"),
                        "claim_attempt_id": args.get("claim_attempt_id"),
                    },
                    timeout=5,
                )
                resp.raise_for_status()
                return json.dumps({"success": True})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    def _tool_colony_approve_initiative(self, args: dict) -> str:
        return json.dumps({"error": "initiative approval is operator-only"})

    def _tool_colony_initiative_feedback(self, args: dict) -> str:
        denial = self._mutation_denial("colony_initiative_feedback")
        if denial:
            return json.dumps({"error": denial})
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
            with self._cache_lock:
                self._cached_context = ""
                self._cached_query = ""
                self._cached_session = ""
                self._cached_participant = ""
                self._cached_contact = ""
            self._prefetch_ready.set()
        self._session_id = new_session_id
        self._rw_touch_session(new_session_id)
        logger.debug("Colony memory provider switched to session=%s (reset=%s)", new_session_id, reset)

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Called at the start of each turn."""
        self._turn_number = int(turn_number or 0)
        with self._handle_cache_lock:
            self._handle_negative_cache.clear()
        _now = _ttime.time()
        if self._last_turn_started_at:
            self._prev_turn_gap_secs = _now - self._last_turn_started_at
        self._last_turn_started_at = _now
        logger.debug("Colony: turn %d started (session=%s)", turn_number, self._session_id)

    def on_memory_write(self, action: str, target: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Mirror built-in memory writes back to Colony."""
        if not self._turn_writer_enabled():
            return
        contact_id = self._prefetch_contact()
        if not contact_id:
            logger.debug(
                "Colony on_memory_write skipped: no exact turn participant"
            )
            return
        metadata = metadata or {}
        kind = metadata.get("kind", "fact")
        try:
            with httpx.Client(timeout=3) as client:
                payload = {
                    "identity": {"host_id": "hermes"},
                    "context": {
                        "session_id": self._session_id,
                        "contact_id": contact_id,
                    },
                    "content": content,
                    "type": kind,
                    "person_id": contact_id,
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
        self._rw_mark_compressed(self._session_id)
        if not self._turn_writer_enabled():
            return ""
        contact_id = self._prefetch_contact()
        if not contact_id:
            logger.debug(
                "Colony on_pre_compress skipped: no exact turn participant"
            )
            return ""
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
                                "contact_id": contact_id,
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
        with self._cache_lock:
            self._cached_context = ""
            self._cached_query = ""
            self._cached_session = ""
            self._cached_participant = ""
            self._cached_contact = ""
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
                        turn_id=(
                            f"hermes:{self._session_id}:{self._turn_number}"
                            if self._session_id and self._turn_number > 0 else ""
                        ),
                    )
            except Exception:
                pass

    def shutdown(self) -> None:
        """Clean up. SYNCHRONOUS by Hermes contract (MemoryManager calls this
        synchronously; an async def here was never awaited)."""
        with self._cache_lock:
            self._cached_context = ""
            self._cached_query = ""
            self._cached_session = ""
            self._cached_participant = ""
            self._cached_contact = ""
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=3.0)
        t = self._prefetch_thread
        if t is not None and t.is_alive():
            t.join(timeout=3.0)
        # _async_client (if ever created) closes on GC; nothing to await here.
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
        return ("<memory-context>\n[My own memory & awareness — what I already know going\ninto this turn. This is me, not an external system; read it first and never re-ask what\nis here.]\n\n" + "\n\n".join(parts) + "\n</memory-context>")
