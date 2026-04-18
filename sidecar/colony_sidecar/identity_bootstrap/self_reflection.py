"""Colony Identity Bootstrap — SelfReflectionComponent.

Provides a lightweight self-reflection entry point for the CIB system.
A full self-reflection run analyses the bootstrap report anomalies
and produces an improvement proposal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SelfReflectionResult:
    """Output of one self-reflection cycle."""
    colony_id: str
    health_score: float          # 0.0–1.0; 1.0 = all checks passed
    critical_anomaly_count: int
    warning_anomaly_count: int
    improvement_proposals: List[str] = field(default_factory=list)
    raw_notes: str = ""

    @property
    def healthy(self) -> bool:
        return self.critical_anomaly_count == 0 and self.health_score >= 0.8


class SelfReflectionComponent:
    """Analyses bootstrap anomalies and proposes improvements.

    This is a lightweight reflection component — it does not call an LLM.
    It uses rule-based heuristics over the anomaly list to generate proposals.
    """

    def __init__(self, metrics_collector: Optional[Any] = None) -> None:
        self._metrics = metrics_collector

    async def reflect(
        self,
        corpus: Any,
        anomalies: List[Any],
    ) -> SelfReflectionResult:
        """Reflect on the given anomaly list and return a result."""
        colony_id = corpus.colony_id
        critical = [a for a in anomalies if a.severity == "CRITICAL"]
        warnings = [a for a in anomalies if a.severity == "WARNING"]

        total_checks = 18
        passed = total_checks - len(anomalies)
        health_score = max(0.0, passed / total_checks)

        proposals = self._generate_proposals(critical, warnings)

        notes = self._build_notes(colony_id, corpus, critical, warnings, health_score)

        result = SelfReflectionResult(
            colony_id=colony_id,
            health_score=health_score,
            critical_anomaly_count=len(critical),
            warning_anomaly_count=len(warnings),
            improvement_proposals=proposals,
            raw_notes=notes,
        )

        # Record metric if available
        if self._metrics is not None:
            try:
                await self._metrics.record(
                    metric_type="bootstrap_health_score",
                    value=health_score,
                    domain="identity",
                    context={"colony_id": colony_id},
                )
            except Exception as exc:
                logger.debug("self_reflection: metrics record failed: %s", exc)

        logger.info(
            "self_reflection: colony=%s health=%.2f critical=%d warnings=%d",
            colony_id,
            health_score,
            len(critical),
            len(warnings),
        )
        return result

    def _generate_proposals(self, critical: List[Any], warnings: List[Any]) -> List[str]:
        proposals: List[str] = []

        systems_with_issues = {a.system for a in critical + warnings}

        if "world_model" in systems_with_issues:
            proposals.append(
                "Re-run WorldModelSeeder: world model entity or relationship is missing."
            )
        if "contacts" in systems_with_issues:
            proposals.append(
                "Re-run RelationshipSeeder: self-contact not found in contacts store."
            )
        if "memory" in systems_with_issues:
            proposals.append(
                "Re-run MemorySeeder: foundational memory entries are missing."
            )
        if "goals" in systems_with_issues:
            proposals.append(
                "Re-run GoalsSeeder: system goals not found — GoalStore may have been reset."
            )
        if "briefings" in systems_with_issues:
            proposals.append(
                "Re-run BriefingsSeeder: welcome briefing not found."
            )
        if "sessions" in systems_with_issues:
            proposals.append(
                "Re-run SessionsSeeder: bootstrap session record not found."
            )
        if "corpus" in systems_with_issues:
            proposals.append(
                "Update the identity_bootstrap corpus to match the current Colony version."
            )
        if any(a.check == "colony_id_set" for a in critical):
            proposals.append(
                "Set COLONY_ID env var or ensure ChainManager is initialized before bootstrap."
            )

        if not proposals and not critical and not warnings:
            proposals.append("All checks passed — no improvements required.")

        return proposals

    def _build_notes(
        self,
        colony_id: str,
        corpus: Any,
        critical: List[Any],
        warnings: List[Any],
        health_score: float,
    ) -> str:
        lines = [
            f"Colony: {corpus.colony_name} ({colony_id})",
            f"Version: {corpus.colony_version}",
            f"Health: {health_score:.0%}",
            f"Critical: {len(critical)}  Warnings: {len(warnings)}",
        ]
        if critical:
            lines.append("--- CRITICAL ---")
            for a in critical:
                lines.append(f"  [{a.system}] {a.check}: expected={a.expected!r} actual={a.actual!r}")
        if warnings:
            lines.append("--- WARNINGS ---")
            for a in warnings:
                lines.append(f"  [{a.system}] {a.check}: expected={a.expected!r} actual={a.actual!r}")
        return "\n".join(lines)
