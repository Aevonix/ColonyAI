"""Slash commands for the Colony general plugin.

/colony status  →  Sidecar health + capabilities
/colony goals   →  Active goals list
/colony context →  Fetch cognitive context
/colony events  →  Recent cached events
/colony sync    →  Force a turn sync
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import ColonyClient

logger = logging.getLogger(__name__)


async def _handle_status(client: "ColonyClient", args: str) -> str:
    health = client.health()
    status = health.get("status", "unknown")
    caps = health.get("capabilities", [])
    return (
        f"🟢 Colony sidecar: {status}\n"
        f"Capabilities: {', '.join(caps) or 'none'}\n"
        f"Version: {health.get('api_version', 'n/a')}"
    )


async def _handle_goals(client: "ColonyClient", args: str) -> str:
    status_filter = args.strip() or "active"
    goals = client.list_goals(status=status_filter)
    if not goals:
        return f"No {status_filter} goals found."
    lines = [f"🎯 Goals ({status_filter}):"]
    for g in goals[:10]:
        progress = g.get("progress", 0)
        bar = "■" * int(progress / 10) + "□" * (10 - int(progress / 10))
        lines.append(
            f"  • {g['title']} [{g.get('status', '?')}] {bar} {progress}%"
        )
    return "\n".join(lines)


async def _handle_context(client: "ColonyClient", args: str) -> str:
    query = args.strip() or "context check"
    # contact_id is baked into client
    result = client.assemble_context(query, contact_id="default", session_id="slash")
    sections = result.get("sections", [])
    if not sections:
        return "No cognitive context available."
    lines = ["🧠 Cognitive Context:"]
    for s in sections:
        lines.append(f"\n## {s.get('title', 'Context')} [p{s.get('priority', 50)}]")
        lines.append(s.get("body", ""))
    return "\n".join(lines)


async def _handle_events(client: "ColonyClient", args: str) -> str:
    # events are on the subscriber, not the client — handled in __init__
    return "Use /colony events via the general plugin (subscriber not directly accessible)."


async def _handle_sync(client: "ColonyClient", args: str) -> str:
    return "Use /colony sync via the CLI or turn-level sync."


SLASH_COMMANDS = {
    "status": _handle_status,
    "goals": _handle_goals,
    "context": _handle_context,
    "events": _handle_events,
    "sync": _handle_sync,
}
