"""Agent SDK models."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class NodeCertificate(BaseModel):
    """Node certificate structure."""

    colony_id: str
    node_id: str
    public_key: Optional[str] = None  # node_public_key_ed25519 in spec
    issued_at: str  # ISO timestamp
    expires_at: Optional[str] = None  # ISO timestamp
    signature: str


class AgentConfig(BaseModel):
    """Agent configuration file schema.

    This is the structure saved to ~/.colony/agent.json after
    running `colony agent connect`.
    """

    agent_id: str
    node_id: str
    colony_id: str
    websocket_url: Optional[str] = None  # For remote mode
    name: str
    capabilities: List[str] = Field(default_factory=list)
    is_primary: bool = False
    max_concurrent: int = 5
    node_cert: Optional[NodeCertificate] = None
    connection_mode: str = "remote"
    registered_at: Optional[str] = None  # ISO timestamp

    # Optional fields
    priority: int = 1
    excluded_types: List[str] = Field(default_factory=list)
    included_types: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        extra = "allow"  # Forward compatibility
