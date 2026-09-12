"""Ordinary recall selects evidence without replaying unrelated global briefs."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from apsimo.api.routers import host
from apsimo.briefings.models import Briefing, BriefingSection
from test_contact_fact_recall import contact_context
from test_turn_source_evidence import source_app


@pytest.mark.asyncio
@pytest.mark.parametrize('scoped_projection', [False, True])
async def test_ordinary_recall_preserves_source_and_history_without_global_briefs(
        contact_context, monkeypatch, scoped_projection):
    runtime = contact_context
    monkeypatch.setenv('COLONY_RECALL_RERANK', 'off')
    if not scoped_projection:
        monkeypatch.setattr(host, '_p8_runtime', None)
    keys = json.loads(runtime.keyring.read_text())
    keys['principals'][0]['allow_unscoped_api'] = True
    keys['principals'][0]['scopes'].append('api:access')
    runtime.keyring.write_text(json.dumps(keys))
    fact = 'The hydrofoil pickup gate is violet.'
    runtime.ledger.record_source('pickup-source', contact_id='contact-a', session_id='earlier-text',
        messages=[{'role': 'user', 'content': fact}], derive_claims=True)
    brief = Briefing(briefing_id='retained-brief', created_at=datetime(2020, 7, 1, tzinfo=timezone.utc),
        sections=[BriefingSection(name='Operations', narrative='Obsolete unrelated operational narrative.',
                                  content={'unrelated': 'old operational detail ' * 2000})])
    calls = []

    def get_recent(limit=10):
        calls.append(limit)
        return [brief]

    monkeypatch.setattr(host, '_briefings_engine', SimpleNamespace(get_recent=get_recent))
    headers = {'Authorization': 'Bearer owner-key'}
    async with AsyncClient(transport=ASGITransport(app=runtime.app), base_url='http://test') as client:
        response = await client.post('/v1/host/context/assemble', headers=headers, json={
            'identity': {'host_id': 'native-fixture'},
            'context': {'contact_id': 'contact-a', 'session_id': 'later-voice'},
            'incoming_message': {'role': 'user', 'content': 'Which hydrofoil pickup gate?'}})
        assert response.status_code == 200, response.text
        sections = response.json()['sections']
        assert 'colony-briefing' not in {s['id'] for s in sections}
        assert calls == [], 'ordinary recall still queried the global briefing store'
        assert 'Obsolete unrelated' not in response.text
        memory = next(s for s in sections if s['id'] == 'colony-memory')
        assert fact in memory['body']
        assert any(c['source_id'] == 'pickup-source' for c in memory['citations'])

        retained = await client.get('/v1/host/briefings', headers=headers)
        assert retained.status_code == 200, retained.text
        assert retained.json()['briefings'][0]['id'] == 'retained-brief'
        assert retained.json()['briefings'][0]['body'] == 'Obsolete unrelated operational narrative.'
        assert calls == [10]
    assert len(brief.sections[0].content['unrelated']) == 46000
