#!/usr/bin/env python3
"""Test the initiative engine fix directly."""

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from neo4j import AsyncGraphDatabase
from colony_sidecar.intelligence.components.initiative_engine import InitiativeEngine, InitiativeConfig


class SimpleGraph:
    """Minimal graph wrapper matching what InitiativeEngine expects."""
    def __init__(self, driver, database="neo4j"):
        self.driver = driver
        self.database = database


async def main():
    uri = os.environ.get("COLONY_NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("COLONY_NEO4J_USER", "neo4j")
    password = os.environ.get("COLONY_NEO4J_PASSWORD", "password")
    driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    graph = SimpleGraph(driver)

    engine = InitiativeEngine(
        graph_client=graph,
        event_bus=None,
        mind_model=None,
        store=None,
        config=InitiativeConfig(contact_neglect_days=14)
    )

    print("=== Testing _load_neglected_contacts ===")
    await engine._load_neglected_contacts()
    contacts = engine._context.get("neglected_contacts", [])
    print(f"Genuinely neglected contacts found: {len(contacts)}")
    for c in contacts:
        print(f"  {c['name']}: {c['days_since_contact']} days (id={c['entity_id']})")

    print(f"\n=== Testing _generate_relationship_suggestions ===")
    suggestions = await engine._generate_relationship_suggestions()
    print(f"Relationship initiatives after dedup: {len(suggestions)}")
    for s in suggestions:
        print(f"  {s.description} (priority={s.priority:.2f}, dedup={s.dedup_key})")

    await driver.close()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
