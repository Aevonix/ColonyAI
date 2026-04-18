"""ToolExecutor — dispatch tool calls and collect results.

The executor is pluggable: hosts can register tool handlers, or the
default executor can be used (which returns "not implemented" for
unknown tools, letting the host handle them client-side).

In the current MVP, tool execution is stubbed — the ReasoningLoop
returns ``needs_tool`` when it encounters tool calls, and the host
plugin (colony-core) handles them locally. Future work will add
server-side tool execution for Colony-native tools (memory, search, etc.).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


# Type alias for tool handler functions
ToolHandler = Callable[..., Coroutine[Any, Any, str]]


class ToolExecutor:
    """Dispatch tool calls and collect results.

    Parameters
    ----------
    handlers :
        Optional mapping of tool name → async handler function.
        Handlers receive the tool's arguments dict and must return
        a string result.
    """

    def __init__(
        self,
        handlers: dict[str, ToolHandler] | None = None,
    ) -> None:
        self._handlers: dict[str, ToolHandler] = handlers or {}

    def register(self, name: str, handler: ToolHandler) -> None:
        """Register a tool handler."""
        self._handlers[name] = handler

    def unregister(self, name: str) -> None:
        """Remove a tool handler."""
        self._handlers.pop(name, None)

    def get_definitions(self, available_tools: list[str] | None = None) -> list[dict[str, Any]]:
        """Build OpenAI-format tool definitions for the LLM call.

        For the MVP, we don't auto-generate definitions from handlers.
        Instead, the host passes its own tool definitions through the
        ReasoningLoop and the LLM call includes them directly.

        This method is reserved for future use when Colony has its own
        native tools that need to be advertised to the LLM.
        """
        # Future: generate definitions from registered handlers
        return []

    async def execute_batch(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        session_id: str = "",
    ) -> list[dict[str, Any]]:
        """Execute a batch of tool calls and return results.

        Each result dict has:
        - tool_call_id: the ID from the original tool call
        - content: the string result

        Unknown tools return a "not implemented" result rather than
        raising, so the LLM can see the failure and adjust.
        """
        results = []
        for tc in tool_calls:
            tc_id = tc.get("id", str(uuid.uuid4()))
            name = tc.get("name", "unknown")
            arguments = tc.get("arguments", {})

            handler = self._handlers.get(name)
            if handler is None:
                logger.debug("ToolExecutor: no handler for '%s' — returning stub", name)
                results.append({
                    "tool_call_id": tc_id,
                    "content": json.dumps({
                        "error": f"Tool '{name}' not implemented server-side. "
                                 "The host should handle this tool call locally.",
                        "tool_name": name,
                        "status": "needs_host_execution",
                    }),
                })
                continue

            try:
                result = await handler(arguments)
                results.append({
                    "tool_call_id": tc_id,
                    "content": str(result),
                })
            except Exception as exc:
                logger.error("ToolExecutor: handler '%s' failed: %s", name, exc)
                results.append({
                    "tool_call_id": tc_id,
                    "content": json.dumps({
                        "error": f"Tool '{name}' execution failed: {exc}",
                        "tool_name": name,
                    }),
                })

        return results
