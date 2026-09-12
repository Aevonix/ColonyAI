"""The rollback reader preserves and erases original tool evidence without a writer."""
import copy

import httpx

from pacomind.turns import TurnIdempotencyLedger
from test_hermes_turn_outbox import _load_client


def stored_origin(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'canonical.db')
    ledger.record_source('origin', contact_id='owner', session_id='native',
        messages=[{'role': 'user', 'content': 'Inspect the copper synchronization.'}],
        derive_claims=False)
    origin = ledger.source_references(['origin'], contact_id='owner', session_id='native')[0]
    return ledger, origin


def test_persisted_original_is_recalled_and_erased_with_its_origin(tmp_path):
    ledger, origin = stored_origin(tmp_path)
    ledger.record_source('original', contact_id='owner', session_id='native', messages=[{
        'role': 'tool', 'content': 'Copper synchronization completed with exit code 3.',
        '_native_tool_observation': 'native-tool-observation-v1',
        '_observation_sources': [origin],
    }], derive_claims=False)
    reader = TurnIdempotencyLedger(ledger.db_path)
    rows = reader.search_sources('copper synchronization', contact_id='owner', session_id='later')
    assert any(row['turn_id'] == 'original' and row['role'] == 'tool' for row in rows)
    erased = reader.erase_sources(contact_id='owner', turn_ids=['origin'])
    assert 'original' in erased['affected_source_ids']
    reopened = TurnIdempotencyLedger(ledger.db_path)
    assert not reopened.search_sources('copper synchronization', contact_id='owner', session_id='later')


def test_unavailable_observation_writer_keeps_pending_bytes_until_origin_erasure(tmp_path, monkeypatch):
    ledger, origin = stored_origin(tmp_path)
    module = _load_client('observation_reader_floor_client')
    outbox = module.TurnOutbox(tmp_path / 'outbox.db')
    payload = {'turn_id': 'queued-original', 'contact_id': 'owner', 'session_id': 'native',
        'require_source_receipt': True, 'observation': {'origin': origin, 'sources': [],
            'content': 'Copper synchronization completed with exit code 3.'}}
    original = copy.deepcopy(payload)
    outbox.enqueue('queued-original', payload)
    client = module.PacoMindClient(url='http://127.0.0.1:1', api_key='fixture-never-sent')
    monkeypatch.setattr(client, 'get', lambda *a, **k: httpx.Response(200,
        json=ledger.erasure_feed('owner'), request=httpx.Request('GET', 'http://fixture')))
    for method in ('put', 'post'):
        monkeypatch.setattr(client, method, lambda *a, **k: httpx.Response(404,
            request=httpx.Request('PUT', 'http://fixture')))
    # The old client rejects the keyword before HTTP. A later client may reach
    # an unavailable endpoint. Neither is a receipt confirming persistence.
    assert outbox.drain(lambda stored, timeout_seconds: client.sync_turn(
        **stored, outbox=outbox, timeout_seconds=timeout_seconds), limit=1, timeout_seconds=.25) == 0
    row = module.TurnOutbox(outbox.path).snapshot()[0]
    assert row['state'] == 'pending' and row['payload'] == original
    ledger.erase_sources(contact_id='owner', turn_ids=['origin'])
    outbox.apply_erasure_page('owner', ledger.erasure_feed('owner'))
    assert module.TurnOutbox(outbox.path).snapshot() == []
    assert outbox.enqueue('queued-original', original)['state'] == 'erased'
