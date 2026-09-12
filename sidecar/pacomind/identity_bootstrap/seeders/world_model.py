"""World Model seeder — creates self + subsystem ConceptEntities in the SQLite world model."""

from __future__ import annotations

import logging
import time
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _wm_id() -> str:
    ts = int(time.time() * 1000)
    rand = secrets.token_hex(4)
    return f"we-{ts}-{rand}"


def _wr_id() -> str:
    ts = int(time.time() * 1000)
    rand = secrets.token_hex(4)
    return f"wr-{ts}-{rand}"


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


class WorldModelSeeder:
    name = "world_model"

    async def seed(self, corpus: Any) -> None:
        """Seed self-knowledge entities into the world model."""
        try:
            import pacomind.api.routers.world as wm_mod
            backend = getattr(wm_mod, "_wm_backend", None)
        except ImportError:
            logger.debug("world_model: router not importable — skipping")
            return

        if backend is None:
            logger.debug("world_model: _wm_backend is None — skipping")
            return

        from pacomind.world_model.entities import ConceptEntity
        from pacomind.world_model.relationships import WorldRelationship

        pacomind_id = corpus.pacomind_id
        now = _now_dt()

        # Self entity
        self_id = f"pacomind-self-{pacomind_id}"
        self_entity = ConceptEntity(
            id=self_id,
            name=corpus.pacomind_name,
            entity_type="concept",
            concept_type="pacomind_instance",
            aliases=[pacomind_id, "self", corpus.pacomind_name],
            confidence=1.0,
            properties={
                "pacomind_id": pacomind_id,
                "pacomind_version": corpus.pacomind_version,
                "network_id": corpus.network_id,
                "public_key_hex": corpus.public_key_hex,
                "corpus_version": corpus.corpus_version,
            },
            first_seen=now,
            last_seen=now,
            created_at=now,
            updated_at=now,
        )
        try:
            await backend.upsert_entity(self_entity)
        except Exception as exc:
            logger.warning("world_model: failed to upsert self entity: %s", exc)
            return

        # Subsystem concept entities + WM_PART_OF relationships
        from pacomind.identity_bootstrap.corpus import SUBSYSTEMS
        for subsystem in SUBSYSTEMS:
            sub_id = f"pacomind-subsystem-{pacomind_id}-{subsystem}"
            sub_entity = ConceptEntity(
                id=sub_id,
                name=f"{subsystem} subsystem",
                entity_type="concept",
                concept_type="pacomind_subsystem",
                aliases=[subsystem],
                confidence=1.0,
                properties={
                    "subsystem_name": subsystem,
                    "parent_pacomind_id": pacomind_id,
                },
                first_seen=now,
                last_seen=now,
                created_at=now,
                updated_at=now,
            )
            try:
                await backend.upsert_entity(sub_entity)
            except Exception as exc:
                logger.warning("world_model: failed to upsert subsystem %s: %s", subsystem, exc)
                continue

            # WM_PART_OF: subsystem → self
            rel_part_of = WorldRelationship(
                id=_wr_id(),
                source_id=sub_id,
                target_id=self_id,
                relationship_type="WM_PART_OF",
                confidence=1.0,
                properties={"seeded_by": "identity_bootstrap"},
            )
            try:
                await backend.upsert_relationship(rel_part_of)
            except Exception as exc:
                logger.warning("world_model: WM_PART_OF rel failed for %s: %s", subsystem, exc)

        # WM_DEPENDS_ON: goals → task_queue, intelligence → world_model
        dep_pairs = [
            ("goals", "task_queue"),
            ("intelligence", "world_model"),
            ("briefings", "goals"),
            ("skills", "task_queue"),
            ("federation", "world_model"),
        ]
        for src_sub, tgt_sub in dep_pairs:
            src_id = f"pacomind-subsystem-{pacomind_id}-{src_sub}"
            tgt_id = f"pacomind-subsystem-{pacomind_id}-{tgt_sub}"
            rel = WorldRelationship(
                id=_wr_id(),
                source_id=src_id,
                target_id=tgt_id,
                relationship_type="WM_DEPENDS_ON",
                confidence=0.95,
                properties={"seeded_by": "identity_bootstrap"},
            )
            try:
                await backend.upsert_relationship(rel)
            except Exception as exc:
                logger.debug("world_model: WM_DEPENDS_ON %s→%s failed: %s", src_sub, tgt_sub, exc)

        logger.info("world_model: seeded self + %d subsystems", len(SUBSYSTEMS))
