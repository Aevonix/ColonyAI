"""Briefings seeder — creates a welcome briefing on first boot."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class BriefingsSeeder:
    name = "briefings"

    async def seed(self, corpus: Any) -> None:
        try:
            from colony_sidecar.briefings.store import BriefingStore
            from colony_sidecar.briefings.models import (
                Briefing,
                BriefingSection,
                BriefingType,
                BriefingStatus,
                BriefingPriority,
            )
        except ImportError as exc:
            logger.debug("briefings: import failed — skipping: %s", exc)
            return

        try:
            store = BriefingStore.get_instance()
        except Exception as exc:
            logger.debug("briefings: BriefingStore.get_instance() failed: %s", exc)
            return

        colony_id = corpus.colony_id
        briefing_id = f"briefing-bootstrap-{colony_id[:8]}"

        # Idempotency check
        try:
            existing = store.get(briefing_id)
            if existing is not None:
                logger.debug("briefings: welcome briefing already exists — skipping")
                return
        except Exception:
            pass

        now = datetime.now(timezone.utc)

        welcome = Briefing(
            briefing_id=briefing_id,
            briefing_type=BriefingType.TACTICAL,
            status=BriefingStatus.DRAFT,
            priority=BriefingPriority.NORMAL,
            triggered_by="identity_bootstrap",
            gateway=None,
            created_at=now,
        )

        welcome.sections = [
            BriefingSection(
                section_id=f"sec-bootstrap-identity-{colony_id[:8]}",
                name="Identity",
                content={
                    "colony_id": colony_id,
                    "colony_name": corpus.colony_name,
                    "colony_version": corpus.colony_version,
                    "network_id": corpus.network_id,
                },
                narrative=(
                    f"This is {corpus.colony_name} (Colony v{corpus.colony_version}), "
                    f"instance ID {colony_id}, on network {corpus.network_id}. "
                    "Bootstrap complete."
                ),
                priority=100,
            ),
            BriefingSection(
                section_id=f"sec-bootstrap-systems-{colony_id[:8]}",
                name="Active Systems",
                content={
                    "layers": [l.name for l in corpus.layers],
                    "subsystem_count": len(corpus.layers),
                    "api_endpoint_count": len(corpus.api_endpoints),
                },
                narrative=(
                    f"Colony has {len(corpus.layers)} architectural layers and "
                    f"{len(corpus.api_endpoints)} API endpoints ready."
                ),
                priority=80,
            ),
            BriefingSection(
                section_id=f"sec-bootstrap-safety-{colony_id[:8]}",
                name="Safety Status",
                content={
                    "gate_layers": [g.name for g in corpus.gate_layers],
                    "gate_layer_count": len(corpus.gate_layers),
                },
                narrative=(
                    f"ResponseGate pipeline active with {len(corpus.gate_layers)} safety layers. "
                    "All outbound content is filtered."
                ),
                priority=90,
            ),
        ]

        try:
            store.save(welcome)
            logger.info("briefings: welcome briefing seeded (id=%s)", briefing_id)
        except Exception as exc:
            logger.warning("briefings: failed to save welcome briefing: %s", exc)
