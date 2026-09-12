"""Source forgetting reaches exact communication summaries without deleting receipts."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient
import pytest

from apsimo.api.routers import host
from apsimo.contacts.comms import CommsLog
from apsimo.turns import TurnIdempotencyLedger
from apsimo.turns.idempotency import SourceErased
from test_turn_source_evidence import source_app


def turn(identifier, text, *, contact='person'):
    return {'identity': {'host_id': 'fixture'}, 'context': {
        'turn_id': identifier, 'session_id': 'shared-session', 'contact_id': contact,
        'channel_id': 'sms:fixture'}, 'user_message': {'role': 'user', 'content': text},
        'assistant_message': {'role': 'assistant', 'content': 'Recorded: ' + text},
        'summary': text}


@pytest.mark.asyncio
async def test_ordinary_capture_forget_removes_only_linked_summaries(source_app, tmp_path, monkeypatch):
    log = CommsLog(str(tmp_path / 'communications.db'))
    monkeypatch.setattr(host, '_comms_log', log)
    log.log('person', summary='An older unlinked note.', session_id='shared-session')
    log.log_receipt(event_id='receipt-event', contact_id='person', channel='sms:fixture', direction='out',
        external_ref='provider-message', receipt_ref='transport-receipt',
        occurred_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), status='accepted')
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://fixture') as client:
        for identifier, text in [('first', 'Copper inventory contains seven crates.'),
                                 ('second', 'The blue bicycle needs a new bell.')]:
            response = await client.put('/v2/host/turns/' + identifier, json=turn(identifier, text))
            assert response.status_code == 201 and response.json()['source_recorded']
        assert any('Copper inventory' in row['summary'] for row in log.history('person'))
        erased = await client.post('/v1/host/memory/sources/forget',
                                   json={'contact_id': 'person', 'source_ids': ['first']})
        assert erased.status_code == 200 and erased.json()['source_erased']
        assert not any('Copper inventory' in row['summary'] for row in log.history('person'))
        assert erased.json()['communications_cleanup'] == 'complete'
        assert erased.json()['communications_scope'] == 'source_linked_summaries_only'
        assert erased.json()['communications_unlinked_rows'] == 1
        assert erased.json()['host_reconciliation'] == 'not_observed'
        assert any('blue bicycle' in row['summary'] for row in log.recent())
        assert any('older unlinked' in row['summary'] for row in log.history('person'))
        assert log.outbound_receipts(contact_id='person', outbound_ref='provider-message')
        with sqlite3.connect(log._db_path) as db:
            assert not db.execute("SELECT 1 FROM communications WHERE summary LIKE '%Copper inventory%'").fetchall()
        retry = await client.put('/v2/host/turns/first', json=turn('first', 'Copper inventory contains seven crates.'))
        assert retry.json()['source_recorded'] is False
        reopened = CommsLog(log._db_path)
        assert not any('Copper inventory' in row['summary'] for row in reopened.history('person'))


def linked_log(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'canonical.db')
    ledger.record_source('original', contact_id='person', session_id='session',
        messages=[{'role': 'user', 'content': 'Copper inventory has seven crates.'}], derive_claims=False)
    log = CommsLog(str(tmp_path / 'communications.db'), source_ledger=ledger)
    lineage, _ = log.source_input('original', 'person')
    return ledger, log, lineage


def test_reopened_read_reconciles_erasure_and_keeps_unlinked_other_person(tmp_path):
    ledger, log, lineage = linked_log(tmp_path)
    log.log('person', summary='Copper inventory has seven crates.', source_lineage=lineage)
    with pytest.raises(SourceErased):
        log.log('other', summary='Cannot borrow another contact source.', source_lineage=lineage)
    log.log('other', summary='Copper inventory has seven crates.')
    ledger.erase_sources(contact_id='person', turn_ids=['original'])  # No API cleanup callback.
    reopened = CommsLog(log._db_path, source_ledger=ledger)
    assert reopened.recent() == [
        {'contact_id': 'other', **row} for row in log.history('other')]
    assert reopened.history('person') == [] and reopened.last_per_channel('person') == {}
    assert reopened.last_outbound('person') is None
    with sqlite3.connect(log._db_path) as db:
        assert db.execute('SELECT count(*) FROM communications').fetchone()[0] == 1
    with pytest.raises(SourceErased):
        log.log('person', summary='A stale retry.', source_lineage=lineage)


def test_erasure_during_projection_write_is_reconciled_before_return(tmp_path, monkeypatch):
    ledger, log, lineage = linked_log(tmp_path)
    visible, calls = log._source_visible, []
    def check(person, reference):
        calls.append(person)
        if len(calls) == 2:
            ledger.erase_sources(contact_id='person', turn_ids=['original'])
        return visible(person, reference)
    monkeypatch.setattr(log, '_source_visible', check)
    with pytest.raises(SourceErased):
        log.log('person', summary='Copper inventory has seven crates.', source_lineage=lineage)
    assert len(calls) == 2
    assert log._conn.execute('SELECT count(*) FROM communications').fetchone()[0] == 0


def test_parent_erasure_removes_linked_answer_summary_and_keeps_user_source(tmp_path):
    ledger, log, _ = linked_log(tmp_path)
    references = ledger.source_references(['original'], contact_id='person', session_id='later')
    ledger.record_source('answer', contact_id='person', session_id='later', derive_claims=False,
        messages=[{'role': 'user', 'content': 'Please summarize the inventory.'},
                  {'role': 'assistant', 'content': 'Seven crates are available.', '_supplied_sources': references}])
    answer_lineage, _ = log.source_input('answer', 'person')
    log.log('person', direction='out', summary='Seven crates are available.', source_lineage=answer_lineage)
    result = ledger.erase_sources(contact_id='person', turn_ids=['original'])
    assert 'answer' in result['affected_source_ids']
    assert log.purge_erased_sources(result['source_ids'] + result['affected_source_ids'], contact_id='person') == 1
    assert log.history('person') == []
    with sqlite3.connect(ledger.db_path) as db:
        remaining = json.loads(db.execute("SELECT messages_json FROM turn_sources WHERE turn_id='answer'").fetchone()[0])
    assert remaining == [{'role': 'user', 'content': 'Please summarize the inventory.'}]


def test_legacy_rows_are_not_backlinked_by_identical_summary_or_session(tmp_path):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE communications(id TEXT PRIMARY KEY, contact_id TEXT NOT NULL,
            channel TEXT, direction TEXT NOT NULL, summary TEXT, session_id TEXT, ts TEXT NOT NULL)''')
        db.execute("INSERT INTO communications VALUES ('old','person','sms','in',?,'session','2026-01-01')",
                   ('Copper inventory has seven crates.',))
    ledger, _, _ = linked_log(tmp_path)
    log = CommsLog(str(path), source_ledger=ledger)
    ledger.erase_sources(contact_id='person', turn_ids=['original'])
    assert log.purge_erased_sources(['original'], contact_id='person') == 0
    assert log.unlinked_summary_count('person') == 1
    assert log.history('person')[0]['summary'] == 'Copper inventory has seven crates.'


@pytest.mark.asyncio
async def test_projection_cleanup_failure_stays_pending_and_read_retries(source_app, tmp_path, monkeypatch):
    log = CommsLog(str(tmp_path / 'communications.db'))
    monkeypatch.setattr(host, '_comms_log', log)
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://fixture') as client:
        await client.put('/v2/host/turns/first', json=turn('first', 'Copper inventory has seven crates.'))
        purge = log.purge_erased_sources
        with monkeypatch.context() as fault:
            fault.setattr(log, 'purge_erased_sources', lambda *a, **kw: (_ for _ in ()).throw(OSError('offline')))
            result = (await client.post('/v1/host/memory/sources/forget',
                json={'contact_id': 'person', 'source_ids': ['first']})).json()
            assert result['source_erased'] and result['communications_cleanup'] == 'pending'
        assert log.history('person') == []
        assert purge(['first'], contact_id='person') == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('disabled', [False, True])
async def test_absent_store_status_is_not_a_deletion_claim(source_app, tmp_path, monkeypatch, disabled):
    import apsimo.vector as vector
    monkeypatch.setattr(vector, '_store', None)
    monkeypatch.setenv('COLONY_GRAPH_ENABLED', 'false' if disabled else 'true')
    monkeypatch.setenv('COLONY_EMBED_PROVIDER', 'skip' if disabled else 'openai_api')
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    ledger.record_source('original', contact_id='person', session_id='session',
        messages=[{'role': 'user', 'content': 'A neutral fact.'}], derive_claims=False)
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://fixture') as client:
        result = (await client.post('/v1/host/memory/sources/forget',
            json={'contact_id': 'person', 'source_ids': ['original']})).json()
    assert result['source_erased']
    assert result['graph_cleanup'] == result['vector_cleanup'] == ('disabled_not_checked' if disabled else 'unavailable')
    assert result['host_reconciliation'] == 'not_observed'


@pytest.mark.asyncio
async def test_configured_store_failure_remains_pending(source_app, tmp_path, monkeypatch):
    import apsimo.vector as vector
    graph = SimpleNamespace(delete_source_memories=AsyncMock(side_effect=OSError('offline')))
    vectors = SimpleNamespace(catalog=object(), erase_source_projections=AsyncMock(side_effect=OSError('offline')))
    monkeypatch.setattr(host, '_graph', graph)
    monkeypatch.setattr(vector, '_store', vectors)
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    ledger.record_source('original', contact_id='person', session_id='session',
        messages=[{'role': 'user', 'content': 'A neutral fact.'}], derive_claims=False)
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url='http://fixture') as client:
        result = (await client.post('/v1/host/memory/sources/forget',
            json={'contact_id': 'person', 'source_ids': ['original']})).json()
    assert result['source_erased'] and result['graph_cleanup'] == result['vector_cleanup'] == 'pending'
    graph.delete_source_memories.assert_awaited_once()
    vectors.erase_source_projections.assert_awaited_once()
