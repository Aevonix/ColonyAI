"""Mesh node data models.

Colony runs as a distributed mesh where nodes take on different roles.
The Sovereign node is the primary brain, Regents are backup brains,
and Vassals are worker nodes that handle specific tasks.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class NodeRole(str, Enum):
    """Role a node plays in the Colony mesh.

    SOVEREIGN: The queen node with full brain capabilities.
    REGENT: Backup brain, can assume sovereignty if needed.
    VASSAL: Worker node, handles delegated tasks.
    """

    SOVEREIGN = "sovereign"
    REGENT = "regent"
    VASSAL = "vassal"


class NodeStatus(str, Enum):
    """Current operational status of a mesh node.

    ONLINE: Node is healthy and responsive.
    OFFLINE: Node is unreachable.
    DEGRADED: Node is up but not fully functional.
    SYNCING: Node is catching up on state.
    """

    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"
    SYNCING = "syncing"


@dataclass
class MeshNode:
    """A node in the Colony mesh network.

    Each node has a role, capabilities, and resource score that determines
    what work gets routed to it. Nodes communicate via encrypted channels
    using their public keys.

    Attributes:
        id: Unique node identifier
        role: What role this node serves (sovereign, regent, vassal)
        status: Current operational status
        capabilities: What this node can do ("gpu", "neo4j", "llm", etc.)
        resource_score: Relative compute power score (higher = more capable)
        endpoint: URL for the node's API
        public_key: Encryption key for secure mesh communication
        last_seen: Most recent heartbeat timestamp
        registered_at: When the node joined the mesh
        metadata: Additional node-specific data
    """

    id: str
    role: NodeRole
    status: NodeStatus = NodeStatus.OFFLINE
    capabilities: List[str] = field(default_factory=list)
    resource_score: int = 0
    endpoint: Optional[str] = None
    public_key: Optional[bytes] = None
    last_seen: Optional[datetime] = None
    registered_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
