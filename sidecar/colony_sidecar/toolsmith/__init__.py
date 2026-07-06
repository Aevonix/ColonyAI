"""Toolsmith: the agent's self-extension loop (Mind M1).

Mines the action journal for repeated procedures, drafts a callable tool
(source + input schema + a test) with the LLM, verifies it by running the
test inside the egress-none Docker sandbox, registers it in shadow, and
graduates it to live through the trust engine. Live tools are advertised to
the reasoning loop dynamically, so the agent genuinely gains capability over
time instead of only tuning parameters.

Composes shipped infrastructure: SandboxManager (Docker, egress-none),
TrustEngine (shadow -> ask_first -> act_first), ActionJournal, LLM router.
Generic in ColonyAI; the deployment supplies the sandbox runtimes and which
patterns are worth mining first.
"""

from colony_sidecar.toolsmith.registry import Tool, ToolRegistry, ToolStatus
from colony_sidecar.toolsmith.miner import ToolCandidate, ToolsmithMiner
from colony_sidecar.toolsmith.engine import Toolsmith, toolsmith_enabled

__all__ = [
    "Tool", "ToolRegistry", "ToolStatus",
    "ToolCandidate", "ToolsmithMiner",
    "Toolsmith", "toolsmith_enabled",
]
