"""Colony context engine for Hermes.

Calls Colony sidecar's /v1/host/context/assemble endpoint
to get cognitive context (commitments, affect, facts, patterns, surprises)
and injects it as an ephemeral system prompt layer.
"""

import os
from typing import Any, Optional

import httpx


class ColonyContextEngine:
    """Colony context engine for Hermes.

    Reads cognitive context from Colony's sidecar and returns it
    as a string to be injected into the prompt.

    Config keys (from ~/.hermes/config.yaml context_engine.config):
        url: Colony sidecar URL (default http://127.0.0.1:7777)
        api_key: Colony API key (or set COLONY_API_KEY env var)
        contact_id: Contact ID for context assembly (or set COLONY_MCP_CONTACT_ID)
    """

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or {}
        self.sidecar_url = config.get("url", os.environ.get("COLONY_URL", "http://127.0.0.1:7777"))
        self.api_key = config.get("api_key", os.environ.get("COLONY_API_KEY", ""))
        self.contact_id = config.get("contact_id", os.environ.get("COLONY_MCP_CONTACT_ID", "default"))

    def assemble(self, messages: list[dict[str, Any]], session_id: str = "") -> Optional[str]:
        """Call Colony's context assembly and return formatted context.

        Args:
            messages: Conversation messages (used to extract last user message
                      for query-aware context).
            session_id: Current session ID.

        Returns:
            Formatted context string, or None if the sidecar is unreachable.
        """
        incoming = self._extract_last_user_message(messages)

        try:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            resp = httpx.post(
                f"{self.sidecar_url}/v1/host/context/assemble",
                headers=headers,
                json={
                    "identity": {"host_id": "hermes"},
                    "context": {
                        "session_id": session_id,
                        "contact_id": self.contact_id,
                    },
                    "incoming_message": incoming,
                },
                timeout=10,
            )
            resp.raise_for_status()
        except (httpx.HTTPError, OSError):
            return None

        data = resp.json()
        sections = data.get("sections", [])
        if not sections:
            return None

        return self._format_sections(sections)

    def _extract_last_user_message(self, messages: list[dict[str, Any]]) -> dict[str, str]:
        """Extract the last user message for query-aware context."""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return {"role": "user", "content": msg.get("content", "")}
        return {"role": "user", "content": ""}

    def _format_sections(self, sections: list[dict[str, Any]]) -> str:
        """Format Colony sections into a prompt block."""
        parts = []
        for section in sections:
            header = section.get("id", "colony-context")
            content = section.get("content", "")
            priority = section.get("priority", 50)
            parts.append(f"## {header} [priority {priority}]\n{content}")
        return "# Colony Cognitive Context\n\n" + "\n\n".join(parts)
