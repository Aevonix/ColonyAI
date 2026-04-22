"""Neo4j cognition seeder — writes bootstrap metrics and a BootstrapEvent node to Neo4j."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class Neo4jCognitionSeeder:
    name = "neo4j_cognition"

    def __init__(
        self,
        colony_graph: Optional[Any] = None,
        metrics_collector: Optional[Any] = None,
    ) -> None:
        self._graph = colony_graph
        self._metrics = metrics_collector

    async def seed(self, corpus: Any) -> None:
        colony_id = corpus.colony_id
        now_iso = datetime.now(timezone.utc).isoformat()

        # Record bootstrap metrics
        if self._metrics is not None:
            try:
                await self._metrics.record(
                    metric_type="bootstrap_layer_count",
                    value=float(len(corpus.layers)),
                    domain="identity",
                    context={"colony_id": colony_id},
                )
                await self._metrics.record(
                    metric_type="bootstrap_endpoint_count",
                    value=float(len(corpus.api_endpoints)),
                    domain="identity",
                    context={"colony_id": colony_id},
                )
                await self._metrics.record(
                    metric_type="bootstrap_gate_layers",
                    value=float(len(corpus.gate_layers)),
                    domain="safety",
                    context={"colony_id": colony_id},
                )
                logger.info("neo4j_cognition: bootstrap metrics recorded")
            except Exception as exc:
                logger.warning("neo4j_cognition: metrics recording failed: %s", exc)

        # Write BootstrapEvent node to Neo4j
        if self._graph is not None:
            cypher = """
            MERGE (b:BootstrapEvent {colony_id: $colony_id})
            SET b.colony_name      = $colony_name,
                b.colony_version   = $colony_version,
                b.network_id       = $network_id,
                b.corpus_version   = $corpus_version,
                b.bootstrapped_at  = $bootstrapped_at,
                b.layer_count      = $layer_count,
                b.endpoint_count   = $endpoint_count
            RETURN b.colony_id
            """
            params = {
                "colony_id": colony_id,
                "colony_name": corpus.colony_name,
                "colony_version": corpus.colony_version,
                "network_id": corpus.network_id,
                "corpus_version": corpus.corpus_version,
                "bootstrapped_at": now_iso,
                "layer_count": len(corpus.layers),
                "endpoint_count": len(corpus.api_endpoints),
            }
            try:
                await self._graph.run_query(cypher, params)
                logger.info("neo4j_cognition: BootstrapEvent node upserted for colony_id=%s", colony_id)
            except Exception as exc:
                logger.warning("neo4j_cognition: run_query failed: %s", exc)
