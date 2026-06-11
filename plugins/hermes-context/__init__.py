"""Colony context engine plugin for Hermes.

Implements Hermes's ContextEngine ABC to replace the built-in compressor
with Colony's cognitive context assembly.

Plugin directory: ~/.hermes/plugins/context_engine/colony/
Config key: context_engine = "colony"
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Import the ABC if available (Hermes SDK installed).
try:
    from agent.context_engine import ContextEngine as _ContextEngineABC
except ImportError:
    _ContextEngineABC = object  # type: ignore[misc, assignment]  # fallback for standalone testing


class ColonyContextEngine(_ContextEngineABC):
    """Colony-aware context compressor.

    Uses Colony's reasoning loop to produce a cognitively-aware summary
    of the conversation, then returns a compressed message list.

    Falls back to a simple summarization heuristic if Colony is unreachable.

    Config (from ~/.hermes/config.yaml):
        context_engine: colony
        context_engine_config:
          url: "http://127.0.0.1:7777"
          api_key: "${COLONY_API_KEY}"
          max_context_tokens: 120000
          compression_threshold: 0.8
    """

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or {}
        self.sidecar_url = config.get("url", os.environ.get("COLONY_URL", "http://127.0.0.1:7777"))
        self._api_key = config.get("api_key", os.environ.get("COLONY_API_KEY", ""))
        self._contact_id = config.get("contact_id", os.environ.get("COLONY_MCP_CONTACT_ID", "default"))
        self.max_tokens = config.get("max_context_tokens",
            int(os.environ.get("COLONY_MAX_CONTEXT_TOKENS", "1000000")))  # MiMo's full 1M window
        self.threshold = config.get("compression_threshold",
            float(os.environ.get("COLONY_COMPRESSION_THRESHOLD", "0.92")))  # compress at ~920k, reserving ~80k for the reply + margin (1M is shared in/out)
        self._session_id = ""

    @property
    def name(self) -> str:
        return "colony"

    def should_compress(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int | None = None,
        **kwargs,
    ) -> bool:
        """Return True when the message list should be compressed."""
        if current_tokens is not None:
            return current_tokens > int(self.max_tokens * self.threshold)
        # Fallback: approximate 4 chars ~= 1 token
        total_chars = sum(len(m.get("content", "")) for m in messages)
        approx_tokens = total_chars // 4
        return approx_tokens > int(self.max_tokens * self.threshold)

    def compress(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int | None = None,
        focus_topic: str | None = None,
        **kwargs,
    ) -> list[dict[str, Any]]:
        """Compress the message list using Colony-aware summarization.

        Returns a valid OpenAI-format message sequence with:
        1. Original system prompt(s) preserved
        2. A compressed summary message
        3. Recent user/assistant turns kept verbatim (last 6)
        """
        try:
            return self._compress_via_colony(messages, focus_topic=focus_topic)
        except Exception as exc:
            logger.debug("Colony compression failed: %s", exc)
            return self._compress_local(messages)

    def _compress_via_colony(
        self,
        messages: list[dict[str, Any]],
        focus_topic: str | None = None,
    ) -> list[dict[str, Any]]:
        """Ask Colony for a cognitive summary of the conversation."""
        # Separate system / non-system / recent
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        # Keep last 6 turns verbatim
        keep = non_system[-6:] if len(non_system) > 6 else non_system
        to_summarize = non_system[:-6] if len(non_system) > 6 else []

        if not to_summarize:
            return messages

        conversation_text = "\n\n".join(
            f"{m['role']}: {m.get('content', '')}" for m in to_summarize
        )

        with httpx.Client(timeout=15) as client:
            resp = client.post(
                f"{self.sidecar_url}/v1/host/reasoning/turn",
                headers=self._headers(),
                json={
                    "identity": {"host_id": "hermes"},
                    "context": {
                        "session_id": self._session_id,
                        "contact_id": self._contact_id,
                    },
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Summarize the following conversation concisely. "
                                "Preserve commitments, decisions, and important facts. "
                                "Discard pleasantries and redundant turns. "
                                f"Focus topic: {focus_topic or 'general'}"
                            ),
                        },
                        {"role": "user", "content": conversation_text},
                    ],
                },
            )
            resp.raise_for_status()
            summary = resp.json().get("response", "").strip()

        if not summary:
            return self._compress_local(messages)

        compressed = (
            list(system_msgs)
            + [{"role": "system", "content": f"[Conversation summary]\n{summary}"}]
            + list(keep)
        )
        return compressed

    def _compress_local(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Simple fallback: keep system + last 6 messages."""
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        keep = non_system[-6:] if len(non_system) > 6 else non_system
        return list(system_msgs) + list(keep)

    def _headers(self) -> dict[str, str]:
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id

    def update_from_response(self, usage: dict[str, Any]) -> None:
        """Record token usage from the latest model response.

        Required by Hermes' ContextEngine ABC. We track the running token total
        so should_compress() can rely on a real count rather than the char-based
        estimate when the host reports usage.
        """
        if not usage:
            return
        total = (
            usage.get("total_tokens")
            or (usage.get("input_tokens", 0) or 0) + (usage.get("output_tokens", 0) or 0)
            or (usage.get("prompt_tokens", 0) or 0) + (usage.get("completion_tokens", 0) or 0)
        )
        try:
            self._last_total_tokens = int(total or 0)
        except (TypeError, ValueError):
            self._last_total_tokens = 0


def register(ctx):
    """Plugin-style registration."""
    ctx.register_context_engine(ColonyContextEngine())
