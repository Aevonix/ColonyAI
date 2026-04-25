"""Multi-agent support for Colony.

Provides:
- AgentStore: Registry of connected agents
- InviteStore: Setup code management
- WebSocketManager: Remote agent connections
"""

from .models import Agent, AgentStatus, AgentMetadata
from .store import AgentStore, InviteStore
from .websocket import WebSocketManager

__all__ = [
    "Agent",
    "AgentStatus",
    "AgentMetadata",
    "AgentStore",
    "InviteStore",
    "WebSocketManager",
]
