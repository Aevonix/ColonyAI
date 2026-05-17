"""Data quality executor skill.

Monitors schema drift, orphaned records, and data inconsistencies.
Attempts deterministic auto-fixes, proposes ambiguous ones.
"""

import logging
from typing import Any, Dict

from colony_sidecar.skills.base import (
    ExecutionResult,
    InitiativeExecutionContext,
    InitiativeExecutorSkill,
)

logger = logging.getLogger(__name__)


class DataQualitySkill(InitiativeExecutorSkill):
    """Skill for monitoring and fixing data quality issues."""

    skill_name = "data_quality"
    skill_version = "1.0.0"

    async def can_execute(
        self, category: Dict[str, Any], context: Dict[str, Any]
    ) -> bool:
        return category.get("executor_skill") == self.skill_name

    async def execute(
        self, initiative: InitiativeExecutionContext
    ) -> ExecutionResult:
        entity_id = initiative.entity_id or "unknown"
        entity_type = initiative.entity_type or "schema"
        self._log("info", "Checking data quality: %s (%s)", entity_id, entity_type)

        if entity_type == "schema_drift":
            return await self._handle_schema_drift(entity_id, initiative.trigger_data)
        elif entity_type == "orphan_nodes":
            return await self._handle_orphan_nodes(entity_id, initiative.trigger_data)
        elif entity_type == "stale_index":
            return await self._handle_stale_index(entity_id, initiative.trigger_data)
        else:
            self._log("warning", "Unknown data quality issue type: %s", entity_type)
            return ExecutionResult.NO_ACTION

    async def _handle_schema_drift(
        self, entity_id: str, trigger_data: Dict[str, Any]
    ) -> ExecutionResult:
        """Handle detected schema drift."""
        self._log("info", "Handling schema drift: %s", entity_id)

        drift = trigger_data.get("drift", {})
        missing_relationships = drift.get("missing_relationships", [])
        missing_properties = drift.get("missing_properties", [])

        fixes_applied = []
        fixes_proposed = []

        # Auto-fix: create missing relationships if source/target exist
        for rel in missing_relationships:
            rel_type = rel.get("type")
            from_node = rel.get("from")
            to_node = rel.get("to")
            if not all([rel_type, from_node, to_node]):
                continue

            if self.graph:
                try:
                    async with self.graph.driver.session(
                        database=self.graph.database
                    ) as session:
                        # Check if nodes exist before creating relationship
                        result = await session.run(
                            "MATCH (a {id: $from_id}), (b {id: $to_id}) "
                            "RETURN count(a) as a_count, count(b) as b_count",
                            from_id=from_node,
                            to_node=to_node,
                        )
                        record = await result.single()
                        if record and record.get("a_count") > 0 and record.get("b_count") > 0:
                            # Safe to create relationship
                            await session.run(
                                "MATCH (a {id: $from_id}), (b {id: $to_id}) "
                                "MERGE (a)-[r:" + rel_type + "]->(b) "
                                "RETURN count(r) as created",
                                from_id=from_node,
                                to_node=to_node,
                            )
                            fixes_applied.append(rel)
                        else:
                            fixes_proposed.append(rel)
                except Exception as e:
                    self._log("warning", "Failed to fix relationship %s: %s", rel, e)
                    fixes_proposed.append(rel)

        # Auto-fix: set default values for missing properties
        for prop in missing_properties:
            node_id = prop.get("node_id")
            prop_name = prop.get("property")
            default_value = prop.get("default")
            if not all([node_id, prop_name]):
                continue

            if self.graph:
                try:
                    async with self.graph.driver.session(
                        database=self.graph.database
                    ) as session:
                        await session.run(
                            "MATCH (n {id: $node_id}) "
                            "SET n." + prop_name + " = $default_value",
                            node_id=node_id,
                            default_value=default_value,
                        )
                        fixes_applied.append(prop)
                except Exception as e:
                    self._log("warning", "Failed to set property %s: %s", prop, e)
                    fixes_proposed.append(prop)

        if fixes_applied and not fixes_proposed:
            self._log("info", "Auto-fixed %d schema issues", len(fixes_applied))
            return ExecutionResult.AUTO_FIXED
        elif fixes_proposed:
            self._log("info", "Proposed %d fixes, applied %d", len(fixes_proposed), len(fixes_applied))
            return ExecutionResult.PROPOSAL_CREATED
        else:
            return ExecutionResult.NO_ACTION

    async def _handle_orphan_nodes(
        self, entity_id: str, trigger_data: Dict[str, Any]
    ) -> ExecutionResult:
        """Handle orphaned nodes (e.g., Memory without :ABOUT edges)."""
        self._log("info", "Handling orphan nodes: %s", entity_id)
        orphan_count = trigger_data.get("count", 0)

        if orphan_count == 0:
            return ExecutionResult.NO_ACTION

        # For now, just log and report. In the future, could attempt
        # to link orphans to relevant Person nodes via content analysis.
        self._log("info", "Found %d orphan nodes", orphan_count)
        return ExecutionResult.PROPOSAL_CREATED

    async def _handle_stale_index(
        self, entity_id: str, trigger_data: Dict[str, Any]
    ) -> ExecutionResult:
        """Handle stale indexes (e.g., LanceDB)."""
        self._log("info", "Handling stale index: %s", entity_id)
        index_age_days = trigger_data.get("age_days", 0)

        if index_age_days < 7:
            return ExecutionResult.NO_ACTION

        # Trigger re-index via event bus
        if self.events:
            try:
                await self.events.publish("index_rebuild_requested", {
                    "index_name": entity_id,
                    "reason": f"stale ({index_age_days} days)",
                })
                self._log("info", "Requested re-index for %s", entity_id)
                return ExecutionResult.AUTO_FIXED
            except Exception as e:
                self._log("warning", "Failed to request re-index: %s", e)

        return ExecutionResult.FAILED
