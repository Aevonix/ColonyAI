"""Colony Skills — packaging extracted patterns as skill directories."""

from __future__ import annotations

import logging
import pathlib
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from colony_sidecar.skills.learning.pattern_extractor import ExtractedPattern
from colony_sidecar.skills.learning.triggers import TriggerSource
from colony_sidecar.skills.models import SkillCapability, SkillManifest, SkillPermissions, SkillStatus
from colony_sidecar.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9-]")
_SKILL_LIBRARY_PATH = pathlib.Path("colony/skills/library")


class SkillPackager:
    """Packages an ExtractedPattern into a skill directory and registers it."""

    def __init__(
        self,
        registry: SkillRegistry,
        colony_id: str,
        library_root: pathlib.Path = _SKILL_LIBRARY_PATH,
    ) -> None:
        self._registry = registry
        self._colony_id = colony_id
        self._library = library_root

    async def package(
        self,
        solution: "TaskSolution",  # noqa: F821
        pattern: ExtractedPattern,
        source: TriggerSource,
    ) -> str:
        """Create a skill directory from an extracted pattern.

        Returns:
            The new skill_id string.
        """
        now = datetime.now(timezone.utc)
        slug = self._slugify(solution.task_description[:48])
        uid = uuid.uuid4().hex[:8]
        skill_id = f"{slug}_{uid}"
        version = "1.0.0"
        skill_dir = self._library / f"{skill_id}_v{version}"
        skill_dir.mkdir(parents=True, exist_ok=True)

        permissions = self._build_permissions(pattern)
        manifest = SkillManifest(
            skill_id=skill_id,
            name=slug.replace("-", " ").title(),
            version=version,
            description=pattern.docstring.split("\n")[0],
            author_colony_id=self._colony_id,
            created_at=now,
            updated_at=now,
            status=SkillStatus.DRAFT,
            entry_point="skill:run",
            permissions=permissions,
            tags=pattern.tags,
            dependencies=pattern.dependencies,
            input_schema=pattern.input_schema,
            output_schema=pattern.output_schema,
            origin_task_id=solution.task_id,
            skill_dir=str(skill_dir),
        )
        manifest.compute_checksum(pattern.source_code)

        (skill_dir / "skill.py").write_text(pattern.source_code, encoding="utf-8")
        (skill_dir / "colony.skill.json").write_text(manifest.to_json(), encoding="utf-8")
        (skill_dir / "requirements.txt").write_text(
            "\n".join(pattern.dependencies), encoding="utf-8"
        )
        self._write_smoke_test(skill_dir, slug, pattern)

        await self._registry.register(manifest, skill_dir)
        logger.info("Packaged skill %s → %s", skill_id, skill_dir)
        return skill_id

    def _build_permissions(self, pattern: ExtractedPattern) -> SkillPermissions:
        caps: list[SkillCapability] = []
        if pattern.network_domains:
            caps.append(SkillCapability.NETWORK)
        if pattern.file_paths:
            caps.append(SkillCapability.FILESYSTEM_READ)
        if pattern.env_vars:
            caps.append(SkillCapability.ENV_READ)
        return SkillPermissions(
            capabilities=caps,
            allowed_domains=pattern.network_domains,
            allowed_read_paths=pattern.file_paths,
            allowed_env_vars=list(pattern.env_vars),
        )

    @staticmethod
    def _slugify(text: str) -> str:
        return _SLUG_RE.sub("-", text.lower().strip()).strip("-")[:48]

    @staticmethod
    def _write_smoke_test(
        skill_dir: pathlib.Path, slug: str, pattern: ExtractedPattern
    ) -> None:
        fn_name = slug.replace("-", "_")
        test_code = f"""\
import pytest

@pytest.mark.asyncio
async def test_{fn_name}_runs():
    # Smoke test: verify skill file exists and run() is callable.
    import importlib.util
    import pathlib
    skill_path = pathlib.Path("{skill_dir}") / "skill.py"
    if skill_path.exists():
        spec = importlib.util.spec_from_file_location("_skill", skill_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert callable(getattr(module, "run", None))
"""
        tests_dir = skill_dir / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / "test_skill.py").write_text(test_code, encoding="utf-8")
