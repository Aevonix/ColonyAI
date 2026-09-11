"""Retired composite closeness is preserved as history, never refreshed."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apsimo.autonomy.loop import AutonomyLoop
from apsimo.contacts.config import ContactsConfig
from apsimo.contacts.store import SQLiteContactStore
from apsimo.contacts.scoring import compute_relationship_score


@pytest.mark.asyncio
async def test_relationship_phase_preserves_legacy_value_without_governing(tmp_path):
    store = SQLiteContactStore(config=ContactsConfig(sqlite_path=str(tmp_path / 'c.db')))
    await store.connect()
    try:
        contact = await store.create(display_name='Quiet contact', trust_tier='regular')
        await store.update_relationship_score(contact.contact_id, .95)
        assert compute_relationship_score(await store.get(contact.contact_id), None) is None
        registry = SimpleNamespace(graph=None, contacts=store, affect_store=None)
        loop = AutonomyLoop(registry=registry)
        await loop._phase_relationships()
        assert (await store.get(contact.contact_id)).relationship_score == .95
        assert loop.stats.scoring_runs == 0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_relationship_phase_does_not_poll_or_rescore_from_mood_and_silence():
    graph, contacts, affect = AsyncMock(), AsyncMock(), AsyncMock()
    loop = AutonomyLoop(registry=SimpleNamespace(graph=graph, contacts=contacts, affect_store=affect))
    await loop._phase_relationships()
    assert graph.mock_calls == contacts.mock_calls == affect.mock_calls == []
    assert loop.stats.scoring_runs == 0
