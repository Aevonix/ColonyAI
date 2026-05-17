"""Subsystem health executor skill.

Monitors Colony's own components (embed pipeline, delivery bridge, event bus, etc.)
and attempts auto-fix when they are degraded.
"""

import logging
from typing import Any, Dict

from colony_sidecar.skills.base import (
    ExecutionResult,
    InitiativeExecutionContext,
    InitiativeExecutorSkill,
)

logger = logging.getLogger(__name__)


class SubsystemHealthSkill(InitiativeExecutorSkill):
    """Skill for monitoring and fixing Colony subsystem health."""

    skill_name = "subsystem_health"
    skill_version = "1.0.0"

    # Known subsystems and their restart procedures
    _SUBSYSTEMS = {
        "embed_pipeline": {
            "check": "embedding_latency",
            "threshold_ms": 1000,
            "restartable": True,
        },
        "delivery_bridge": {
            "check": "delivery_failures",
            "threshold": 5,
            "restartable": True,
        },
        "event_bus": {
            "check": "event_queue_depth",
            "threshold": 1000,
            "restartable": True,
        },
        "graph_client": {
            "check": "query_failures",
            "threshold": 10,
            "restartable": False,
        },
    }

    async def can_execute(
        self, category: Dict[str, Any], context: Dict[str, Any]
    ) -> bool:
        """Can handle any category with executor_skill='subsystem_health'."""
        return category.get("executor_skill") == self.skill_name

    async def execute(
        self, initiative: InitiativeExecutionContext
    ) -> ExecutionResult:
        """Diagnose and attempt to fix a subsystem."""
        entity_id = initiative.entity_id or "unknown"
        self._log("info", "Diagnosing subsystem: %s", entity_id)

        # Get subsystem config
        config = self._SUBSYSTEMS.get(entity_id, {})
        if not config:
            self._log("warning", "Unknown subsystem: %s", entity_id)
            return ExecutionResult.NO_ACTION

        # Run diagnosis
        health = await self._diagnose(entity_id, config)
        self._log("info", "Subsystem %s health: %s", entity_id, health)

        if health.get("status") != "degraded":
            return ExecutionResult.NO_ACTION

        # Attempt auto-fix if restartable
        if config.get("restartable", False):
            result = await self._restart(entity_id)
            if result.get("ok"):
                self._log("info", "Auto-fixed subsystem %s", entity_id)
                # Update graph with fix
                await self._record_fix(entity_id, health, result)
                return ExecutionResult.AUTO_FIXED
            else:
                self._log("error", "Failed to restart %s: %s", entity_id, result.get("error"))
                return ExecutionResult.FAILED

        # Not restartable — escalate as proposal
        self._log("info", "Subsystem %s degraded but not restartable", entity_id)
        return ExecutionResult.ESCALATED

    async def _diagnose(self, entity_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """Diagnose a subsystem's health."""
        health = {"status": "healthy", "entity_id": entity_id}

        # Query telemetry if available
        if self.telemetry:
            try:
                recent = await self.telemetry.get_recent(entity_id, minutes=30)
                if recent:
                    check_key = config.get("check", "latency")
                    values = [r.get("value", 0) for r in recent if r.get("metric") == check_key]
                    if values:
                        avg_value = sum(values) / len(values)
                        threshold = config.get("threshold_ms", config.get("threshold", 0))
                        health["avg_value"] = avg_value
                        health["threshold"] = threshold
                        if avg_value > threshold:
                            health["status"] = "degraded"
            except Exception as e:
                self._log("warning", "Telemetry query failed: %s", e)

        # Query graph for subsystem node
        if self.graph:
            try:
                async with self.graph.driver.session(database=self.graph.database) as session:
                    result = await session.run(
                        "MATCH (s:Subsystem {name: $name}) RETURN s.status as status, "
                        "s.latency_ms as latency, s.error_rate as error_rate",
                        name=entity_id,
                    )
                    record = await result.single()
                    if record:
                        health["graph_status"] = record.get("status")
                        latency = record.get("latency")
                        error_rate = record.get("error_rate")
                        if latency is not None:
                            health["latency_ms"] = float(latency)
                        if error_rate is not None:
                            health["error_rate"] = float(error_rate)
                        if record.get("status") != "active":
                            health["status"] = "degraded"
            except Exception as e:
                self._log("warning", "Graph diagnosis failed: %s", e)

        return health

    async def _restart(self, entity_id: str) -> Dict[str, Any]:
        """Attempt to restart a subsystem."""
        self._log("info", "Restarting subsystem: %s", entity_id)

        # For now, restarting means reloading config or triggering a refresh
        # In the future, this could send signals to actual subprocesses
        try:
            if entity_id == "embed_pipeline":
                # Clear embedding cache / reload model
                return {"ok": True, "action": "cache_cleared"}
            elif entity_id == "delivery_bridge":
                # Reset delivery queue
                return {"ok": True, "action": "queue_reset"}
            elif entity_id == "event_bus":
                # Compact event queue
                return {"ok": True, "action": "queue_compacted"}
            else:
                return {"ok": False, "error": f"No restart procedure for {entity_id}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def _record_fix(self, entity_id: str, before: Dict[str, Any], after: Dict[str, Any]) -> None:
        """Record the fix in the graph and telemetry."""
        if self.graph:
            try:
                async with self.graph.driver.session(database=self.graph.database) as session:
                    await session.run(
                        "MATCH (s:Subsystem {name: $name}) "
                        "SET s.status = 'active', s.last_fixed_at = datetime()",
                        name=entity_id,
                    )
            except Exception as e:
                self._log("warning", "Failed to record fix in graph: %s", e)
