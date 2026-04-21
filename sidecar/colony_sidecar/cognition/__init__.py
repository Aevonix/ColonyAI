"""Cognition substrate for Colony.

Background thinking powered by OpenClaw subagent spawning.
Colony owns the trigger pipeline, cognition prompt, and API surface.
OpenClaw owns the LLM execution, model routing, and concurrency.
"""

from colony_sidecar.cognition.prompt import build_cognition_prompt
from colony_sidecar.cognition.trigger import trigger_cognition

__all__ = ["build_cognition_prompt", "trigger_cognition"]
