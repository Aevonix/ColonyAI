#!/usr/bin/env python3
"""Graph cleanup: remove obvious duplicate Person nodes using fuzzy matching."""

import asyncio
import os
import unicodedata
import re
from collections import defaultdict
from difflib import SequenceMatcher

from neo4j import AsyncGraphDatabase


def normalize(name: str) -> str:
    if not name:
        return ""
    name = name.strip().lower()
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"\s+", " ", name).strip()
    return name


def name_similarity(a: str, b: str) -> float:
    """Return similarity ratio between two names (0-1)."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    # Exact match
    if na == nb:
        return 1.0
    # One contains the other (e.g. "marc seal" in "marc andrew seal")
    if na in nb or nb in na:
        return 0.95
    # Token overlap (e.g. "vitor souza" vs "vitor lopes de souza")
    ta, tb = set(na.split()), set(nb.split())
    overlap = len(ta & tb)
    total = len(ta | tb)
    if total > 0 and overlap >= 2:
        return 0.90
    # Fuzzy similarity
    return SequenceMatcher(None, na, nb).ratio()


async def main():
    uri = os.environ.get("COLONY_NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("COLONY_NEO4J_USER", "neo4j")
    password = os.environ.get("COLONY_NEO4J_PASSWORD", "password")
    driver = AsyncGraphDatabase.driver(uri, auth=(user, password))

    async with driver.session() as session:
        result = await session.run("""
            MATCH (p:Person)
            RETURN p.id as id, p.name as name
            ORDER BY p.name
        """)
        records = []
        async for rec in result:
            records.append((rec["id"], rec["name"]))

    print(f"Total Person nodes: {len(records)}")

    # Find similar pairs
    similar_groups = []
    processed = set()
    for i, (id1, name1) in enumerate(records):
        if id1 in processed:
            continue
        group = [(id1, name1)]
        for j, (id2, name2) in enumerate(records):
            if i >= j or id2 in processed:
                continue
            sim = name_similarity(name1, name2)
            if sim >= 0.85:
                group.append((id2, name2))
                processed.add(id2)
        if len(group) > 1:
            similar_groups.append(group)
            processed.add(id1)

    print(f"\nFound {len(similar_groups)} duplicate groups:")
    for group in similar_groups:
        print(f"  {group[0][1]} ({group[0][0]})")
        for dup_id, dup_name in group[1:]:
            print(f"    → duplicate: {dup_name} ({dup_id})")

    # Delete duplicates (keep first in each group)
    deleted = 0
    for group in similar_groups:
        keep_id = group[0][0]
        for dup_id, dup_name in group[1:]:
            try:
                async with driver.session() as session:
                    await session.run("""
                        MATCH (dup:Person {id: $dup_id})
                        DETACH DELETE dup
                    """, dup_id=dup_id)
                deleted += 1
                print(f"  Deleted {dup_id}")
            except Exception as e:
                print(f"  Failed to delete {dup_id}: {e}")

    print(f"\nDeleted {deleted} duplicate Person nodes.")
    await driver.close()


if __name__ == "__main__":
    asyncio.run(main())
