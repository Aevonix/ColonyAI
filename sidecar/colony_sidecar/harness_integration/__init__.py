"""Harness integration - writes context files and skills to each harness.

This module provides automatic integration with:
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
    write_colony_check_skill,
    remove_colony_skill,
    get_skill_path,
    get_skill_config_path,
    COLONY_DIAGNOSTIC_SKILL,
    COLONY_CHECK_SKILL,
)

__all__ = [
    "write_colony_context",
    "write_colony_skill",
    "write_colony_check_skill",
    "remove_colony_skill",
    "get_skill_path",
    "get_skill_config_path",
    "COLONY_CONTEXT_TEMPLATE",
    "COLONY_DIAGNOSTIC_SKILL",
    "COLONY_CHECK_SKILL",
]
