"""Colony Skills — typed Protocol interfaces for dependency injection."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from colony_sidecar.skills.models import SkillManifest, SkillStatus
from colony_sidecar.skills.learning.novelty_detector import NoveltyResult
from colony_sidecar.skills.learning.pattern_extractor import ExtractedPattern
from colony_sidecar.skills.security.scanner import ASTScanResult
from colony_sidecar.skills.executor import ExecutionResult


@runtime_checkable
class ISkillRegistry(Protocol):
    """Read/write interface to the skill registry."""

    async def register(self, manifest: SkillManifest, skill_dir: Any) -> None: ...
    async def get(self, skill_id: str) -> Optional[SkillManifest]: ...
    async def search(
        self,
        query: str = "",
        tags: Optional[List[str]] = None,
        status: Optional[SkillStatus] = None,
        limit: int = 50,
    ) -> List[SkillManifest]: ...
    async def activate(self, skill_id: str) -> None: ...
    async def deactivate(self, skill_id: str) -> None: ...
    async def quarantine(self, skill_id: str, reason: str) -> None: ...
    async def list_summaries(self) -> List[Any]: ...


@runtime_checkable
class INoveltyDetector(Protocol):
    """Scores how novel a task solution is relative to the skill registry."""

    async def score(self, solution: Any) -> NoveltyResult: ...


@runtime_checkable
class IPatternExtractor(Protocol):
    """Extracts a generalizable pattern from a task solution."""

    def extract(self, solution: Any) -> ExtractedPattern: ...


@runtime_checkable
class ISkillPackager(Protocol):
    """Packages an extracted pattern as a skill directory."""

    async def package(self, solution: Any, pattern: ExtractedPattern, source: Any) -> str: ...


@runtime_checkable
class IASTScanner(Protocol):
    """Statically analyzes skill Python source for security issues."""

    def scan(self, source: str, skill_id: str) -> ASTScanResult: ...


@runtime_checkable
class ISkillExecutor(Protocol):
    """Executes a skill by ID inside a capability-gated sandbox."""

    async def invoke(
        self,
        skill_id: str,
        inputs: Dict[str, Any],
        caller_context: Optional[str] = None,
    ) -> ExecutionResult: ...


@runtime_checkable
class ISkillMarketplace(Protocol):
    """Publishes and receives skills over the federation layer."""

    async def publish_skill(self, skill_id: str) -> int: ...
    async def receive_skill_offer(self, msg: Any) -> Optional[str]: ...
