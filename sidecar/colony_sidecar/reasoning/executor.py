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
        graph_client = None,
    ) -> None:
        self._handlers: dict[str, ToolHandler] = handlers or {}
        self._registry = registry
        self._graph = graph_client
        # Dynamic tools (e.g. toolsmith-built): a provider returning
        # {name: (openai_definition, async_handler)} consulted at call time
        # so newly-graduated tools appear without re-instantiation.
        self._dynamic_provider = None

        # Auto-register Colony-native tool handlers if registry is provided
        if registry is not None:
            for name, handler in TOOL_HANDLERS.items():
                if name not in self._handlers:
                    # Wrap handler to inject registry
                    self._handlers[name] = lambda args, h=handler, r=registry: h(args, r)

    def register(self, name: str, handler: ToolHandler) -> None:
        """Register a tool handler."""
        self._handlers[name] = handler

    def register_native_tools(self, search_orchestrator=None, sandbox_dir: str = "") -> None:
        """Register Colony-native tools that run inside the sidecar.

        Tool classes expose ``.execute(args) -> dict``; the executor's
        handler contract is ``(args) -> Awaitable[str|dict]``, so we bind
        the bound-method ``tool.execute`` directly — registering the bare
        instance would fail because the tool classes are not callable.
        """
        try:
            from colony_sidecar.reasoning.native_tools.calculate import CalculateTool
            self.register("calculate", CalculateTool().execute)
        except Exception as exc:
            logger.warning("register calculate tool failed: %s", exc)

        if search_orchestrator and search_orchestrator.has_providers:
            try:
                from colony_sidecar.reasoning.native_tools.web_search import WebSearchTool
                ws_tool = WebSearchTool(search_orchestrator)
                self.register("web_search", ws_tool.execute)
            except Exception as exc:
                logger.warning("register web_search tool failed: %s", exc)

        if sandbox_dir:
            try:
                from colony_sidecar.reasoning.native_tools.file_ops import (
                    ReadFileTool, WriteFileTool, ListDirectoryTool,
                )
                self.register("read_file", ReadFileTool(sandbox_dir).execute)
                self.register("write_file", WriteFileTool(sandbox_dir).execute)
                self.register("list_directory", ListDirectoryTool(sandbox_dir).execute)
            except Exception as exc:
                logger.warning("register file_ops tools failed: %s", exc)

    def unregister(self, name: str) -> None:
        """Remove a tool handler."""
        self._handlers.pop(name, None)

    def set_dynamic_provider(self, provider) -> None:
        """Register a provider callable returning a dict
        {name: (openai_definition, async_handler)} of runtime tools
        (e.g. toolsmith-graduated tools). Consulted on every turn."""
        self._dynamic_provider = provider

    def _dynamic_tools(self) -> dict[str, Any]:
        if self._dynamic_provider is None:
            return {}
        try:
            return self._dynamic_provider() or {}
        except Exception as exc:
            logger.debug("dynamic tool provider failed: %s", exc)
            return {}

    def get_definitions(self, available_tools: list[str] | None = None) -> list[dict[str, Any]]:
        """Build OpenAI-format tool definitions for the LLM call.

        Returns Colony-native tool definitions that can be executed server-side.
        These are in addition to any host-side tools passed through the ReasoningLoop.

        Parameters
        ----------
        available_tools :
            Optional filter for specific tool names to include. When None,
            defaults to the set of registered handlers.

        Returns
        -------
        List of OpenAI-format tool definitions.
        """
        dynamic = self._dynamic_tools()
        explicit_filter = available_tools is not None
        names = available_tools if explicit_filter else list(self._handlers.keys())
        defs = get_tool_definitions(tool_names=names)
        for name, (definition, _handler) in dynamic.items():
            # a filter must name a dynamic tool to include it; unfiltered
            # turns see all graduated tools
            if definition and (not explicit_filter or name in names):
                defs.append(definition)
        return defs

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
                dyn = self._dynamic_tools().get(name)
                if dyn is not None:
                    handler = dyn[1]
            if handler is None:
                logger.debug("ToolExecutor: no handler for '%s' — returning error", name)
                results.append({
                    "tool_call_id": tc_id,
                    "content": json.dumps({
                        "error": True,
                        "message": f"Tool '{name}' is not available. Try a different approach.",
                        "available_tools": list(self._handlers.keys())
                        + list(self._dynamic_tools().keys()),
                    }),
                })
                continue

            try:
                result = await handler(arguments)
                if isinstance(result, (dict, list)):
                    content = json.dumps(result)
                else:
                    content = str(result)
                results.append({
                    "tool_call_id": tc_id,
                    "content": content,
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
