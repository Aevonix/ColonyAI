"""Harness integration - writes context files and skills to each harness.

This module provides automatic integration with:
- OpenClaw (plugin + context + skill)
- Crush (MCP + skill)
- Codex (MCP + skill)
- Claude Code (MCP + skill)
- OpenCode (MCP + skill)
- Hermes (plugin only)
"""

from .context import (
    write_colony_context,
    COLONY_CONTEXT_TEMPLATE,
)
from .skills import (
    write_colony_skill,
    remove_colony_skill,
    get_skill_path,
    get_skill_config_path,
    COLONY_DIAGNOSTIC_SKILL,
)
from .detect import (
    detect_openclaw_workspace,
)

__all__ = [
    "write_colony_context",
    "write_colony_skill",
    "remove_colony_skill",
    "get_skill_path",
    "get_skill_config_path",
    "detect_openclaw_workspace",
    "COLONY_CONTEXT_TEMPLATE",
    "COLONY_DIAGNOSTIC_SKILL",
]
