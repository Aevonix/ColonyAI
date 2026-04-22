"""Reasoning module — host-agnostic reasoning loop.

Extracted from colony-ai's run_agent.py. The ReasoningLoop is the
server-side engine that powers the ``/v1/host/reasoning/turn`` endpoint.
Hosts (OpenClaw, Hermes, etc.) call that endpoint when their plugin
enables ``ownReasoningLoop``; the sidecar runs the actual LLM call +
tool iteration and returns the result.
"""

from colony_sidecar.reasoning.loop import ReasoningLoop, ReasoningConfig, ReasoningResult
from colony_sidecar.reasoning.executor import ToolExecutor

__all__ = ["ReasoningLoop", "ReasoningConfig", "ReasoningResult", "ToolExecutor"]
