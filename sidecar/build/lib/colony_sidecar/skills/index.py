"""Colony Skills — lightweight in-memory skill index for progressive loading."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from colony_sidecar.skills.models import SkillManifest, SkillStatus
from colony_sidecar.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SkillEntry:
    """Minimal in-memory representation of one skill."""

    skill_id: str
    name: str
    description: str
    trigger_patterns: List[str]
    context_tokens_estimate: int
    dependencies: List[str]
    lazy_loader: Optional[str]
    skill_dir: Path
    trust_score: float
    execution_count: int
    _patterns: List[re.Pattern] = field(default_factory=list, repr=False)

    def compile_patterns(self) -> None:
        """Compile trigger_patterns strings into re.Pattern objects."""
        compiled = []
        for pat in self.trigger_patterns:
            try:
                compiled.append(re.compile(pat, re.IGNORECASE))
            except re.error as exc:
                logger.warning(
                    "Skill %s: invalid trigger pattern %r: %s", self.skill_id, pat, exc
                )
        self._patterns = compiled

    def matches(self, text: str) -> bool:
        """Return True if any compiled pattern matches text."""
        return any(p.search(text) for p in self._patterns)


class SkillIndex:
    """Lightweight, always-resident index of skill manifests.

    Built from the SQLite registry without importing skill modules.
    Thread-safe for concurrent reads; uses asyncio.Lock for mutations.
    """

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry
        self._entries: dict[str, SkillEntry] = {}
        self._lock = asyncio.Lock()

    async def build(self) -> None:
        """Scan registry for ACTIVE skills; build SkillEntry list."""
        manifests = await self._registry.list_all(status=SkillStatus.ACTIVE)
        async with self._lock:
            self._entries = {}
            for m in manifests:
                entry = self._manifest_to_entry(m)
                entry.compile_patterns()
                self._entries[entry.skill_id] = entry
        logger.info("SkillIndex: built %d entries", len(self._entries))

    def match(self, text: str) -> List[SkillEntry]:
        """Return SkillEntries whose trigger_patterns match text.

        Pure regex — O(skills × patterns), no I/O.
        Results sorted by trust_score DESC.
        """
        matched = [e for e in self._entries.values() if e.matches(text)]
        matched.sort(key=lambda e: e.trust_score, reverse=True)
        return matched

    async def register(self, manifest: SkillManifest, skill_dir: Path) -> None:
        """Add or update entry after a new skill is packaged."""
        entry = self._manifest_to_entry(manifest, skill_dir)
        entry.compile_patterns()
        async with self._lock:
            self._entries[entry.skill_id] = entry

    async def evict(self, skill_id: str) -> None:
        """Remove entry (called when a skill is archived/quarantined)."""
        async with self._lock:
            self._entries.pop(skill_id, None)

    def all_entries(self) -> List[SkillEntry]:
        """Return all entries for budget planning."""
        return list(self._entries.values())

    def get(self, skill_id: str) -> Optional[SkillEntry]:
        """Return entry by skill_id, or None."""
        return self._entries.get(skill_id)

    def __len__(self) -> int:
        return len(self._entries)

    @staticmethod
    def _manifest_to_entry(
        m: SkillManifest, skill_dir: Optional[Path] = None
    ) -> SkillEntry:
        resolved_dir = skill_dir or Path(m.skill_dir or "")
        return SkillEntry(
            skill_id=m.skill_id,
            name=m.name,
            description=m.description,
            trigger_patterns=list(m.trigger_patterns),
            context_tokens_estimate=m.context_tokens_estimate,
            dependencies=list(m.dependencies),
            lazy_loader=m.lazy_loader,
            skill_dir=resolved_dir,
            trust_score=m.trust_score,
            execution_count=m.execution_count,
        )
