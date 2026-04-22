"""Memory seeder — writes foundational memories to Neo4j graph + LanceDB.

Uses ColonyGraph.store_memory() which persists to Neo4j and indexes in
LanceDB when configured.  Falls back to the API router in-memory dict
only when the graph is unavailable (e.g. Neo4j offline).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemorySeeder:
    name = "memory"

    def __init__(self, colony_graph: Optional[Any] = None) -> None:
        self._graph = colony_graph

    async def seed(self, corpus: Any) -> None:
        colony_id = corpus.colony_id
        now = _now_iso()

        memories = [
            {
                "id": f"mem-bootstrap-identity-{colony_id[:8]}",
                "type": "episodic",
                "content": (
                    f"I am {corpus.colony_name}, a Colony AI agent instance with ID {colony_id}. "
                    f"I run Colony v{corpus.colony_version} on network {corpus.network_id}. "
                    "I was bootstrapped with full self-knowledge at first boot."
                ),
                "tags": ["self", "identity", "bootstrap", "core"],
                "source": "identity_bootstrap",
                "entities": [corpus.colony_name, "Colony"],
                "metadata": {
                    "bootstrap_id": f"mem-bootstrap-identity-{colony_id[:8]}",
                    "colony_id": colony_id,
                    "colony_name": corpus.colony_name,
                    "colony_version": corpus.colony_version,
                    "network_id": corpus.network_id,
                    "corpus_version": corpus.corpus_version,
                    "tags": ["self", "identity", "bootstrap", "core"],
                    "source": "identity_bootstrap",
                    "created_at": now,
                },
                "importance": 0.95,
            },
            {
                "id": f"mem-bootstrap-architecture-{colony_id[:8]}",
                "type": "semantic",
                "content": (
                    "Colony has 10 architectural layers: Gateway, API, Intelligence, Memory, "
                    "Goals, Skills, TaskQueue, Federation, Safety, and Inference. "
                    "Each layer is independently optional and fails gracefully."
                ),
                "tags": ["architecture", "bootstrap", "core"],
                "source": "identity_bootstrap",
                "entities": ["Colony"] + [l.name for l in corpus.layers],
                "metadata": {
                    "bootstrap_id": f"mem-bootstrap-architecture-{colony_id[:8]}",
                    "layer_count": len(corpus.layers),
                    "layers": [l.name for l in corpus.layers],
                    "tags": ["architecture", "bootstrap", "core"],
                    "source": "identity_bootstrap",
                    "created_at": now,
                },
                "importance": 0.85,
            },
            {
                "id": f"mem-bootstrap-safety-{colony_id[:8]}",
                "type": "semantic",
                "content": (
                    "The ResponseGate pipeline has 7 layers: L1 RecipientAllowlist, L2 PIIScrubber, "
                    "L3 CrossContextGuard, L4 TrustTierGate, L5 InjectionDetector, "
                    "L6 HumanReview, L7 SendDelay. All outbound content passes through this pipeline."
                ),
                "tags": ["safety", "gate", "bootstrap"],
                "source": "identity_bootstrap",
                "entities": ["ResponseGate", "Colony"],
                "metadata": {
                    "bootstrap_id": f"mem-bootstrap-safety-{colony_id[:8]}",
                    "gate_layer_count": len(corpus.gate_layers),
                    "tags": ["safety", "gate", "bootstrap"],
                    "source": "identity_bootstrap",
                    "created_at": now,
                },
                "importance": 0.90,
            },
            {
                "id": f"mem-bootstrap-cognition-{colony_id[:8]}",
                "type": "semantic",
                "content": (
                    "Colony's cognition pipeline runs 8 phases: MetaLearning, StrategyAdjustment, "
                    "SelfReflection, SessionContinuity, ToolLearning, PreferenceLearning, "
                    "TaskPlanning, and ResearchOrchestration."
                ),
                "tags": ["cognition", "intelligence", "bootstrap"],
                "source": "identity_bootstrap",
                "entities": ["Colony", "CognitionPipeline"],
                "metadata": {
                    "bootstrap_id": f"mem-bootstrap-cognition-{colony_id[:8]}",
                    "phase_count": len(corpus.cognition_phases),
                    "phases": [p.name for p in corpus.cognition_phases],
                    "tags": ["cognition", "intelligence", "bootstrap"],
                    "source": "identity_bootstrap",
                    "created_at": now,
                },
                "importance": 0.80,
            },
        ]

        stored_count = 0

        # Primary path: write to Neo4j graph (+ LanceDB via store_memory)
        if self._graph is not None:
            for mem in memories:
                try:
                    # Check if this bootstrap memory already exists in the graph
                    exists = await self._memory_exists_in_graph(mem["id"])
                    if exists:
                        logger.debug("memory: %s already in graph — skipping", mem["id"])
                        stored_count += 1
                        continue

                    memory_id = await self._graph.store_memory(
                        content=mem["content"],
                        memory_type=mem["type"],
                        entities=mem.get("entities", []),
                        metadata=mem["metadata"],
                        importance=mem["importance"],
                    )
                    logger.debug(
                        "memory: stored %s as graph node %s", mem["id"], memory_id
                    )
                    stored_count += 1
                except Exception as exc:
                    logger.warning("memory: failed to store %s in graph: %s", mem["id"], exc)
        else:
            # Fallback: write to API router in-memory dict (non-persistent)
            logger.warning(
                "memory: no colony_graph available — falling back to ephemeral API dict"
            )
            try:
                import colony.api.routers.memory as memory_mod
                api_store = getattr(memory_mod, "_store", None)
                if api_store is not None:
                    for mem in memories:
                        mem_id = mem["id"]
                        if mem_id not in api_store:
                            api_store[mem_id] = {
                                "id": mem_id,
                                "content": mem["content"],
                                "type": mem["type"],
                                "tags": mem["tags"],
                                "source": mem["source"],
                                "metadata": mem["metadata"],
                                "strength": 1.0,
                                "importance": mem["importance"],
                                "decay_factor": 1.0,
                                "created_at": now,
                                "person_id": None,
                            }
                            stored_count += 1
            except ImportError:
                logger.debug("memory: API router not importable — skipping fallback")

        logger.info("memory: seeded %d foundational memories", stored_count)

    async def _memory_exists_in_graph(self, bootstrap_id: str) -> bool:
        """Check if a bootstrap memory already exists by searching for its source tag."""
        try:
            async with self._graph.driver.session(
                database=self._graph.database
            ) as session:
                result = await session.run(
                    "MATCH (m:Memory) WHERE m.metadata CONTAINS $tag RETURN count(m) > 0 AS exists",
                    tag=bootstrap_id,
                )
                record = await result.single()
                return record is not None and record["exists"]
        except Exception:
            return False
