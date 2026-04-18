"""Colony Self-Creating Skills — automated skill learning, packaging, registry, and marketplace."""

from colony_sidecar.skills.models import (
    SkillManifest,
    SkillPermissions,
    SkillCapability,
    SkillStatus,
    TaskSolution,
    SkillSummary,
)
from colony_sidecar.skills.registry import SkillRegistry
from colony_sidecar.skills.packager import SkillPackager
from colony_sidecar.skills.executor import SkillExecutor, ExecutionResult
from colony_sidecar.skills.versioning import check_schema_compatibility, bump_version
from colony_sidecar.skills.marketplace import SkillMarketplace, SkillOffer, FederationMessage

__all__ = [
    "SkillManifest",
    "SkillPermissions",
    "SkillCapability",
    "SkillStatus",
    "TaskSolution",
    "SkillSummary",
    "SkillRegistry",
    "SkillPackager",
    "SkillExecutor",
    "ExecutionResult",
    "check_schema_compatibility",
    "bump_version",
    "SkillMarketplace",
    "SkillOffer",
    "FederationMessage",
]
