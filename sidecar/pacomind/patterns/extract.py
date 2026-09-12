"""Pattern extraction — pull patterns from the world model."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def extract_patterns(
    world_store: Any = None,
    pattern_store: Any = None,
) -> Dict[str, Any]:
    """Run pattern extraction against the world model.

    If world_store or pattern_store is None, returns a no-op result.
    Returns {"new": N, "updated": M, "total": T}.
    """
    if world_store is None or pattern_store is None:
        return {"new": 0, "updated": 0, "total": 0, "reason": "stores_not_wired"}

    results = {"new": 0, "updated": 0, "total": 0}

    # Extract entity cooccurrence patterns.
    try:
        cooc = _extract_entity_cooccurrence(world_store)
        for pattern in cooc:
            result = pattern_store.create_pattern(**pattern)
            if result.get("frequency", 1) > 1:
                results["updated"] += 1
            else:
                results["new"] += 1
    except Exception as exc:
        logger.debug("entity cooccurrence extraction failed: %s", exc)

    # Extract relation frequency patterns.
    try:
        rels = _extract_relation_frequency(world_store)
        for pattern in rels:
            result = pattern_store.create_pattern(**pattern)
            if result.get("frequency", 1) > 1:
                results["updated"] += 1
            else:
                results["new"] += 1
    except Exception as exc:
        logger.debug("relation frequency extraction failed: %s", exc)

    # Extract attribute cluster patterns.
    try:
        attrs = _extract_attribute_clusters(world_store)
        for pattern in attrs:
            result = pattern_store.create_pattern(**pattern)
            if result.get("frequency", 1) > 1:
                results["updated"] += 1
            else:
                results["new"] += 1
    except Exception as exc:
        logger.debug("attribute cluster extraction failed: %s", exc)

    total = pattern_store.list_patterns(active_only=True)
    results["total"] = total.get("total", 0)
    return results


def _extract_entity_cooccurrence(world_store: Any) -> List[Dict[str, Any]]:
    """Find entities that share relationships."""
    patterns: List[Dict[str, Any]] = []

    try:
        entities = world_store.list_entities(limit=200)
    except Exception as exc:
        logger.debug("list_entities failed in pattern extraction: %s", exc)
        return patterns

    if not entities:
        return patterns

    # Group entities by relationship target.
    target_map: Dict[str, List[str]] = {}
    for entity in entities:
        name = entity.get("name", "")
        etype = entity.get("entity_type", "")
        if not name:
            continue
        # Use entity type as a grouping key for cooccurrence.
        if etype not in target_map:
            target_map[etype] = []
        target_map[etype].append(name)

    # Entities that share a type are cooccurring.
    for etype, names in target_map.items():
        if len(names) < 2:
            continue
        # Create pairwise patterns for top entities.
        for i in range(min(len(names), 5)):
            for j in range(i + 1, min(len(names), 5)):
                key = f"cooc:{sorted([names[i], names[j]])[0]}→{sorted([names[i], names[j]])[1]}"
                patterns.append({
                    "pattern_type": "entity_cooccurrence",
                    "description": f"{names[i]} and {names[j]} both appear as {etype}",
                    "pattern_key": key,
                    "confidence": 0.6,
                    "source": "extraction",
                    "metadata": {"type": etype, "entities": [names[i], names[j]]},
                })

    return patterns


def _extract_relation_frequency(world_store: Any) -> List[Dict[str, Any]]:
    """Find frequently occurring relationship types."""
    patterns: List[Dict[str, Any]] = []

    try:
        entities = world_store.list_entities(limit=200)
    except Exception as exc:
        logger.debug("list_entities failed in pattern extraction: %s", exc)
        return patterns

    # Count relationship types.
    rel_counts: Dict[str, int] = {}
    for entity in entities:
        rels = entity.get("relationships", [])
        if isinstance(rels, list):
            for rel in rels:
                rtype = rel.get("type", "") if isinstance(rel, dict) else str(rel)
                if rtype:
                    rel_counts[rtype] = rel_counts.get(rtype, 0) + 1

    for rtype, count in rel_counts.items():
        if count < 2:
            continue
        patterns.append({
            "pattern_type": "relation_frequency",
            "description": f"Relationship type '{rtype}' appears {count} times",
            "pattern_key": f"rel_freq:{rtype}",
            "frequency": count,
            "confidence": min(1.0, 0.3 + count * 0.1),
            "source": "extraction",
            "metadata": {"relation_type": rtype, "count": count},
        })

    return patterns


def _extract_attribute_clusters(world_store: Any) -> List[Dict[str, Any]]:
    """Find entities of the same type that share attribute keys."""
    patterns: List[Dict[str, Any]] = []

    try:
        entities = world_store.list_entities(limit=200)
    except Exception as exc:
        logger.debug("list_entities failed in pattern extraction: %s", exc)
        return patterns

    # Group by type, collect attribute keys.
    type_attrs: Dict[str, Dict[str, int]] = {}
    for entity in entities:
        etype = entity.get("entity_type", "")
        attrs = entity.get("attributes", {})
        if not isinstance(attrs, dict):
            continue
        if etype not in type_attrs:
            type_attrs[etype] = {}
        for key in attrs:
            type_attrs[etype][key] = type_attrs[etype].get(key, 0) + 1

    for etype, attr_counts in type_attrs.items():
        if len(attr_counts) < 2:
            continue
        # Only include attributes that appear in multiple entities.
        common = [k for k, v in attr_counts.items() if v >= 2]
        if len(common) < 2:
            continue
        common.sort()
        key = f"attr_cluster:{etype}:{','.join(common[:5])}"
        patterns.append({
            "pattern_type": "attribute_cluster",
            "description": f"{etype} entities share attributes: {', '.join(common[:5])}",
            "pattern_key": key,
            "confidence": 0.7,
            "source": "extraction",
            "metadata": {"entity_type": etype, "attributes": common[:10]},
        })

    return patterns
