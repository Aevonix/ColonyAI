"""Agent data models for multi-agent Colony.

Defines:
- AgentStatus enum
- AgentMetadata schema
- Agent dataclass
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class AgentStatus(str, Enum):
    """Agent status values."""

    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"
    SUSPENDED = "suspended"
    REVOKED = "revoked"

    def is_active(self) -> bool:
        """Can this agent receive assignments?"""
        return self in (AgentStatus.ONLINE, AgentStatus.BUSY)

    def can_reconnect(self) -> bool:
        """Can this agent reconnect to Colony?"""
        return self != AgentStatus.REVOKED


@dataclass
class AgentMetadata:
    """Structured metadata about an agent's environment.

    Standard fields:
    - hostname: Machine hostname
    - platform: OS platform (darwin, linux, windows)
    - version: Colony version
    - harness: Which harness (openclaw, claude-code, codex, crush)
    - {harness}_version: Harness version
    - python_version: Python version (for Python-based harnesses)
    - started_at: ISO timestamp when agent started
    - tz: IANA timezone
    """

    hostname: Optional[str] = None
    platform: Optional[str] = None
    version: Optional[str] = None
    harness: Optional[str] = None
    started_at: Optional[str] = None
    tz: Optional[str] = None
    last_connection_ip: Optional[str] = None
    last_connection_ip_ts: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentMetadata":
        """Create from dict, handling unknown fields."""
        known = {
            "hostname",
            "platform",
            "version",
            "harness",
            "started_at",
            "tz",
            "last_connection_ip",
            "last_connection_ip_ts",
        }
        return cls(
            hostname=data.get("hostname"),
            platform=data.get("platform"),
            version=data.get("version"),
            harness=data.get("harness"),
            started_at=data.get("started_at"),
            tz=data.get("tz"),
            last_connection_ip=data.get("last_connection_ip"),
            last_connection_ip_ts=data.get("last_connection_ip_ts"),
            extra={k: v for k, v in data.items() if k not in known},
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        result = {}
        if self.hostname:
            result["hostname"] = self.hostname
        if self.platform:
            result["platform"] = self.platform
        if self.version:
            result["version"] = self.version
        if self.harness:
            result["harness"] = self.harness
        if self.started_at:
            result["started_at"] = self.started_at
        if self.tz:
            result["tz"] = self.tz
        if self.last_connection_ip:
            result["last_connection_ip"] = self.last_connection_ip
        if self.last_connection_ip_ts:
            result["last_connection_ip_ts"] = self.last_connection_ip_ts
        result.update(self.extra)
        return result

    def get_harness_version(self) -> Optional[str]:
        """Get harness-specific version field."""
        if not self.harness:
            return None
        return self.extra.get(f"{self.harness}_version")


@dataclass
class Agent:
    """A connected agent (OpenClaw instance, Claude Code, etc.).

    Attributes:
        agent_id: Unique identifier (UUID)
        node_id: Device node ID
        colony_id: Parent Colony ID
        name: Human-readable name (e.g., "spark1", "macmini")
        connection_mode: "local" (HTTP) or "remote" (WebSocket)
        gateway_url: For local mode, URL to push initiatives
        websocket_connected: For remote mode, is WebSocket active?
        capabilities: What can this agent do? (messaging, calendar, coding)
        is_primary: Preferred for user-facing initiatives
        priority: 0=backup, 1=normal, 2=high
        max_concurrent: Max simultaneous initiative assignments
        max_initiatives_per_hour: Rate limit
        excluded_types: Initiative types to skip
        included_types: Only these types (if set)
        status: Current status
        current_assignments: Count of active assignments
        last_seen_at: Last heartbeat/disconnect time
        metadata: Environment info
        registered_at: When agent was registered
        node_cert: Signed certificate (JSON)
    """

    agent_id: str
    node_id: str
    colony_id: str
    name: str
    connection_mode: str = "local"
    gateway_url: Optional[str] = None
    websocket_connected: bool = False
    capabilities: List[str] = field(default_factory=list)
    is_primary: bool = False
    priority: int = 1
    max_concurrent: int = 5
    max_initiatives_per_hour: int = 10
    excluded_types: List[str] = field(default_factory=list)
    included_types: List[str] = field(default_factory=list)
    status: str = "offline"
    current_assignments: int = 0
    last_seen_at: Optional[datetime] = None
    metadata: AgentMetadata = field(default_factory=AgentMetadata)
    registered_at: Optional[datetime] = None
    node_cert: Optional[Dict[str, Any]] = None

    @property
    def load(self) -> float:
        """Current load as ratio (0.0-1.0)."""
        if self.max_concurrent <= 0:
            return 1.0
        return self.current_assignments / self.max_concurrent

    @property
    def has_capacity(self) -> bool:
        """Can accept more assignments?"""
        return (
            self.status in ("online", "busy")
            and self.current_assignments < self.max_concurrent
        )

    def can_handle_type(self, initiative_type: str) -> bool:
        """Check if agent can handle this initiative type."""
        # Excluded types take priority
        if initiative_type in self.excluded_types:
            return False

        # If included_types is set, must be in list
        if self.included_types and initiative_type not in self.included_types:
            return False

        return True

    def has_capability(self, capability: str) -> bool:
        """Check if agent has a specific capability."""
        return capability in self.capabilities

    def has_capabilities(self, capabilities: List[str]) -> bool:
        """Check if agent has all specified capabilities."""
        return all(cap in self.capabilities for cap in capabilities)

    @classmethod
    def from_row(cls, row: dict) -> "Agent":
        """Create from SQLite row dict."""
        import json

        metadata_raw = row.get("metadata", "{}")
        if isinstance(metadata_raw, str):
            metadata_dict = json.loads(metadata_raw)
        else:
            metadata_dict = metadata_raw

        capabilities_raw = row.get("capabilities", "[]")
        if isinstance(capabilities_raw, str):
            capabilities = json.loads(capabilities_raw)
        else:
            capabilities = capabilities_raw

        excluded_raw = row.get("excluded_types", "[]")
        if isinstance(excluded_raw, str):
            excluded_types = json.loads(excluded_raw)
        else:
            excluded_types = excluded_raw

        included_raw = row.get("included_types", "[]")
        if isinstance(included_raw, str):
            included_types = json.loads(included_raw)
        else:
            included_types = included_raw

        node_cert_raw = row.get("node_cert")
        if node_cert_raw and isinstance(node_cert_raw, str):
            node_cert = json.loads(node_cert_raw)
        else:
            node_cert = node_cert_raw

        return cls(
            agent_id=row["agent_id"],
            node_id=row["node_id"],
            colony_id=row["colony_id"],
            name=row["name"],
            connection_mode=row.get("connection_mode", "local"),
            gateway_url=row.get("gateway_url"),
            websocket_connected=bool(row.get("websocket_connected", 0)),
            capabilities=capabilities,
            is_primary=bool(row.get("is_primary", 0)),
            priority=row.get("priority", 1),
            max_concurrent=row.get("max_concurrent", 5),
            max_initiatives_per_hour=row.get("max_initiatives_per_hour", 10),
            excluded_types=excluded_types,
            included_types=included_types,
            status=row.get("status", "offline"),
            current_assignments=row.get("current_assignments", 0),
            last_seen_at=_parse_datetime(row.get("last_seen_at")),
            metadata=AgentMetadata.from_dict(metadata_dict),
            registered_at=_parse_datetime(row.get("registered_at")),
            node_cert=node_cert,
        )


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse ISO datetime string."""
    if not value:
        return None
    try:
        # Handle both with and without timezone
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
