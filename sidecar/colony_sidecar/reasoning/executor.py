"""ToolExecutor — dispatch tool calls and collect results.

The executor is pluggable: hosts can register tool handlers, or the
default executor can be used (which returns "not implemented" for
unknown tools, letting the host handle them client-side).

Colony-native tools are defined in tools/definitions.py and handlers
are in tools/handlers.py. These tools provide direct access to
Colony's intelligence systems (memory, goals, relationships, etc.)
without going through the host plugin.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any, Callable, Coroutine

if TYPE_CHECKING:
    from colony_sidecar.autonomy.registry import SubsystemRegistry

from colony_sidecar.tools.definitions import get_tool_definitions
from colony_sidecar.tools.handlers import TOOL_HANDLERS

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
    registry :
        Optional SubsystemRegistry for Colony-native tools. When provided,
        Colony tools are automatically available.
    """

    def __init__(
        self,
        handlers: dict[str, ToolHandler] | None = None,
        registry: SubsystemRegistry | None = None,
    ) -> None:
        self._handlers: dict[str, ToolHandler] = handlers or {}
        self._registry = registry

        # Auto-register Colony-native tool handlers if registry is provided
        if registry is not None:
            for name, handler in TOOL_HANDLERS.items():
                if name not in self._handlers:
                    # Wrap handler to inject registry
                    self._handlers[name] = lambda args, h=handler, r=registry: h(args, r)

    def register(self, name: str, handler: ToolHandler) -> None:
        """Register a tool handler."""
        self._handlers[name] = handler

    def unregister(self, name: str) -> None:
        """Remove a tool handler."""
        self._handlers.pop(name, None)

    def get_definitions(self, available_tools: list[str] | None = None) -> list[dict[str, Any]]:
        """Build OpenAI-format tool definitions for the LLM call.

        Returns Colony-native tool definitions that can be executed server-side.
        These are in addition to any host-side tools passed through the ReasoningLoop.

        Parameters
        ----------
        available_tools :
            Optional filter for specific tool names to include.

        Returns
        -------
        List of OpenAI-format tool definitions.
        """
        # Get Colony-native tool definitions
        return get_tool_definitions(tool_names=available_tools)

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
