"""ReasoningLoop — orchestrates model calls and tool execution.

This is the core of Colony's reasoning capability. It replaces the
tightly-coupled reasoning loop in ``run_agent.py`` with a clean,
host-agnostic interface that can be driven via the ``/v1/host/reasoning/turn``
HTTP endpoint.

The loop works like this:

1. Receive messages + optional system prompt from the host
2. Call the LLM via LLMRouter
3. If the LLM returns tool calls, execute them and append results
4. Repeat until the LLM responds without tool calls (or budget exhausted)
5. Return the final response + any pending tool calls

Key differences from run_agent.py:

- No session persistence (hosts manage their own sessions)
- No streaming callbacks (hosts handle streaming themselves)
- No display/print logic (hosts handle display)
- No memory auto-recall (that's the context engine's job)
- Tool execution is delegated to ToolExecutor (pluggable)
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from colony_sidecar.reasoning.executor import ToolExecutor

logger = logging.getLogger(__name__)


@dataclass
class ReasoningConfig:
    """Tunable parameters for the reasoning loop."""

    max_iterations: int = 10
    tool_delay_seconds: float = 0.0  # Delay between tool call batches
    force_tier: str | None = None  # Force a specific LLM tier


@dataclass
class ReasoningResult:
    """Result of a single reasoning turn."""

    status: str  # "completed" | "needs_tool" | "error"
    message: dict[str, Any] | None = None  # HostMessage-shaped
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def _messages_to_dicts(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise messages for LiteLLM consumption.

    Ensures every message has at least ``role`` and ``content`` keys.
    Strips None content, replacing with empty string.
    """
    result = []
    for m in messages:
        d = dict(m)
        if d.get("content") is None:
            d["content"] = ""
        result.append(d)
    return result


class ReasoningLoop:
    """Orchestrate model calls and tool execution for a reasoning turn.

    Parameters
    ----------
    model :
        An object with an ``async complete(messages, *, tools, force_tier, context)``
        method — typically an :class:`~colony_sidecar.router.router.LLMRouter`.
    tools :
        A :class:`ToolExecutor` that can dispatch tool calls.
    config :
        Optional tuning parameters.
    """

    def __init__(
        self,
        model: Any,
        tools: ToolExecutor | None = None,
        config: ReasoningConfig | None = None,
    ) -> None:
        self._model = model
        self._tools = tools or ToolExecutor()
        self._config = config or ReasoningConfig()

    async def run_turn(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        available_tools: list[str] | None = None,
        model_override: str | None = None,
        system_prompt: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> ReasoningResult:
        """Run a single reasoning turn with tool iteration.

        This is the main entry point called by the host router.

        Parameters
        ----------
        session_id :
            Identifier for the reasoning session (for logging / tracing).
        messages :
            OpenAI-format message list (role + content).
        available_tools :
            Tool names the host has made available.
        model_override :
            If set, passed as ``force_tier`` hint to the LLMRouter.
        system_prompt :
            Optional system prompt prepended to messages.
        context :
            Optional routing hints for the LLMRouter.

        Returns
        -------
        ReasoningResult
            The final response, or a needs_tool signal if the budget
            ran out mid-iteration.
        """
        turn_id = str(uuid.uuid4())[:8]
        log_prefix = f"[reasoning:{turn_id}]"

        # Build the working message list
        working = []
        if system_prompt:
            working.append({"role": "system", "content": system_prompt})
        working.extend(_messages_to_dicts(messages))

        # Build tool definitions for the LLM call
        tool_defs = self._tools.get_definitions(available_tools)

        iterations = 0
        last_usage: dict[str, Any] = {}
        cumulative_usage: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

        while iterations < self._config.max_iterations:
            iterations += 1
            logger.debug(
                "%s iteration %d/%d — %d messages, %d tools",
                log_prefix,
                iterations,
                self._config.max_iterations,
                len(working),
                len(tool_defs),
            )

            # Call the LLM
            try:
                tier_hint = model_override or self._config.force_tier
                if isinstance(tier_hint, str):
                    from colony_sidecar.router.tiers import ModelTier
                    try:
                        tier_hint = ModelTier(tier_hint)
                    except ValueError:
                        tier_hint = None
                response = await self._model.complete(
                    working,
                    tools=tool_defs if tool_defs else None,
                    force_tier=tier_hint,
                    context=context or {},
                )
            except Exception as exc:
                logger.error("%s LLM call failed: %s", log_prefix, exc)
                return ReasoningResult(
                    status="error",
                    error=f"LLM call failed: {exc}",
                )

            # Accumulate usage
            last_usage = dict(response.usage)
            for k in cumulative_usage:
                cumulative_usage[k] += last_usage.get(k, 0)

            # Extract the assistant's response
            raw = response.raw
            assistant_content = response.content or ""
            tool_calls_raw = self._extract_tool_calls(raw)

            if not tool_calls_raw:
                # No tool calls — the LLM is done
                return ReasoningResult(
                    status="completed",
                    message={
                        "role": "assistant",
                        "content": assistant_content,
                    },
                    usage=cumulative_usage,
                )

            # Tool calls — execute them
            logger.debug(
                "%s LLM requested %d tool calls",
                log_prefix,
                len(tool_calls_raw),
            )

            # Add assistant message with tool calls to working history
            working.append(self._build_assistant_message(raw, assistant_content, tool_calls_raw))

            # Execute tool calls
            tool_delay = self._config.tool_delay_seconds
            if tool_delay > 0:
                await asyncio.sleep(tool_delay)

            try:
                tool_results = await self._tools.execute_batch(
                    tool_calls_raw,
                    session_id=session_id,
                )
            except Exception as exc:
                logger.error("%s tool execution failed: %s", log_prefix, exc)
                # Return what we have — the host can decide what to do
                return ReasoningResult(
                    status="error",
                    message={
                        "role": "assistant",
                        "content": assistant_content or f"Tool execution error: {exc}",
                    },
                    tool_calls=tool_calls_raw,
                    usage=cumulative_usage,
                    error=f"Tool execution failed: {exc}",
                )

            # Append tool results to working history
            for result in tool_results:
                working.append({
                    "role": "tool",
                    "tool_call_id": result["tool_call_id"],
                    "content": result["content"],
                })

        # Budget exhausted — return needs_tool with pending calls
        logger.warning(
            "%s budget exhausted after %d iterations",
            log_prefix,
            iterations,
        )
        return ReasoningResult(
            status="needs_tool",
            message={
                "role": "assistant",
                "content": assistant_content,
            },
            tool_calls=tool_calls_raw,
            usage=cumulative_usage,
        )

    @staticmethod
    def _extract_tool_calls(raw_response: Any) -> list[dict[str, Any]]:
        """Extract tool calls from a LiteLLM response object.

        Returns a list of dicts with keys: id, name, arguments.
        """
        if raw_response is None:
            return []

        # LiteLLM / OpenAI response format
        choices = getattr(raw_response, "choices", None)
        if not choices:
            return []

        message = getattr(choices[0], "message", None)
        if message is None:
            return []

        raw_calls = getattr(message, "tool_calls", None)
        if not raw_calls:
            return []

        result = []
        for tc in raw_calls:
            tc_id = getattr(tc, "id", None) or str(uuid.uuid4())
            func = getattr(tc, "function", None)
            if func is None:
                continue
            name = getattr(func, "name", "unknown")
            try:
                arguments = json.loads(getattr(func, "arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                arguments = {}

            result.append({
                "id": tc_id,
                "name": name,
                "arguments": arguments,
            })

        return result

    @staticmethod
    def _build_assistant_message(
        raw_response: Any,
        content: str,
        tool_calls: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build an assistant message dict that includes tool calls.

        This preserves the tool call structure in the conversation
        history so the LLM can see its own prior tool calls.
        """
        msg: dict[str, Any] = {"role": "assistant"}

        if content:
            msg["content"] = content
        else:
            msg["content"] = None

        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": (
                            tc["arguments"]
                            if isinstance(tc["arguments"], str)
                            else json.dumps(tc["arguments"])
                        ),
                    },
                }
                for tc in tool_calls
            ]

        return msg
