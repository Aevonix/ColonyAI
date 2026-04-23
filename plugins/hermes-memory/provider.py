"""Colony memory provider for Hermes.

Implements Hermes's MemoryProvider ABC to inject Colony's cognitive context
(commitments, affect, facts, patterns, world model) into Hermes conversations
and sync turns back for extraction.

Plugin directory: ~/.hermes/plugins/memory/colony/
Config key: memory.provider = "colony"
"""

import logging
import os
import threading
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class ColonyMemoryProvider:
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
        config = config or {}
        self.sidecar_url = config.get("url", os.environ.get("COLONY_URL", "http://127.0.0.1:7777"))
        self._api_key = config.get("api_key", os.environ.get("COLONY_API_KEY", ""))
        self._contact_id = config.get("contact_id", os.environ.get("COLONY_MCP_CONTACT_ID", "default"))
        self._session_id = ""
        self._cached_context: str = ""
        self._prefetch_thread: Optional[threading.Thread] = None
        self._platform = "cli"

    @property
    def name(self) -> str:
        return "colony"

    # -- Core lifecycle -------------------------------------------------------

    def is_available(self) -> bool:
        """Check if the Colony sidecar is reachable."""
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
        hermes_home = kwargs.get("hermes_home", "")
        logger.info("Colony memory provider initialized (session=%s, platform=%s, home=%s)",
                     session_id, self._platform, hermes_home)

    def system_prompt_block(self) -> str:
        """Return static context about Colony for the system prompt."""
        return ("Colony cognitive infrastructure is active. You have access to commitments, "
                "affect state, shared facts, patterns, and world model through Colony tools. "
                "Use colony_check_commitments and colony_get_context to stay informed.")

    # -- Prefetch (context injection) -----------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant context from Colony for the upcoming turn.

        Called by MemoryManager before each API call. Returns formatted
        context text or empty string if unavailable.
        """
        if self._cached_context:
            return self._cached_context

        try:
            headers = self._headers()
            resp = httpx.post(
                f"{self.sidecar_url}/v1/host/context/assemble",
                headers=headers,
                json={
                    "identity": {"host_id": "hermes"},
                    "context": {
                        "session_id": session_id or self._session_id,
                        "contact_id": self._contact_id,
                    },
                    "incoming_message": {"role": "user", "content": query},
                },
                timeout=10,
            )
            resp.raise_for_status()
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

        def _fetch():
            self._cached_context = self.prefetch(query, session_id=session_id)

        self._prefetch_thread = threading.Thread(target=_fetch, daemon=True)
        self._prefetch_thread.start()

    # -- Turn sync ------------------------------------------------------------

    def sync_turn(self, user_msg: str, assistant_response: str) -> None:
        """Sync a completed turn to Colony for extraction.

        Fires POST /v1/host/turns/sync so Colony can extract commitments,
        affect, facts, and patterns from the conversation.
        """
        try:
            headers = self._headers()
            httpx.post(
                f"{self.sidecar_url}/v1/host/turns/sync",
                headers=headers,
                json={
                    "identity": {"host_id": "hermes"},
                    "context": {
                        "session_id": self._session_id,
                        "contact_id": self._contact_id,
                    },
                    "user_message": {"role": "user", "content": user_msg},
                    "assistant_message": {"role": "assistant", "content": assistant_response},
                },
                timeout=5,
            )
        except (httpx.HTTPError, OSError) as exc:
            logger.debug("Colony turn sync failed: %s", exc)

    # -- Tool schemas (optional) ----------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return tool schemas for Colony MCP tools.

        These mirror the MCP tools but through Hermes's native tool system.
        """
        return []

    def handle_tool_call(self, name: str, args: Dict[str, Any], **kwargs) -> str:
        """Handle a Colony tool call from the agent."""
        import json
        return json.dumps({"error": f"Unknown Colony tool: {name}"})

    # -- Optional hooks -------------------------------------------------------

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Flush any pending context at session end."""
        self._cached_context = ""

    def shutdown(self) -> None:
        """Clean up."""
        self._cached_context = ""
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=2)

    # -- Internals ------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @property
    def _contact_id(self) -> str:
        return self.__contact_id

    @_contact_id.setter
    def _contact_id(self, value: str):
        self.__contact_id = value

    def _format_sections(self, sections: list[dict[str, Any]]) -> str:
        """Format Colony sections into a memory-context block."""
        parts = []
        for section in sections:
            header = section.get("id", "colony-context")
            content = section.get("content", "")
            priority = section.get("priority", 50)
            parts.append(f"## {header} [priority {priority}]\n{content}")
        return "<memory-context>\n[Colony Cognitive Context]\n\n" + "\n\n".join(parts) + "\n</memory-context>"
