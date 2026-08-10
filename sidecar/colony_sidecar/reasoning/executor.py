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

import inspect
import json
import logging
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Callable, Coroutine

if TYPE_CHECKING:
    from colony_sidecar.autonomy.registry import SubsystemRegistry

from colony_sidecar.tools.definitions import STATIC_TOOL_NAMES, get_tool_definitions
from colony_sidecar.tools.handlers import TOOL_HANDLERS
from colony_sidecar.reasoning.tool_policy import (
    ToolActorPolicy,
    ToolEffect,
    action_target,
    actor_allows_effect,
    classify_tool,
)

logger = logging.getLogger(__name__)


# Type alias for tool handler functions
ToolHandler = Callable[..., Coroutine[Any, Any, str]]


class ToolRegistryError(RuntimeError):
    """A dynamic tool surface could not be resolved without ambiguity."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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
        self._directive_manager = None
        self._boundary_required = False

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

    def configure_execution_policy(
        self,
        *,
        directive_manager,
        boundary_required: bool = True,
    ) -> None:
        """Install the standing-boundary dependency for all executions.

        Server startup uses ``boundary_required=True``. Tests and legacy
        in-process embeddings that never configure a manager retain their
        historical behavior; a configured live server does not silently run a
        private/mutating tool when the boundary dependency is absent.
        """

        self._directive_manager = directive_manager
        self._boundary_required = bool(boundary_required)

    def _dynamic_tools(self) -> dict[str, Any]:
        if self._dynamic_provider is None:
            return {}
        try:
            supplied = self._dynamic_provider()
        except Exception as exc:
            logger.warning("dynamic tool provider failed: %s", exc)
            raise ToolRegistryError(
                "tool_registry_unavailable",
                "dynamic tool provider is unavailable",
            ) from exc
        if supplied is None:
            return {}
        if not isinstance(supplied, Mapping):
            raise ToolRegistryError(
                "tool_registry_malformed",
                "dynamic tool provider returned a malformed registry",
            )

        dynamic = dict(supplied)
        names = set()
        for name in dynamic:
            if (
                not isinstance(name, str)
                or not name
                or name.strip() != name
            ):
                raise ToolRegistryError(
                    "tool_registry_malformed",
                    "dynamic tool provider returned an invalid name",
                )
            names.add(name)

        collisions = names.intersection(
            set(STATIC_TOOL_NAMES).union(self._handlers)
        )
        if collisions:
            listed = ", ".join(sorted(collisions))
            logger.error("dynamic/static tool name collision: %s", listed)
            raise ToolRegistryError(
                "tool_name_collision",
                f"dynamic tool name collides with first-party tool: {listed}",
            )

        validated: dict[str, Any] = {}
        for name, value in dynamic.items():
            if not isinstance(value, (tuple, list)) or len(value) != 2:
                raise ToolRegistryError(
                    "tool_registry_malformed",
                    f"dynamic tool '{name}' has a malformed provider entry",
                )
            definition, handler = value
            if (
                self._definition_name(definition) != name
                or not inspect.iscoroutinefunction(handler)
            ):
                raise ToolRegistryError(
                    "tool_registry_malformed",
                    f"dynamic tool '{name}' has invalid definition provenance",
                )
            validated[name] = (definition, handler)
        return validated

    @staticmethod
    def _definition_name(definition: object) -> str | None:
        if not isinstance(definition, Mapping):
            return None
        if definition.get("type") == "function":
            function = definition.get("function")
            if not isinstance(function, Mapping):
                return None
            name = function.get("name")
        else:
            name = definition.get("name")
        return name if isinstance(name, str) and name else None

    def _resolved_handler(
        self,
        name: str,
        dynamic: Mapping[str, Any],
    ) -> tuple[ToolHandler | None, ToolEffect | None, str | None]:
        """Resolve executable and provenance before any authority decision."""

        handler = self._handlers.get(name)
        if handler is not None:
            return handler, classify_tool(name), "static"
        supplied = dynamic.get(name)
        if supplied is not None:
            # Dynamic capabilities are always mutations. Their human-selected
            # names are never an authorization type declaration.
            return supplied[1], ToolEffect.MUTATION, "dynamic"
        return None, None, None

    def available_names(self) -> list[str]:
        """Return a stable, de-duplicated snapshot of executable tool names."""

        dynamic = self._dynamic_tools()
        return list(dict.fromkeys((
            *self._handlers.keys(),
            *dynamic.keys(),
        )))

    def filter_names_for_actor(
        self,
        names: list[str] | tuple[str, ...] | set[str] | frozenset[str] | None,
        actor_policy: ToolActorPolicy | None,
    ) -> list[str]:
        """Filter one registry snapshot using resolved handler provenance."""

        dynamic = self._dynamic_tools()
        requested = (
            (*self._handlers.keys(), *dynamic.keys())
            if names is None else names
        )
        allowed = []
        for name in dict.fromkeys(requested):
            handler, effect, _provenance = self._resolved_handler(name, dynamic)
            if (
                handler is not None
                and effect is not None
                and actor_allows_effect(effect, actor_policy)
            ):
                allowed.append(name)
        return allowed

    def _boundary_verdict(
        self,
        name: str,
        arguments: dict[str, Any],
        effect: ToolEffect,
    ) -> tuple[bool, str | None, str]:
        """Return ``(allowed, error_code, reason)`` for standing boundaries."""

        manager = self._directive_manager
        if manager is None:
            # Keep general information tools usable during a boundary-store
            # outage. Private reads and mutations fail closed on a configured
            # server because they can expose or alter durable owner state.
            if not self._boundary_required or effect is ToolEffect.PUBLIC_READ:
                return True, None, "boundary_not_required"
            return False, "tool_boundary_unavailable", "directive manager unavailable"

        try:
            from colony_sidecar.directives import Action

            verdict = manager.check(Action(
                kind=(
                    "read"
                    if effect in {ToolEffect.PUBLIC_READ, ToolEffect.PRIVATE_READ}
                    else "execute_tool"
                ),
                text=f"reasoning tool {name}",
                target=action_target(arguments),
                tool_name=name,
                args=dict(arguments),
                high_risk=(effect is ToolEffect.MUTATION),
            ))
        except Exception as exc:
            logger.warning("ToolExecutor: boundary check failed for '%s': %s", name, exc)
            if effect is ToolEffect.PUBLIC_READ:
                return True, None, "public_read_boundary_unavailable"
            return False, "tool_boundary_unavailable", "directive check unavailable"

        allowed = getattr(verdict, "allowed", None)
        if not isinstance(allowed, bool):
            logger.warning("ToolExecutor: malformed boundary verdict for '%s'", name)
            if effect is ToolEffect.PUBLIC_READ:
                return True, None, "public_read_boundary_malformed"
            return False, "tool_boundary_unavailable", "malformed directive verdict"
        if not allowed:
            return (
                False,
                "tool_boundary_denied",
                str(getattr(verdict, "reason", "standing owner boundary") or
                    "standing owner boundary"),
            )
        return True, None, str(getattr(verdict, "reason", "ok") or "ok")

    @staticmethod
    def _error_result(
        tool_call_id: str,
        *,
        code: str,
        message: str,
        tool_name: str,
    ) -> dict[str, Any]:
        return {
            "tool_call_id": tool_call_id,
            "content": json.dumps({
                "error": code,
                "message": message,
                "tool_name": tool_name,
            }),
            "executed": False,
            "error": code,
        }

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
        names = list(dict.fromkeys(
            available_tools if explicit_filter else self._handlers.keys()
        ))
        definitions = get_tool_definitions(tool_names=names)
        defs = []
        seen = set()
        for definition in definitions:
            name = self._definition_name(definition)
            if name is None:
                raise ToolRegistryError(
                    "tool_registry_malformed",
                    "first-party tool definition is malformed",
                )
            # The static catalog is authoritative. Keep one stable definition
            # if a future composition accidentally repeats a shipped entry.
            if name not in seen:
                defs.append(definition)
                seen.add(name)
        for name, (definition, _handler) in dynamic.items():
            # a filter must name a dynamic tool to include it; unfiltered
            # turns see all graduated tools
            if definition and (not explicit_filter or name in names):
                if name in seen:
                    raise ToolRegistryError(
                        "tool_name_collision",
                        f"model tool definition collision for '{name}'",
                    )
                defs.append(definition)
                seen.add(name)
        return defs

    async def execute_batch(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        session_id: str = "",
        allowed_tools: set[str] | frozenset[str] | None = None,
        actor_policy: ToolActorPolicy | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a batch of tool calls and return results.

        Each result dict has:
        - tool_call_id: the ID from the original tool call
        - content: the string result

        Unknown tools return a "not implemented" result rather than
        raising, so the LLM can see the failure and adjust.
        """
        results = []
        try:
            dynamic = self._dynamic_tools()
        except ToolRegistryError as exc:
            for tc in tool_calls:
                tc_id = tc.get("id", str(uuid.uuid4()))
                name = str(tc.get("name", "unknown"))
                results.append(self._error_result(
                    tc_id,
                    code=exc.code,
                    message=str(exc),
                    tool_name=name,
                ))
            return results

        for tc in tool_calls:
            tc_id = tc.get("id", str(uuid.uuid4()))
            name = tc.get("name", "unknown")
            arguments = tc.get("arguments", {})
            if not isinstance(arguments, dict):
                arguments = {}

            handler, effect, _provenance = self._resolved_handler(name, dynamic)
            if handler is None or effect is None:
                logger.debug("ToolExecutor: no handler for '%s' — returning error", name)
                results.append({
                    "tool_call_id": tc_id,
                    "content": json.dumps({
                        "error": True,
                        "message": f"Tool '{name}' is not available. Try a different approach.",
                        "available_tools": list(self._handlers.keys())
                        + list(dynamic.keys()),
                    }),
                    "executed": False,
                    "error": "tool_unavailable",
                })
                continue

            if allowed_tools is not None and name not in allowed_tools:
                results.append(self._error_result(
                    tc_id,
                    code="tool_not_authorized",
                    message=f"Tool '{name}' was not authorized for this turn.",
                    tool_name=name,
                ))
                continue

            if not actor_allows_effect(effect, actor_policy):
                results.append(self._error_result(
                    tc_id,
                    code="tool_authority_denied",
                    message=f"Tool '{name}' exceeds caller authority.",
                    tool_name=name,
                ))
                continue

            boundary_allowed, boundary_error, boundary_reason = (
                self._boundary_verdict(name, arguments, effect)
            )
            if not boundary_allowed:
                assert boundary_error is not None
                results.append(self._error_result(
                    tc_id,
                    code=boundary_error,
                    message=boundary_reason,
                    tool_name=name,
                ))
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
                    "executed": True,
                })
            except Exception as exc:
                logger.error("ToolExecutor: handler '%s' failed: %s", name, exc)
                results.append({
                    "tool_call_id": tc_id,
                    "content": json.dumps({
                        "error": f"Tool '{name}' execution failed: {exc}",
                        "tool_name": name,
                    }),
                    "executed": False,
                    "error": "tool_execution_failed",
                })

        return results
