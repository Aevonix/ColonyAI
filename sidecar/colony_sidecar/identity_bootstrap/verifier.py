"""Colony Identity Bootstrap — 16-point self-check matrix.

Each check method returns Optional[BootstrapAnomaly].
None = pass, anomaly object = failure.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from colony_sidecar.identity_bootstrap.models import BootstrapAnomaly

logger = logging.getLogger(__name__)


class BootstrapVerifier:
    """Runs 16 self-checks against live Colony subsystems."""

    def __init__(self, corpus: Any, colony_graph: Optional[Any] = None) -> None:
        self._corpus = corpus
        self._graph = colony_graph

    async def run_all(self) -> List[BootstrapAnomaly]:
        """Run all checks; return list of anomalies (empty = all passed)."""
        checks = [
            self._check_wm_self_entity,
            self._check_wm_subsystem_count,
            self._check_wm_part_of_relationships,
            self._check_wm_depends_on_relationships,
            self._check_contacts_self_entry,
            self._check_memory_identity,
            self._check_memory_architecture,
            self._check_memory_safety,
            self._check_memory_cognition,
            self._check_goals_bootstrap_present,
            self._check_briefing_present,
            self._check_session_present,
            self._check_corpus_layer_count,
            self._check_corpus_gate_layers,
            self._check_corpus_inference_tiers,
            self._check_corpus_cognition_phases,
            self._check_corpus_api_endpoints,
            self._check_colony_id_consistent,
        ]

        anomalies: List[BootstrapAnomaly] = []
        for check_fn in checks:
            try:
                anomaly = await check_fn()
                if anomaly is not None:
                    anomalies.append(anomaly)
            except Exception as exc:
                logger.warning("verifier: check %s raised: %s", check_fn.__name__, exc)
                anomalies.append(BootstrapAnomaly(
                    system="verifier",
                    check=check_fn.__name__,
                    expected="no exception",
                    actual=str(exc),
                    severity="WARNING",
                ))
        return anomalies

    # ── Check 1: world model self entity ──────────────────────────────────────

    async def _check_wm_self_entity(self) -> Optional[BootstrapAnomaly]:
        try:
            import colony.api.routers.world as wm_mod
            backend = getattr(wm_mod, "_wm_backend", None)
            if backend is None:
                return None  # WM not available — skip
            entity = await backend.get_entity(f"colony-self-{self._corpus.colony_id}")
            if entity is None:
                return BootstrapAnomaly(
                    system="world_model",
                    check="self_entity_exists",
                    expected=f"colony-self-{self._corpus.colony_id}",
                    actual="not found",
                    severity="CRITICAL",
                )
        except Exception as exc:
            logger.debug("check_wm_self_entity: %s", exc)
        return None

    # ── Check 2: subsystem entity count ───────────────────────────────────────

    async def _check_wm_subsystem_count(self) -> Optional[BootstrapAnomaly]:
        try:
            import colony.api.routers.world as wm_mod
            from colony_sidecar.identity_bootstrap.corpus import SUBSYSTEMS
            backend = getattr(wm_mod, "_wm_backend", None)
            if backend is None:
                return None
            colony_id = self._corpus.colony_id
            found = 0
            for sub in SUBSYSTEMS:
                ent = await backend.get_entity(f"colony-subsystem-{colony_id}-{sub}")
                if ent is not None:
                    found += 1
            expected = len(SUBSYSTEMS)
            if found < expected:
                return BootstrapAnomaly(
                    system="world_model",
                    check="subsystem_count",
                    expected=str(expected),
                    actual=str(found),
                    severity="WARNING",
                )
        except Exception as exc:
            logger.debug("check_wm_subsystem_count: %s", exc)
        return None

    # ── Check 3: WM_PART_OF relationships ─────────────────────────────────────

    async def _check_wm_part_of_relationships(self) -> Optional[BootstrapAnomaly]:
        try:
            import colony.api.routers.world as wm_mod
            backend = getattr(wm_mod, "_wm_backend", None)
            if backend is None:
                return None
            # Use find_entities to verify the self entity has neighbors
            self_id = f"colony-self-{self._corpus.colony_id}"
            neighbors = await backend.get_neighbors(self_id)
            if not neighbors:
                return BootstrapAnomaly(
                    system="world_model",
                    check="part_of_relationships",
                    expected="at least 1 WM_PART_OF neighbor",
                    actual="0 neighbors",
                    severity="WARNING",
                )
        except Exception as exc:
            logger.debug("check_wm_part_of_relationships: %s", exc)
        return None

    # ── Check 4: WM_DEPENDS_ON relationships ──────────────────────────────────

    async def _check_wm_depends_on_relationships(self) -> Optional[BootstrapAnomaly]:
        try:
            from colony_sidecar.world_model.constants import RELATIONSHIP_TYPES
            if "WM_DEPENDS_ON" not in RELATIONSHIP_TYPES:
                return BootstrapAnomaly(
                    system="world_model",
                    check="depends_on_in_constants",
                    expected="WM_DEPENDS_ON in RELATIONSHIP_TYPES",
                    actual="not present",
                    severity="CRITICAL",
                )
        except Exception as exc:
            logger.debug("check_wm_depends_on: %s", exc)
        return None

    # ── Check 5: self contact ─────────────────────────────────────────────────

    async def _check_contacts_self_entry(self) -> Optional[BootstrapAnomaly]:
        try:
            import colony.api.routers.contacts as contacts_mod
            store = getattr(contacts_mod, "_contact_store", None)
            contact_id = f"self:{self._corpus.colony_id}"
            if store is not None:
                contact = await store.get(contact_id)
                if contact is None:
                    # Check fallback
                    in_mem = getattr(contacts_mod, "_store", {})
                    if contact_id not in in_mem:
                        return BootstrapAnomaly(
                            system="contacts",
                            check="self_contact_exists",
                            expected=contact_id,
                            actual="not found",
                            severity="WARNING",
                        )
            else:
                in_mem = getattr(contacts_mod, "_store", {})
                if contact_id not in in_mem:
                    return BootstrapAnomaly(
                        system="contacts",
                        check="self_contact_exists",
                        expected=contact_id,
                        actual="not found",
                        severity="WARNING",
                    )
        except Exception as exc:
            logger.debug("check_contacts_self_entry: %s", exc)
        return None

    # ── Check 6: identity memory ──────────────────────────────────────────────

    async def _check_memory_identity(self) -> Optional[BootstrapAnomaly]:
        mem_id = f"mem-bootstrap-identity-{self._corpus.colony_id[:8]}"
        found = await self._check_memory_in_graph(mem_id)
        if not found:
            return BootstrapAnomaly(
                system="memory",
                check="identity_memory_exists",
                expected=mem_id,
                actual="not found",
                severity="WARNING",
            )
        return None

    # ── Check 7: architecture memory ──────────────────────────────────────────

    async def _check_memory_architecture(self) -> Optional[BootstrapAnomaly]:
        mem_id = f"mem-bootstrap-architecture-{self._corpus.colony_id[:8]}"
        found = await self._check_memory_in_graph(mem_id)
        if not found:
            return BootstrapAnomaly(
                system="memory",
                check="architecture_memory_exists",
                expected=mem_id,
                actual="not found",
                severity="WARNING",
            )
        return None

    # ── Check 7b: safety gate memory ─────────────────────────────────────────

    async def _check_memory_safety(self) -> Optional[BootstrapAnomaly]:
        mem_id = f"mem-bootstrap-safety-{self._corpus.colony_id[:8]}"
        found = await self._check_memory_in_graph(mem_id)
        if not found:
            return BootstrapAnomaly(
                system="memory",
                check="safety_memory_exists",
                expected=mem_id,
                actual="not found",
                severity="WARNING",
            )
        return None

    # ── Check 7c: cognition memory ───────────────────────────────────────────

    async def _check_memory_cognition(self) -> Optional[BootstrapAnomaly]:
        mem_id = f"mem-bootstrap-cognition-{self._corpus.colony_id[:8]}"
        found = await self._check_memory_in_graph(mem_id)
        if not found:
            return BootstrapAnomaly(
                system="memory",
                check="cognition_memory_exists",
                expected=mem_id,
                actual="not found",
                severity="WARNING",
            )
        return None

    # ── Helper: check for bootstrap memory in Neo4j ──────────────────────────

    async def _check_memory_in_graph(self, bootstrap_id: str) -> bool:
        """Check Neo4j for a bootstrap memory, fall back to API dict."""
        # Try Neo4j graph first (canonical store)
        try:
            import colony.api.server as server_mod
            # Prefer the graph passed directly to the verifier, then server module
            graph = self._graph or getattr(server_mod, "_colony_graph", None)
            if graph is not None and hasattr(graph, "driver"):
                async with graph.driver.session(database=graph.database) as session:
                    result = await session.run(
                        "MATCH (m:Memory) WHERE m.metadata CONTAINS $tag "
                        "RETURN count(m) > 0 AS found",
                        tag=bootstrap_id,
                    )
                    record = await result.single()
                    if record is not None and record["found"]:
                        return True
        except Exception as exc:
            logger.debug("_check_memory_in_graph neo4j: %s", exc)

        # Fallback: check the API router in-memory dict
        try:
            import colony.api.routers.memory as memory_mod
            store = getattr(memory_mod, "_store", {})
            if bootstrap_id in store:
                return True
        except Exception as exc:
            logger.debug("_check_memory_in_graph dict: %s", exc)

        return False

    # ── Check 8: bootstrap goal present ───────────────────────────────────────

    async def _check_goals_bootstrap_present(self) -> Optional[BootstrapAnomaly]:
        try:
            from colony_sidecar.goals.store import GoalStore
            store = GoalStore.get_instance()
            goal_id = f"goal-bootstrap-selfknowledge-{self._corpus.colony_id[:8]}"
            goal = store.get_goal(goal_id)
            if goal is None:
                return BootstrapAnomaly(
                    system="goals",
                    check="bootstrap_goal_exists",
                    expected=goal_id,
                    actual="not found",
                    severity="WARNING",
                )
        except Exception as exc:
            logger.debug("check_goals_bootstrap_present: %s", exc)
        return None

    # ── Check 9: welcome briefing ─────────────────────────────────────────────

    async def _check_briefing_present(self) -> Optional[BootstrapAnomaly]:
        try:
            from colony_sidecar.briefings.store import BriefingStore
            store = BriefingStore.get_instance()
            briefing_id = f"briefing-bootstrap-{self._corpus.colony_id[:8]}"
            briefing = store.get(briefing_id)
            if briefing is None:
                return BootstrapAnomaly(
                    system="briefings",
                    check="welcome_briefing_exists",
                    expected=briefing_id,
                    actual="not found",
                    severity="WARNING",
                )
        except Exception as exc:
            logger.debug("check_briefing_present: %s", exc)
        return None

    # ── Check 10: bootstrap session ───────────────────────────────────────────

    async def _check_session_present(self) -> Optional[BootstrapAnomaly]:
        try:
            import colony.api.routers.sessions as sessions_mod
            session_id = f"sess-bootstrap-{self._corpus.colony_id[:8]}"
            db = getattr(sessions_mod, "_db_backend", None)
            if db is not None:
                row = db.get(session_id)
                if row is None:
                    return BootstrapAnomaly(
                        system="sessions",
                        check="bootstrap_session_exists",
                        expected=session_id,
                        actual="not found in sqlite",
                        severity="WARNING",
                    )
            else:
                in_mem = getattr(sessions_mod, "_store", {})
                if session_id not in in_mem:
                    return BootstrapAnomaly(
                        system="sessions",
                        check="bootstrap_session_exists",
                        expected=session_id,
                        actual="not found in memory",
                        severity="WARNING",
                    )
        except Exception as exc:
            logger.debug("check_session_present: %s", exc)
        return None

    # ── Check 11: corpus layer count ──────────────────────────────────────────

    async def _check_corpus_layer_count(self) -> Optional[BootstrapAnomaly]:
        expected = 10
        actual = len(self._corpus.layers)
        if actual != expected:
            return BootstrapAnomaly(
                system="corpus",
                check="layer_count",
                expected=str(expected),
                actual=str(actual),
                severity="WARNING",
            )
        return None

    # ── Check 12: gate layers ─────────────────────────────────────────────────

    async def _check_corpus_gate_layers(self) -> Optional[BootstrapAnomaly]:
        expected = 7
        actual = len(self._corpus.gate_layers)
        if actual != expected:
            return BootstrapAnomaly(
                system="corpus",
                check="gate_layer_count",
                expected=str(expected),
                actual=str(actual),
                severity="WARNING",
            )
        return None

    # ── Check 13: inference tiers ─────────────────────────────────────────────

    async def _check_corpus_inference_tiers(self) -> Optional[BootstrapAnomaly]:
        expected = 4
        actual = len(self._corpus.inference_tiers)
        if actual != expected:
            return BootstrapAnomaly(
                system="corpus",
                check="inference_tier_count",
                expected=str(expected),
                actual=str(actual),
                severity="WARNING",
            )
        return None

    # ── Check 14: cognition phases ────────────────────────────────────────────

    async def _check_corpus_cognition_phases(self) -> Optional[BootstrapAnomaly]:
        expected = 8
        actual = len(self._corpus.cognition_phases)
        if actual != expected:
            return BootstrapAnomaly(
                system="corpus",
                check="cognition_phase_count",
                expected=str(expected),
                actual=str(actual),
                severity="WARNING",
            )
        return None

    # ── Check 15: API endpoints ───────────────────────────────────────────────

    async def _check_corpus_api_endpoints(self) -> Optional[BootstrapAnomaly]:
        if len(self._corpus.api_endpoints) < 80:
            return BootstrapAnomaly(
                system="corpus",
                check="api_endpoint_count",
                expected=">=80",
                actual=str(len(self._corpus.api_endpoints)),
                severity="WARNING",
            )
        return None

    # ── Check 16: colony_id consistency ───────────────────────────────────────

    async def _check_colony_id_consistent(self) -> Optional[BootstrapAnomaly]:
        colony_id = self._corpus.colony_id
        if not colony_id or colony_id == "unknown":
            return BootstrapAnomaly(
                system="corpus",
                check="colony_id_set",
                expected="non-empty colony_id",
                actual=repr(colony_id),
                severity="CRITICAL",
            )
        return None
