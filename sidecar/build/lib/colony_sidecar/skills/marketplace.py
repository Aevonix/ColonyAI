"""Colony Skills — federation-based skill marketplace."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from colony_sidecar.skills.models import SkillManifest, SkillStatus
from colony_sidecar.skills.registry import SkillRegistry
from colony_sidecar.skills.security.scanner import ASTScanner

logger = logging.getLogger(__name__)

_SKILL_OFFER_TYPE = "skill_offer"
_SKILL_PULL_TYPE = "skill_pull_request"
_SKILL_ATTESTATION_TYPE = "skill_attestation"


@dataclass
class SkillOffer:
    """A skill advertisement broadcast over federation."""
    skill_id: str
    name: str
    version: str
    description: str
    tags: List[str]
    trust_score: float
    checksum_sha256: str
    author_colony_id: str
    manifest_json: str
    source_url: Optional[str] = None


@dataclass
class FederationMessage:
    """Lightweight federation message for skill marketplace communication.

    In production, this is replaced by the real FederationEnvelope from
    colony.federation.models. This class exists to allow the marketplace
    to be tested without a full federation stack.
    """
    type: str
    from_colony_id: str
    payload: Dict[str, Any]


class SkillMarketplace:
    """Bridges the skill registry with the federation layer.

    Responsibilities:
      - Publish locally-available skills to trusted federation peers.
      - Receive and validate incoming skill offers.
      - Handle pull requests from peers wanting a skill's source.
      - Exchange and aggregate trust attestations.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        federation: Any,  # FederationManager or compatible mock
        scanner: ASTScanner,
        colony_id: str,
        auto_install_threshold: float = 0.80,
    ) -> None:
        self._registry = registry
        self._federation = federation
        self._scanner = scanner
        self._colony_id = colony_id
        self._auto_threshold = auto_install_threshold

    async def publish_skill(self, skill_id: str) -> int:
        """Broadcast a skill offer to all trusted peers.

        Returns the number of peers the offer was sent to.

        Raises:
            ValueError: If the skill is not active or trust score is too low.
        """
        manifest = await self._registry.get(skill_id)
        if not manifest or manifest.status != SkillStatus.ACTIVE:
            raise ValueError(f"Skill '{skill_id}' is not active; cannot publish.")
        if manifest.trust_score < 0.70:
            raise ValueError(
                f"Skill trust score {manifest.trust_score:.2f} is below publish threshold 0.70."
            )

        offer = SkillOffer(
            skill_id=manifest.skill_id,
            name=manifest.name,
            version=manifest.version,
            description=manifest.description,
            tags=manifest.tags,
            trust_score=manifest.trust_score,
            checksum_sha256=manifest.checksum_sha256,
            author_colony_id=self._colony_id,
            manifest_json=manifest.to_json(),
        )
        msg = FederationMessage(
            type=_SKILL_OFFER_TYPE,
            from_colony_id=self._colony_id,
            payload=offer.__dict__,
        )
        sent = await self._federation.broadcast_to_trusted(msg)
        logger.info("Published skill %s to %d peers.", skill_id, sent)
        return sent

    async def receive_skill_offer(self, msg: FederationMessage) -> Optional[str]:
        """Handle an incoming skill_offer message from a peer colony.

        Returns the skill_id if accepted, None if rejected.
        """
        payload = msg.payload
        offer = SkillOffer(**payload)

        # Trust gate: only accept from trusted peers
        if not await self._federation.is_trusted(msg.from_colony_id):
            logger.warning(
                "Rejected skill offer from untrusted colony %s.", msg.from_colony_id
            )
            return None

        manifest = SkillManifest.from_json(offer.manifest_json)

        if offer.trust_score >= self._auto_threshold:
            await self._registry.register(manifest, skill_dir=None)
            await self._registry.activate(manifest.skill_id)
            logger.info(
                "Auto-installed skill %s (trust %.2f ≥ threshold %.2f).",
                manifest.skill_id, offer.trust_score, self._auto_threshold,
            )
        else:
            await self._registry.register(manifest, skill_dir=None)
            logger.info(
                "Skill %s received as DRAFT; requires confirmation (trust %.2f).",
                manifest.skill_id, offer.trust_score,
            )
        return manifest.skill_id

    async def compute_trust_score(
        self,
        skill_id: str,
        author_colony_reputation: float = 0.5,
        attestation_count: int = 0,
        days_since_publish: int = 0,
        local_execution_count: int = 0,
        scan_clean: bool = True,
        report_count: int = 0,
        runtime_violations: int = 0,
    ) -> float:
        """Compute a composite trust score for a skill.

        Weighted factors as per spec §8.3:
          - author_colony_reputation  0.25
          - attestation_count         0.20 (capped at 1.0 after 10 attestations)
          - days_since_publish        0.15 (capped at 1.0 after 365 days)
          - local_execution_count     0.15 (capped at 1.0 after 100 executions)
          - scan_clean                0.10
          - report_count (negative)  -0.15 per report (capped at -0.15)
          - no_runtime_violations     0.10

        Returns:
            Float clamped to [0.0, 1.0].
        """
        score = 0.0
        score += 0.25 * min(1.0, max(0.0, author_colony_reputation))
        score += 0.20 * min(1.0, attestation_count / 10.0)
        score += 0.15 * min(1.0, days_since_publish / 365.0)
        score += 0.15 * min(1.0, local_execution_count / 100.0)
        score += 0.10 * (1.0 if scan_clean else 0.0)
        score -= 0.15 * min(1.0, report_count / 1.0)
        score += 0.10 * (1.0 if runtime_violations == 0 else 0.0)
        return max(0.0, min(1.0, score))
