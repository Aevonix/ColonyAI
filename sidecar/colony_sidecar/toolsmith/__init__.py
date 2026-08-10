"""Toolsmith: the agent's self-extension loop (Mind M1).

Mines the action journal for repeated procedures, drafts a callable tool
(source + input schema + a test) with the LLM, verifies it by running the
test inside the egress-none Docker sandbox, registers it in shadow, and
compares it with the incumbent on bounded captured inputs. A one-shot,
owner-scoped authority bound to the exact candidate and artifact digests is
the only path to live. Trust remains evidence, never publication authority.
Live tools are advertised to the reasoning loop dynamically, so the agent
genuinely gains capability over time instead of only tuning parameters.

Composes shipped infrastructure: SandboxManager (Docker, egress-none), a
digest-only evidence and graduation ledger, ActionJournal, and the LLM router.
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
