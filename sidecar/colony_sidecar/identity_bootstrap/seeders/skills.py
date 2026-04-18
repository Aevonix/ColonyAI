"""Skills seeder — registers the bootstrap_self_check skill in the skill registry."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class SkillsSeeder:
    name = "skills"

    def __init__(self, skill_registry: Optional[Any] = None) -> None:
        self._registry = skill_registry

    async def seed(self, corpus: Any) -> None:
        registry = self._registry
        if registry is None:
            logger.debug("skills: no registry — skipping")
            return

        try:
            from colony_sidecar.skills.models import (
                SkillManifest,
                SkillStatus,
                SkillPermissions,
                SkillCapability,
            )
        except ImportError as exc:
            logger.debug("skills: import failed — skipping: %s", exc)
            return

        colony_id = corpus.colony_id
        skill_id = "bootstrap-self-check"
        now = datetime.now(timezone.utc)

        # Check idempotency
        try:
            existing = await registry.get(skill_id)
            if existing is not None:
                logger.debug("skills: bootstrap_self_check already registered — skipping")
                return
        except Exception:
            pass

        manifest = SkillManifest(
            skill_id=skill_id,
            name="bootstrap_self_check",
            version="1.0.0",
            description=(
                "Runs the Colony Identity Bootstrap self-check matrix (16 checks). "
                "Returns a verification report showing which systems are healthy."
            ),
            author_colony_id=colony_id,
            created_at=now,
            updated_at=now,
            status=SkillStatus.ACTIVE,
            entry_point="colony.identity_bootstrap.skill:run",
            permissions=SkillPermissions(
                capabilities=[SkillCapability.FILESYSTEM_READ],
                max_duration_secs=30,
                max_memory_mb=64,
            ),
            tags=["system", "bootstrap", "self-check", "health"],
            dependencies=[],
            input_schema={
                "type": "object",
                "properties": {
                    "colony_id": {"type": "string"},
                    "mode": {"type": "string", "enum": ["full", "quick"], "default": "full"},
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "verified_systems": {"type": "array", "items": {"type": "string"}},
                    "failed_systems": {"type": "array", "items": {"type": "string"}},
                    "anomalies": {"type": "array"},
                    "success": {"type": "boolean"},
                },
            },
            checksum_sha256="bootstrap-internal",
            trust_score=1.0,
        )

        try:
            await registry.register(manifest, skill_dir=None)
            logger.info("skills: bootstrap_self_check skill registered")
        except Exception as exc:
            logger.warning("skills: registry.register failed: %s", exc)
