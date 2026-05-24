#!/usr/bin/env python3
"""Integration validation for v0.15.0 Memory Governance & Epistemic Hygiene.

Connects to the live Neo4j instance and exercises every new governance
function.  Cleans up all test data on exit.
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List

sys.path.insert(0, "/Users/kurcide/colony-work/sidecar")

from colony_sidecar.intelligence.graph.client import (
    ColonyGraph,
    GraphConfig,
    EpistemicState,
    MemorySourceType,
)
from pydantic import SecretStr


TEST_PREFIX = "__test_gov_"
FAILURES: List[str] = []


def log(msg: str) -> None:
    print(f"  {msg}")


def fail(step: str, exc: Exception) -> None:
    FAILURES.append(step)
    print(f"  ❌ {step}: {exc}")
    traceback.print_exception(type(exc), exc, exc.__traceback__)


async def cleanup(graph: ColonyGraph) -> None:
    """Remove all test nodes created by this script."""
    async with graph.driver.session(database=graph.database) as session:
        await session.run(
            """
            MATCH (m:Memory)
            WHERE m.content STARTS WITH $prefix
            DETACH DELETE m
            """,
            prefix=TEST_PREFIX,
        )
        await session.run(
            """
            MATCH (a:ArchivedMemory)
            WHERE a.content STARTS WITH $prefix
            DETACH DELETE a
            """,
            prefix=TEST_PREFIX,
        )
        await session.run(
            """
            MATCH (fa:FileAnchor)
            WHERE fa.uri STARTS WITH $prefix
            DETACH DELETE fa
            """,
            prefix=TEST_PREFIX,
        )
        await session.run(
            """
            MATCH (e:Entity)
            WHERE e.name STARTS WITH $prefix
            DETACH DELETE e
            """,
            prefix=TEST_PREFIX,
        )


async def step_connect() -> ColonyGraph:
    log("Connecting to Neo4j...")
    config = GraphConfig(
        uri="bolt://localhost:7687",
        database="neo4j",
        auth=("neo4j", SecretStr("***")),
    )
    graph = ColonyGraph(config)
    await graph.connect()
    log("  Connected ✓")
    return graph


async def step_store_memory_basic(graph: ColonyGraph) -> str:
    log("store_memory (basic inference)...")
    mid = await graph.store_memory(
        content=f"{TEST_PREFIX}basic_inference",
        memory_type="semantic",
        entities=[f"{TEST_PREFIX}EntityA"],
        importance=0.5,
        source_type=MemorySourceType.INFERENCE.value,
    )
    mem = await graph.get_memory(mid)
    assert mem is not None, "Memory not found after store"
    assert mem["epistemic_state"] == "inferred"
    assert mem["source_type"] == "inference"
    assert mem["protected"] is False
    assert float(mem["effective_confidence"]) > 0
    log(f"  Created {mid} ✓")
    return mid


async def step_store_memory_file_anchor(graph: ColonyGraph) -> str:
    log("store_memory (file source + FileAnchor)...")
    mid = await graph.store_memory(
        content=f"{TEST_PREFIX}file_derived",
        memory_type="semantic",
        entities=[f"{TEST_PREFIX}FileEntity"],
        importance=0.9,
        source_type=MemorySourceType.FILE.value,
        source_uri=f"{TEST_PREFIX}file:///test/doc.md",
        source_version="v1.2.3",
        content_hash="abc123",
    )
    mem = await graph.get_memory(mid)
    assert mem is not None
    assert mem["source_type"] == "file"
    assert mem["source_uri"] == f"{TEST_PREFIX}file:///test/doc.md"
    assert mem["content_hash"] == "abc123"

    # Verify FileAnchor exists and is linked
    async with graph.driver.session(database=graph.database) as session:
        result = await session.run(
            """
            MATCH (m:Memory {id: $mid})-[:DERIVED_FROM]->(fa:FileAnchor)
            RETURN fa.uri AS uri
            """,
            mid=mid,
        )
        record = await result.single()
        assert record is not None, "FileAnchor not linked"
        assert record["uri"] == f"{TEST_PREFIX}file:///test/doc.md"
    log(f"  Created {mid} with FileAnchor ✓")
    return mid


async def step_store_memory_user_assertion(graph: ColonyGraph) -> str:
    log("store_memory (user_assertion + importance clamping)...")
    mid = await graph.store_memory(
        content=f"{TEST_PREFIX}user_assertion",
        memory_type="identity",
        entities=[f"{TEST_PREFIX}UserEntity"],
        importance=1.5,  # exceeds max 1.0
        source_type=MemorySourceType.USER_ASSERTION.value,
    )
    mem = await graph.get_memory(mid)
    assert mem is not None
    assert mem["source_type"] == "user_assertion"
    assert mem["protected"] is True
    assert float(mem["importance"]) == 1.0, f"Expected 1.0, got {mem['importance']}"
    log(f"  Created {mid}, clamped to 1.0 ✓")
    return mid


async def step_compute_effective_confidence(graph: ColonyGraph) -> None:
    log("compute_effective_confidence...")
    now = datetime.now(timezone.utc)
    created = now

    # User assertion should be high
    ec = graph.compute_effective_confidence(
        base_confidence=1.0,
        source_reliability=1.0,
        corroboration_count=0,
        contradiction_count=0,
        recalls=0,
        last_verified_at=None,
        created_at=created,
        epistemic_state="inferred",
        now=now,
    )
    assert 0.9 <= ec <= 1.0, f"Expected ~1.0, got {ec}"

    # Inference should be lower
    ec2 = graph.compute_effective_confidence(
        base_confidence=1.0,
        source_reliability=0.5,
        corroboration_count=0,
        contradiction_count=0,
        recalls=0,
        last_verified_at=None,
        created_at=created,
        epistemic_state="inferred",
        now=now,
    )
    assert ec2 < ec, f"Inference ({ec2}) should be < user_assertion ({ec})"

    # Contradiction penalty
    ec3 = graph.compute_effective_confidence(
        base_confidence=1.0,
        source_reliability=1.0,
        corroboration_count=0,
        contradiction_count=5,
        recalls=0,
        last_verified_at=None,
        created_at=created,
        epistemic_state="inferred",
        now=now,
    )
    assert ec3 < ec, f"Contradicted ({ec3}) should be < clean ({ec})"

    # Verified floor
    ec4 = graph.compute_effective_confidence(
        base_confidence=0.1,
        source_reliability=0.5,
        corroboration_count=0,
        contradiction_count=0,
        recalls=0,
        last_verified_at=None,
        created_at=created,
        epistemic_state="verified",
        now=now,
    )
    assert ec4 >= 0.9, f"Verified floor failed: {ec4}"

    # Stale penalty
    ec5 = graph.compute_effective_confidence(
        base_confidence=1.0,
        source_reliability=1.0,
        corroboration_count=0,
        contradiction_count=0,
        recalls=0,
        last_verified_at=None,
        created_at=created,
        epistemic_state="stale",
        now=now,
    )
    assert ec5 <= 0.35, f"Stale penalty failed: {ec5}"

    log("  All confidence signals ✓")


async def step_touch_memory(graph: ColonyGraph, mid: str) -> None:
    log("touch_memory...")
    mem_before = await graph.get_memory(mid)
    recalls_before = int(mem_before.get("recalls", 0))
    await graph.touch_memory(mid)
    mem_after = await graph.get_memory(mid)
    recalls_after = int(mem_after.get("recalls", 0))
    assert recalls_after == recalls_before + 1, f"Expected {recalls_before+1}, got {recalls_after}"
    log(f"  Recalls {recalls_before} → {recalls_after} ✓")


async def step_decay_memories(graph: ColonyGraph, mid_weak: str) -> None:
    log("decay_memories...")
    # Ensure the weak memory has old accessed_at by manipulating directly
    async with graph.driver.session(database=graph.database) as session:
        await session.run(
            """
            MATCH (m:Memory {id: $mid})
            SET m.accessed_at = datetime() - duration({days: 30}),
                m.importance = 0.1,
                m.recalls = 0
            """,
            mid=mid_weak,
        )

    mem_before = await graph.get_memory(mid_weak)
    strength_before = float(mem_before["strength"])

    await graph.decay_memories(half_life_days=7.0)

    mem_after = await graph.get_memory(mid_weak)
    strength_after = float(mem_after["strength"])
    assert strength_after < strength_before, f"Expected decay {strength_before} → lower, got {strength_after}"
    log(f"  Strength {strength_before:.4f} → {strength_after:.4f} ✓")


async def step_verify_memory(graph: ColonyGraph, mid: str) -> None:
    log("verify_memory...")
    # Set low confidence first
    async with graph.driver.session(database=graph.database) as session:
        await session.run(
            """
            MATCH (m:Memory {id: $mid})
            SET m.effective_confidence = 0.2,
                m.epistemic_state = "observed"
            """,
            mid=mid,
        )
    await graph.verify_memory(mid)
    mem = await graph.get_memory(mid)
    assert mem["epistemic_state"] == "verified", f"Expected verified, got {mem['epistemic_state']}"
    assert float(mem["effective_confidence"]) >= 0.9, f"Expected >=0.9, got {mem['effective_confidence']}"
    assert mem["last_verified_at"] is not None
    log(f"  State observed → verified, confidence floored ✓")


async def step_transition_epistemic_state(graph: ColonyGraph, mid: str) -> None:
    log("transition_epistemic_state...")
    await graph.transition_epistemic_state(mid, "stale")
    mem = await graph.get_memory(mid)
    assert mem["epistemic_state"] == "stale"
    log(f"  State → stale ✓")


async def step_prune_weak_memories(graph: ColonyGraph) -> str:
    log("prune_weak_memories...")
    # Create a very weak inferred memory
    mid_weak = await graph.store_memory(
        content=f"{TEST_PREFIX}prune_me",
        memory_type="episodic",
        entities=[f"{TEST_PREFIX}PruneEntity"],
        importance=0.01,
        source_type=MemorySourceType.INFERENCE.value,
    )
    # Force strength below threshold
    async with graph.driver.session(database=graph.database) as session:
        await session.run(
            """
            MATCH (m:Memory {id: $mid})
            SET m.strength = 0.01
            """,
            mid=mid_weak,
        )

    pruned = await graph.prune_weak_memories(threshold=0.05)
    assert pruned >= 1, f"Expected >=1 pruned, got {pruned}"

    mem = await graph.get_memory(mid_weak)
    assert mem is None, "Pruned memory still exists"
    log(f"  Pruned {pruned} weak memory ✓")
    return mid_weak


async def step_archive_memories(graph: ColonyGraph) -> str:
    log("archive_memories...")
    # Create a stale old memory
    mid_old = await graph.store_memory(
        content=f"{TEST_PREFIX}archive_me",
        memory_type="semantic",
        entities=[f"{TEST_PREFIX}ArchiveEntity"],
        importance=0.3,
        source_type=MemorySourceType.INFERENCE.value,
    )
    async with graph.driver.session(database=graph.database) as session:
        await session.run(
            """
            MATCH (m:Memory {id: $mid})
            SET m.epistemic_state = "stale",
                m.accessed_at = datetime() - duration({days: 60})
            """,
            mid=mid_old,
        )

    archived = await graph.archive_memories(max_age_days=30)
    assert archived >= 1, f"Expected >=1 archived, got {archived}"

    mem = await graph.get_memory(mid_old)
    assert mem is None, "Original memory still exists after archive"

    # Verify ArchivedMemory exists
    async with graph.driver.session(database=graph.database) as session:
        result = await session.run(
            """
            MATCH (a:ArchivedMemory {id: $mid})
            RETURN a.content AS content
            """,
            mid=mid_old,
        )
        record = await result.single()
        assert record is not None, "ArchivedMemory not found"
        assert record["content"] == f"{TEST_PREFIX}archive_me"
    log(f"  Archived {archived} memory ✓")
    return mid_old


async def step_recall_filters_terminal(graph: ColonyGraph) -> None:
    log("recall filters terminal states...")
    # Create a deprecated memory
    mid_dep = await graph.store_memory(
        content=f"{TEST_PREFIX}deprecated_memory",
        memory_type="semantic",
        entities=[f"{TEST_PREFIX}DepEntity"],
        importance=0.8,
        source_type=MemorySourceType.INFERENCE.value,
    )
    await graph.transition_epistemic_state(mid_dep, "deprecated")

    # Fallback keyword recall should filter it out
    results = await graph.recall(
        query=f"{TEST_PREFIX}deprecated_memory",
        limit=10,
        min_strength=0.0,
        min_confidence=0.0,
    )
    ids = [r["id"] for r in results]
    assert mid_dep not in ids, "Deprecated memory should be filtered from recall"
    log(f"  Terminal state filtered from recall ✓")


async def main() -> int:
    print("=" * 60)
    print("v0.15.0 Memory Governance — Live Neo4j Integration Validation")
    print("=" * 60)

    graph = None
    try:
        graph = await step_connect()
        await cleanup(graph)

        # 1. Store memories
        mid_basic = await step_store_memory_basic(graph)
        mid_file = await step_store_memory_file_anchor(graph)
        mid_user = await step_store_memory_user_assertion(graph)

        # 2. Confidence computation
        await step_compute_effective_confidence(graph)

        # 3. Touch
        await step_touch_memory(graph, mid_basic)

        # 4. Decay
        await step_decay_memories(graph, mid_basic)

        # 5. Verify
        await step_verify_memory(graph, mid_user)

        # 6. Transition
        await step_transition_epistemic_state(graph, mid_file)

        # 7. Prune
        await step_prune_weak_memories(graph)

        # 8. Archive
        await step_archive_memories(graph)

        # 9. Recall filters
        await step_recall_filters_terminal(graph)

        print("\n" + "=" * 60)
        if FAILURES:
            print(f"RESULT: {len(FAILURES)} FAILURE(S)")
            for f in FAILURES:
                print(f"  - {f}")
            return 1
        else:
            print("RESULT: ALL TESTS PASSED")
            return 0

    except Exception as exc:
        print(f"\nFATAL: {exc}")
        traceback.print_exc()
        return 1
    finally:
        if graph is not None:
            print("\nCleaning up test data...")
            try:
                await cleanup(graph)
                print("  Cleanup complete ✓")
            except Exception as exc:
                print(f"  Cleanup warning: {exc}")
            await graph.driver.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
