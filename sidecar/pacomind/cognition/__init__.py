"""Cognition substrate for PacoMind.

Background thinking powered by OpenClaw subagent spawning.
PacoMind owns the trigger pipeline, cognition prompt, and API surface.
OpenClaw owns the LLM execution, model routing, and concurrency.
"""

from pacomind.cognition.prompt import build_cognition_prompt
from pacomind.cognition.trigger import trigger_cognition

__all__ = ["build_cognition_prompt", "trigger_cognition"]
