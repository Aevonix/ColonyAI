"""Read-only causal-chain query — the sanctioned way to READ causal edges.

Causal edges are query-only by policy (world_model/causal_policy.py) and are
excluded from every generic graph read path: the ONLY surfaces that return
them are the /world/causal/* endpoints (built on this module) and
explicitly-typed relationship queries. This module answers "why" / "what
happens if" questions by walking causal edges alone; it never writes and
never feeds an action path.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from colony_sidecar.world_model.constants import CAUSAL_RELATIONSHIP_TYPES

logger = logging.getLogger(__name__)

_DIRECTIONS = ("downstream", "upstream", "both")


def _edge_dict(rel: Any) -> Dict[str, Any]:
    props = getattr(rel, "properties", None) or {}
    return {
        "id": rel.id,
        "source_id": rel.source_id,
        "target_id": rel.target_id,
        "relationship_type": rel.relationship_type,
        "confidence": rel.confidence,
        "is_active": bool(getattr(rel, "is_active", rel.valid_to is None)),
        "evidence": props.get("evidence"),
    }


async def causal_chain(
    store: Any,
    entity_id: str,
    direction: str = "downstream",
    max_hops: int = 3,
    min_confidence: float = 0.0,
    max_edges: int = 100,
) -> Dict[str, Any]:
    """BFS over causal edges only (explicitly typed queries, so the generic
    read-path exclusion does not apply here — by construction).

    direction: downstream = effects of entity_id; upstream = its causes;
    both = the full causal neighborhood.
    """
    if direction not in _DIRECTIONS:
        direction = "downstream"
    max_hops = max(1, min(int(max_hops), 5))

    edges: Dict[str, Dict[str, Any]] = {}
    visited: Dict[str, int] = {entity_id: 0}
    frontier = [entity_id]
    truncated = False

    for hop in range(max_hops):
        next_frontier: List[str] = []
        for eid in frontier:
            for rel_type in sorted(CAUSAL_RELATIONSHIP_TYPES):
                queries = []
                if direction in ("downstream", "both"):
                    queries.append({"source_id": eid})
                if direction in ("upstream", "both"):
                    queries.append({"target_id": eid})
                for q in queries:
                    rels = await store.query_relationships(
                        relationship_type=rel_type,
                        min_confidence=min_confidence, limit=50, **q)
                    for rel in rels:
                        if rel.id in edges:
                            continue
                        if len(edges) >= max_edges:
                            truncated = True
                            break
                        edges[rel.id] = _edge_dict(rel)
                        other = (rel.target_id if rel.source_id == eid
                                 else rel.source_id)
                        if other not in visited:
                            visited[other] = hop + 1
                            next_frontier.append(other)
                    if truncated:
                        break
                if truncated:
                    break
            if truncated:
                break
        if truncated or not next_frontier:
            break
        frontier = next_frontier

    nodes: Dict[str, Dict[str, Any]] = {}
    for eid in visited:
        try:
            ent = await store.get_entity(eid, min_confidence=0.0)
        except Exception:
            ent = None
        if ent is not None:
            nodes[eid] = {"name": ent.name, "entity_type": ent.entity_type,
                          "hops": visited[eid]}

    return {
        "entity_id": entity_id,
        "direction": direction,
        "max_hops": max_hops,
        "edges": list(edges.values()),
        "nodes": nodes,
        "truncated": truncated,
    }


async def causal_edges(store: Any, min_confidence: float = 0.0,
                       limit: int = 100) -> List[Dict[str, Any]]:
    """Flat list of causal edges (explicitly typed queries per causal type)."""
    out: List[Dict[str, Any]] = []
    for rel_type in sorted(CAUSAL_RELATIONSHIP_TYPES):
        rels = await store.query_relationships(
            relationship_type=rel_type, min_confidence=min_confidence,
            limit=limit)
        out.extend(_edge_dict(r) for r in rels)
        if len(out) >= limit:
            break
    return out[:limit]
