"""Colony Skills — data models for the self-creating skills subsystem."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set


class SkillStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    INACTIVE = "inactive"
    QUARANTINED = "quarantined"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


class SkillCapability(str, Enum):
    """Capabilities a skill may declare. All are deny-by-default."""
    NETWORK = "network"
    FILESYSTEM_READ = "fs_read"
    FILESYSTEM_WRITE = "fs_write"
    SUBPROCESS = "subprocess"
    ENV_READ = "env_read"
    LLM_CALL = "llm_call"
    TASK_POST = "task_post"
    FEDERATION_EMIT = "fed_emit"


@dataclass
class SkillPermissions:
    """Declared resource access for a skill."""
    capabilities: List[SkillCapability] = field(default_factory=list)
    allowed_domains: List[str] = field(default_factory=list)
    allowed_read_paths: List[str] = field(default_factory=list)
    allowed_write_paths: List[str] = field(default_factory=list)
    allowed_env_vars: List[str] = field(default_factory=list)
    max_duration_secs: int = 60
    max_memory_mb: int = 256


@dataclass
class SkillManifest:
    """Complete metadata for a Colony skill.

    Stored as colony.skill.json alongside the skill source.
    """
    skill_id: str
    name: str
    version: str
    description: str
    author_colony_id: str
    created_at: datetime
    updated_at: datetime
    status: SkillStatus = SkillStatus.DRAFT
    entry_point: str = "skill:run"
    permissions: SkillPermissions = field(default_factory=SkillPermissions)
    tags: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    checksum_sha256: str = ""
    # Empty string means "no checksum recorded" (e.g. MCP-bridged or legacy skills).
    # Any execution path that verifies integrity MUST reject an empty value — it is
    # not a valid hash.  See SkillExecutor._run_skill() which raises SecurityError
    # when this field is falsy.
    origin_task_id: Optional[str] = None
    parent_skill_id: Optional[str] = None
    trust_score: float = 0.0
    execution_count: int = 0
    last_executed_at: Optional[datetime] = None
    signature: str = ""
    skill_dir: Optional[str] = None
    # MCP bridge fields — None for native skills
    origin: Optional[str] = None       # "mcp" for MCP-bridged tools
    mcp_server: Optional[str] = None   # server name (e.g. "github")
    mcp_tool: Optional[str] = None     # original tool name on the server

    # Progressive loading additions
    trigger_patterns: List[str] = field(default_factory=list)
    context_tokens_estimate: int = 2048
    lazy_loader: Optional[str] = None

    def compute_checksum(self, source_code: str) -> str:
        """Compute and store SHA-256 checksum of skill source."""
        digest = hashlib.sha256(source_code.encode()).hexdigest()
        self.checksum_sha256 = digest
        return digest

    def to_json(self) -> str:
        """Serialize to JSON for storage as colony.skill.json."""
        d = {
            "skill_id": self.skill_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author_colony_id": self.author_colony_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "status": self.status.value,
            "entry_point": self.entry_point,
            "permissions": {
                "capabilities": [c.value for c in self.permissions.capabilities],
                "allowed_domains": self.permissions.allowed_domains,
                "allowed_read_paths": self.permissions.allowed_read_paths,
                "allowed_write_paths": self.permissions.allowed_write_paths,
                "allowed_env_vars": self.permissions.allowed_env_vars,
                "max_duration_secs": self.permissions.max_duration_secs,
                "max_memory_mb": self.permissions.max_memory_mb,
            },
            "tags": self.tags,
            "dependencies": self.dependencies,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "checksum_sha256": self.checksum_sha256,
            "origin_task_id": self.origin_task_id,
            "parent_skill_id": self.parent_skill_id,
            "trust_score": self.trust_score,
            "execution_count": self.execution_count,
            "signature": self.signature,
            "trigger_patterns": self.trigger_patterns,
            "context_tokens_estimate": self.context_tokens_estimate,
            "lazy_loader": self.lazy_loader,
            "origin": self.origin,
            "mcp_server": self.mcp_server,
            "mcp_tool": self.mcp_tool,
        }
        return json.dumps(d, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "SkillManifest":
        """Deserialize from JSON."""
        d = json.loads(raw)
        perms_raw = d.get("permissions", {})
        perms = SkillPermissions(
            capabilities=[SkillCapability(c) for c in perms_raw.get("capabilities", [])],
            allowed_domains=perms_raw.get("allowed_domains", []),
            allowed_read_paths=perms_raw.get("allowed_read_paths", []),
            allowed_write_paths=perms_raw.get("allowed_write_paths", []),
            allowed_env_vars=perms_raw.get("allowed_env_vars", []),
            max_duration_secs=perms_raw.get("max_duration_secs", 60),
            max_memory_mb=perms_raw.get("max_memory_mb", 256),
        )
        return cls(
            skill_id=d["skill_id"],
            name=d["name"],
            version=d["version"],
            description=d["description"],
            author_colony_id=d["author_colony_id"],
            created_at=datetime.fromisoformat(d["created_at"]),
            updated_at=datetime.fromisoformat(d["updated_at"]),
            status=SkillStatus(d.get("status", "draft")),
            entry_point=d.get("entry_point", "skill:run"),
            permissions=perms,
            tags=d.get("tags", []),
            dependencies=d.get("dependencies", []),
            input_schema=d.get("input_schema", {}),
            output_schema=d.get("output_schema", {}),
            checksum_sha256=d.get("checksum_sha256", ""),
            origin_task_id=d.get("origin_task_id"),
            parent_skill_id=d.get("parent_skill_id"),
            trust_score=d.get("trust_score", 0.0),
            execution_count=d.get("execution_count", 0),
            signature=d.get("signature", ""),
            trigger_patterns=d.get("trigger_patterns", []),
            context_tokens_estimate=d.get("context_tokens_estimate", 2048),
            lazy_loader=d.get("lazy_loader"),
            origin=d.get("origin"),
            mcp_server=d.get("mcp_server"),
            mcp_tool=d.get("mcp_tool"),
        )


@dataclass
class TaskSolution:
    """A completed task solution that may be captured as a skill."""
    task_id: str
    task_description: str
    inputs: Dict[str, Any]
    output: Any
    trace: List[Dict[str, Any]]
    dependencies: List[str]
    embedding: Optional[List[float]]
    step_fingerprint: Optional[List[str]]
    duration_secs: float
    completed_at: datetime


@dataclass
class SkillSummary:
    """Lightweight skill summary for novelty scoring and search."""
    skill_id: str
    description: str
    tags: List[str]
    dependencies: List[str]
    embedding: Optional[List[float]] = None
    step_fingerprint: Optional[List[str]] = None
