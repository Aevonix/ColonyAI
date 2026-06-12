"""The autonomy loop's relationship phase must periodically recompute the
SQLite closeness score for EVERY contact — not just the one in the active turn.

Regression ("scoring starved"): the scheduled phase only ran the behavioral
graph scorer, which writes Neo4j Person.score and never syncs back to the SQLite
`relationship_score` that every consumer reads. So a contact with no recent turn
kept a frozen, stale score (recency decay never applied). This verifies the
periodic SQLite refresh runs for contacts independent of any turn.
"""
from types import SimpleNamespace

import pytest

from colony_sidecar.autonomy.loop import AutonomyLoop
from colony_sidecar.contacts.config import ContactsConfig
from colony_sidecar.contacts.store import SQLiteContactStore
from colony_sidecar.contacts.scoring import compute_relationship_score


@pytest.mark.asyncio
async def test_phase_relationships_refreshes_all_contact_scores(tmp_path):
    store = SQLiteContactStore(config=ContactsConfig(sqlite_path=str(tmp_path / "c.db")))
    await store.connect()
    try:
        contact = await store.create(display_name="Neglected Pal", trust_tier="inner_circle")
        # Plant a stale, inflated score on disk (as if frozen from an old turn).
        await store.update_relationship_score(contact.contact_id, 0.95)

        loaded = await store.get(contact.contact_id)
        expected = compute_relationship_score(loaded, None)
        # Sanity: the correct computed score differs from the frozen one.
        assert abs(expected - 0.95) > 1e-3

        # No graph, no active turn — only the periodic SQLite refresh should run.
        registry = SimpleNamespace(graph=None, contacts=store, affect_store=None)
        loop = AutonomyLoop(registry=registry)
        await loop._phase_relationships()

        refreshed = await store.get(contact.contact_id)
        assert abs(refreshed.relationship_score - expected) < 1e-3
        assert loop.stats.scoring_runs == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_phase_relationships_noop_when_scores_already_current(tmp_path):
    store = SQLiteContactStore(config=ContactsConfig(sqlite_path=str(tmp_path / "c.db")))
    await store.connect()
    try:
        contact = await store.create(display_name="Up To Date", trust_tier="regular")
        loaded = await store.get(contact.contact_id)
        await store.update_relationship_score(
            contact.contact_id, compute_relationship_score(loaded, None))

        registry = SimpleNamespace(graph=None, contacts=store, affect_store=None)
        loop = AutonomyLoop(registry=registry)
        await loop._phase_relationships()

        # Nothing changed → no work counted (idempotent, avoids needless writes).
        assert loop.stats.scoring_runs == 0
    finally:
        await store.close()
