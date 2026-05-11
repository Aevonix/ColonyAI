#!/usr/bin/env python3
"""Cleanup script: remove duplicate relationship initiatives and junk Person nodes."""

import asyncio
import os
import sqlite3
import unicodedata
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from neo4j import AsyncGraphDatabase


STATE_DIR = Path.home() / ".colony" / "data"
DB_PATH = STATE_DIR / "initiatives.db"


def cleanup_initiatives():
    """Delete all pending/assigned/acknowledged initiatives so they regenerate fresh."""
    if not DB_PATH.exists():
        print("No initiatives database found.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "DELETE FROM initiatives WHERE status IN ('pending', 'assigned', 'acknowledged')"
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    print(f"Deleted {deleted} old initiatives (will be regenerated with fixes).")


def normalize_name(name: str) -> str:
    if not name:
        return ""
    name = name.strip().lower()
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"\s+", " ", name).strip()
    return name


async def cleanup_graph():
    """Delete junk Person nodes and merge obvious duplicates."""
    uri = os.environ.get("COLONY_NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("COLONY_NEO4J_USER", "neo4j")
    password = os.environ.get("COLONY_NEO4J_PASSWORD", "password")
    driver = AsyncGraphDatabase.driver(uri, auth=(user, password))

    async with driver.session() as session:
        # 1. Delete junk nodes
        result = await session.run("""
            MATCH (p:Person)
            WHERE p.name IS NULL 
               OR p.name = ''
               OR p.name =~ '^\\+?\\d.*'
               OR p.name IN ['c', 'default', 'unknown', 'none', 'C', 'Default', 'Unknown', 'None']
               OR p.id IN ['c', 'default', '+151****1505']
            DETACH DELETE p
            RETURN count(p) as deleted
        """)
        record = await result.single()
        junk_deleted = record["deleted"] if record else 0
        print(f"Deleted {junk_deleted} junk Person nodes.")

        # 2. Find duplicates by normalized name
        result = await session.run("""
            MATCH (p:Person)
            RETURN p.id as id, p.name as name
            ORDER BY p.name
        """)
        records = []
        async for rec in result:
            records.append((rec["id"], rec["name"]))

    by_norm = defaultdict(list)
    for pid, name in records:
        by_norm[normalize_name(name)].append(pid)

    merged = 0
    for norm, ids in by_norm.items():
        if len(ids) <= 1:
            continue
        if not norm or len(norm) < 3:
            continue

        keep_id = ids[0]
        for dup_id in ids[1:]:
            try:
                async with driver.session() as session:
                    # Try to copy properties and move relationships with APOC
                    await session.run("""
                        MATCH (keep:Person {id: $keep_id}), (dup:Person {id: $dup_id})
                        SET keep.lastCommunication = coalesce(keep.lastCommunication, dup.lastCommunication),
                            keep.last_interaction = coalesce(keep.last_interaction, dup.last_interaction),
                            keep.lastSeen = coalesce(keep.lastSeen, dup.lastSeen),
                            keep.firstSeen = coalesce(keep.firstSeen, dup.firstSeen)
                        WITH keep, dup
                        OPTIONAL MATCH (dup)-[r]->(other)
                        WITH keep, dup, collect({type: type(r), other: other, props: properties(r)}) as outs
                        UNWIND outs as out
                        WITH keep, dup, out
                        WHERE out.other IS NOT NULL
                        CALL apoc.create.relationship(keep, out.type, out.props, out.other) YIELD rel as r1
                        WITH keep, dup
                        OPTIONAL MATCH (other)-[r]->(dup)
                        WITH keep, dup, collect({type: type(r), other: other, props: properties(r)}) as ins
                        UNWIND ins as in
                        WITH keep, dup, in
                        WHERE in.other IS NOT NULL
                        CALL apoc.create.relationship(in.other, in.type, in.props, keep) YIELD rel as r2
                        WITH keep, dup
                        DETACH DELETE dup
                    """, keep_id=keep_id, dup_id=dup_id)
                merged += 1
                print(f"  Merged {dup_id} into {keep_id} ({norm})")
            except Exception as e:
                # APOC not available or failed — just delete the duplicate
                # (relationships will be lost but data integrity is more important)
                try:
                    async with driver.session() as session:
                        await session.run("""
                            MATCH (dup:Person {id: $dup_id})
                            DETACH DELETE dup
                        """, dup_id=dup_id)
                    merged += 1
                    print(f"  Deleted duplicate {dup_id} ({norm}) — APOC not available")
                except Exception as e2:
                    print(f"  Failed to remove {dup_id}: {e2}")

    print(f"\nMerged/deleted {merged} duplicate Person nodes.")
    await driver.close()


async def main():
    print("=== Step 1: Clear old initiatives ===")
    cleanup_initiatives()

    print("\n=== Step 2: Clean up graph duplicates ===")
    await cleanup_graph()

    print("\nDone. Restart the sidecar to regenerate initiatives correctly.")


if __name__ == "__main__":
    asyncio.run(main())
