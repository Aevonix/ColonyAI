import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from pacomind.contacts.transport_ingress import TransportIngress, ensure_schema


def opened(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn, TransportIngress(conn)


def admit(store, sequence=1, **overrides):
    return store.admit(**(dict(producer='bridge', account_id='account', epoch='epoch', sequence=sequence,
        event_id='provider-' + str(sequence), contact_id='contact', occurred_at=100,
        journal_ref='epoch:' + str(sequence), payload_digest='a'*64, media_available=True,
        metadata={'channel': 'whatsapp', 'sender_ref': 'fixture'}, now=200) | overrides))


def test_two_connections_claim_once_and_native_source_settles_after_restart(tmp_path):
    path = tmp_path/'communications.db'
    conn, store = opened(path); ensure_schema(conn)
    receipt = admit(store)['receipt_id']; conn.close()
    barrier = Barrier(2)
    def claim():
        conn, store = opened(path)
        try:
            barrier.wait(timeout=5)
            return store.handoff(producer='bridge', receipt_ids=[receipt], batch_id='one')['may_dispatch']
        finally:
            conn.close()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sorted(results) == [False, True]
    conn, store = opened(path)
    native = {'session_id': 'session', 'task_id': 'task', 'turn_id': 'source'}
    store.handoff(producer='bridge', receipt_ids=[receipt], batch_id='one', native_turn=native)
    assert store.for_canonical_turn(turn_id='source', contact_id='other') == []
    assert store.for_canonical_turn(turn_id='source', contact_id='contact')[0]['state'] == 'handed_off'
    store.complete(native_turn=native, source_versions={'source': 'v1'}, outcome='captured', now=210)
    assert store.get(receipt)['state'] == 'completed'
    with pytest.raises(ValueError, match='completion_conflict'):
        store.complete(native_turn=native, source_versions={'source': 'v2'}, outcome='captured')
    assert store.erase_sources(['source']) == [{'receipt_id': receipt, 'journal_ref': 'epoch:1'}]
    assert store.get(receipt)['state'] == 'erased'
    conn.close()


def test_lost_ack_repair_conflicts_and_erasure_before_canonical_completion(tmp_path):
    conn, store = opened(tmp_path/'comms.db'); ensure_schema(conn)
    receipt = admit(store, media_available=False)
    assert admit(store, media_available=False) == receipt
    with pytest.raises(ValueError, match='not_ready'):
        store.handoff(producer='bridge', receipt_ids=[receipt['receipt_id']], batch_id='one')
    repaired = admit(store, payload_digest='b'*64)
    assert repaired['media_available'] == 1
    with pytest.raises(ValueError, match='event_conflict'):
        admit(store, payload_digest='b'*64, contact_id='different')
    store.handoff(producer='bridge', receipt_ids=[receipt['receipt_id']], batch_id='one')
    native = {'session_id': 'session', 'task_id': 'task', 'turn_id': 'source'}
    store.handoff(producer='bridge', receipt_ids=[receipt['receipt_id']], batch_id='one', native_turn=native)
    assert store.erase_sources(['source'])
    store.complete(native_turn=native, source_versions={'source':'v1'}, outcome='captured')
    assert store.get(receipt['receipt_id'])['state'] == 'erased'
    conn.close()


def test_coverage_requires_connection_watermark_freshness_and_no_activity(tmp_path):
    conn, store = opened(tmp_path/'comms.db'); ensure_schema(conn)
    observation = dict(producer='bridge', account_id='account', epoch='epoch', connected_since=90,
        observed_at=120, watermark=0, connected=True, unavailable=0, now=120)
    store.observe_coverage(**observation)
    assert store.coverage(producer='bridge', account_id='account',contact_id='contact',since=100,now=121)['observed']
    assert not store.coverage(producer='bridge', account_id='account',contact_id='contact',since=80,now=121)['observed']
    store.observe_coverage(**(observation | {'watermark':1}))
    assert 'intake_gap' in store.coverage(producer='bridge',account_id='account',contact_id='contact',since=100,now=121)['reasons']
    admit(store)
    result = store.coverage(producer='bridge',account_id='account',contact_id='contact',since=100,now=121)
    assert result['reasons'] == ['recipient_activity_requires_review']
    assert store.coverage(producer='bridge',account_id='account',contact_id='other',since=100,now=121)['observed']
    assert not store.coverage(producer='bridge',account_id='account',contact_id='other',since=100,now=126)['observed']
    # A new, actually observed interval excludes older expired sequence gaps,
    # while an older since still cannot claim uninterrupted coverage.
    store.observe_coverage(**(observation | {'watermark':4,'sequence_floor':4,'connected_since':130,'observed_at':140,'now':140}))
    assert store.coverage(producer='bridge',account_id='account',contact_id='other',since=131,now=141)['observed']
    assert not store.coverage(producer='bridge',account_id='account',contact_id='other',since=120,now=141)['observed']
    with pytest.raises(ValueError,match='invalid_ingress_coverage'):
        store.observe_coverage(**(observation | {'sequence_floor':1}))
    conn.close()
