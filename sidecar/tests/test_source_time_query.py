"""Dates inside supplied evidence do not become the request's observation window."""
from datetime import datetime, timezone
import json
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.beliefs.source_time import interpret_time_query
from colony_sidecar.turns import TurnIdempotencyLedger
from colony_sidecar.turns.source_vectors import SourceVectors
from test_turn_source_evidence import source_app


NOW = datetime(2026, 3, 12, 12, tzinfo=timezone.utc)
STAMP = '2026-03-12T09:14:30+00:00'
REPORT = ('The quartz archive comparison is recorded in this report: '
          + json.dumps({'verified_at': STAMP, 'result': 'digests matched'})
          + '. No restore was performed.')


@pytest.mark.parametrize('evidence', [
    REPORT,
    'Please inspect this recorded result: ' + json.dumps([{'verified_at': STAMP}]),
    'Inspect this report:\n```text\nThe camera recorded a visit at ' + STAMP + '.\n```',
    'Inspect this report:\n> The camera recorded a visit at ' + STAMP + '.',
    'Inspect this report: "The camera recorded a visit at ' + STAMP + '."',
    'Inspect this report: “The camera recorded a visit at ' + STAMP + '.”',
    "Inspect this report: 'The camera recorded a visit at " + STAMP + ".'",
])
def test_supplied_report_dates_do_not_filter_observation_time(evidence):
    query = interpret_time_query(evidence, now=NOW)
    assert query.mode == 'current'
    assert query.accepts_observation('2026-03-12T09:14:26+00:00')


@pytest.mark.parametrize('query_text,mode,start', [
    ('What did the camera record on 2026-03-12?', 'observed_range', '2026-03-12T00:00:00+00:00'),
    ('What was recorded on "2026-03-12"?', 'observed_range', '2026-03-12T00:00:00+00:00'),
    ('What was recorded at ' + STAMP + '?', 'observed_range', STAMP),
    ('Was a parcel spotted today?', 'observed_range', '2026-03-12T00:00:00+00:00'),
    ('What did the camera record in the last 2 hours?', 'observed_range', '2026-03-12T10:00:00+00:00'),
    ('What was recorded in the "last 2 hours"?', 'observed_range', '2026-03-12T10:00:00+00:00'),
    ('Where was my office as of March 5, 2026?', 'valid_range', '2026-03-05T00:00:00+00:00'),
    ('What tea should I bring later today?', 'current', NOW.isoformat()),
    ('What was recorded on 2026-03-11? Inspect this report: ' + REPORT,
     'observed_range', '2026-03-11T00:00:00+00:00'),
    ('Where was my office as of 2026-03-05? Compare with "The camera recorded this on 2026-03-12."',
     'valid_range', '2026-03-05T00:00:00+00:00'),
])
def test_explicit_temporal_requests_keep_their_existing_window(query_text, mode, start):
    query = interpret_time_query(query_text, now=NOW)
    assert (query.mode, query.start) == (mode, start)
    if query_text == 'What was recorded at ' + STAMP + '?':
        assert query.end == '2026-03-12T09:14:30.000001+00:00'


@pytest.mark.parametrize('query_text', [
    'office before 2026-03-12',
    'office between March 1, 2026 and March 5, 2026',
    'office last month',
    'office "last month"',
])
def test_unsupported_request_time_remains_unresolved(query_text):
    assert interpret_time_query(query_text, now=NOW).mode == 'unresolved_time'


@pytest.mark.asyncio
async def test_pasted_report_remains_recallable_at_its_actual_capture_time(source_app, tmp_path, monkeypatch):
    monkeypatch.setenv('COLONY_RECALL_RERANK', 'off')
    monkeypatch.setattr(SourceVectors, 'search', AsyncMock(return_value=([], [])))
    ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    ledger.record_source('quartz-report', contact_id='person', session_id='work',
        messages=[{'role': 'assistant', 'content': REPORT}], occurred_at='2026-03-12T09:14:26+00:00')
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://fixture') as client:
        response = await client.post('/v1/host/context/assemble', json={
            'identity': {'host_id': 'fixture'},
            'context': {'contact_id': 'person', 'session_id': 'later'},
            'incoming_message': {'role': 'user', 'content': REPORT}})
    assert response.status_code == 200, response.text
    packet = next((s for s in response.json()['sections'] if s['id'] == 'colony-memory'), None)
    assert packet is not None
    assert json.dumps(REPORT, ensure_ascii=False) in packet['body']
    assert packet['citations'] == ledger.source_references(
        ['quartz-report'], contact_id='person', session_id='later')
