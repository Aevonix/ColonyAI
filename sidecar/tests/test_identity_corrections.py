"""Identity hypotheses cannot inherit another person's history or authority."""
import json

import pytest

from colony_sidecar.contacts.comms import CommsLog
from colony_sidecar.contacts.config import ContactsConfig
from colony_sidecar.contacts.store import SQLiteContactStore
from colony_sidecar.identity.participants import ParticipantResolver


@pytest.fixture
async def store(tmp_path):
    value = SQLiteContactStore(ContactsConfig(sqlite_path=str(tmp_path / 'contacts.db')))
    await value.connect()
    yield value
    await value.close()


async def test_name_candidate_has_separate_repeated_identity_then_exact_correction(store):
    target = await store.create(display_name='Robin', trust_tier='trusted')
    scope = await store.create_scope(scope_type='group', platform='whatsapp', external_id='group')
    await store.add_scope_member(scope.scope_id, target.contact_id, 'member')
    resolver = ParticipantResolver(store)
    first = await resolver.resolve(platform='whatsapp', user_id='test@lid', display_name='Robin', group_id='group')
    assert first.contact_id != target.contact_id
    assert first.candidate_contact_id == target.contact_id
    assert not (await store.get(first.contact_id)).interaction_allowed
    assert not await store.get_handles(target.contact_id)
    assert (await resolver.resolve(platform='whatsapp', user_id='test@lid')).contact_id == first.contact_id
    inputs = dict(operation_id='correction-one', performed_by='owner-test', gateway='whatsapp', address='test@lid',
                  expected_contact_id=first.contact_id, contact_id=target.contact_id,
                  evidence_refs=['source:owner-confirmation'], affected_source_ids=['turn:one'])
    receipt = await store.correct_handle_identity(**inputs)
    assert receipt == await store.correct_handle_identity(**inputs)
    assert receipt['old_contact_id'] == first.contact_id
    assert receipt['authority_granted'] is False
    assert (await resolver.resolve(platform='whatsapp', user_id='test@lid')).contact_id == target.contact_id
    assert await store.list_handle_proposals() == []
    with pytest.raises(ValueError, match='identity_operation_conflict'):
        await store.correct_handle_identity(**(inputs | {'affected_source_ids': ['turn:other']}))
    view = await store.identity_evidence(target.contact_id)
    assert view['handles'][0]['identity_status'] == 'confirmed'
    assert view['candidates'][0]['status'] == 'confirmed'


async def test_rejected_candidate_does_not_reappear_or_change_observed_handle(store):
    target = await store.create(display_name='One')
    first = await store.propose_handle_link(target.contact_id, 'email', 'OTHER@example.test')
    await store.correct_handle_identity(operation_id='reject', performed_by='owner-test',
        gateway='email', address='other@example.test', expected_contact_id=None, contact_id=None,
        evidence_refs=['source:denial'])
    repeated = await store.propose_handle_link(target.contact_id, 'email', 'other@example.test')
    assert first['candidate_id'] == repeated['candidate_id']
    assert repeated['status'] == 'rejected'
    assert await store.resolve_messaging_handle('email', 'other@example.test') is None


async def test_phone_country_codes_do_not_collide_and_cross_channel_aliases_work(store):
    first = await store.create(display_name='First')
    second = await store.create(display_name='Second')
    await store.add_handle(first.contact_id, 'sms', '+12125550101', verified=True)
    await store.add_handle(second.contact_id, 'signal', '+442125550101', verified=True)
    assert (await store.resolve_messaging_handle('whatsapp', '12125550101@s.whatsapp.net')).contact_id == first.contact_id
    assert (await store.resolve_messaging_handle('rcs', '(212) 555-0101')).contact_id == first.contact_id
    assert (await store.resolve_messaging_handle('signal', '+44 2125550101')).contact_id == second.contact_id


async def test_existing_low_confidence_name_handle_is_not_authority(store):
    target = await store.create(display_name='Old')
    await store.add_handle(target.contact_id, 'email', 'guess@example.test', source='auto:scoped-name', confidence=.6)
    assert await store.resolve_handle('email', 'guess@example.test') is None
    assert await store.resolve_messaging_handle('email', 'guess@example.test') is None
    evidence = await store.identity_evidence(target.contact_id)
    assert evidence['handles'][0]['identity_status'] == 'tentative'


def test_reply_reference_matches_across_channels_without_treating_inbound_as_answer(tmp_path):
    log = CommsLog(str(tmp_path / 'comms.db'))
    log.log('cid-one', channel='voice', external_ref='incoming-1', receipt_ref='receipt-1', ts='2026-01-01T01:00:00Z')
    args = dict(contact_id='cid-one', outbound_ref='sent-1', since_iso='2026-01-01T00:00:00Z', until_iso='2026-01-01T04:00:00Z')
    assert log.match_reply(**args)['status'] == 'unrelated'
    log.log('cid-two', external_ref='incoming-2', receipt_ref='receipt-2', reply_to_ref='sent-1', ts='2026-01-01T01:00:00Z')
    log.log('cid-one', external_ref='incoming-3', reply_to_ref='sent-1', ts='2026-01-01T01:00:00Z')
    assert log.match_reply(**args)['status'] == 'unrelated'
    for _ in range(2):
        log.log('cid-one', channel='email', external_ref='incoming-4', receipt_ref='receipt-4',
                reply_to_ref='sent-1', ts='2026-01-01T03:00:00+02:00')
    result = log.match_reply(**args)
    assert result['status'] == 'matched' and len(result['matches']) == 1
    assert result['condition_satisfied'] is False


async def test_migration_retires_only_name_guesses_and_preserves_candidate_evidence(store):
    target = await store.create(display_name='Legacy')
    await store.add_handle(target.contact_id, 'email', 'guess@example.test', source='auto:scoped-name', confidence=.6)
    await store.add_handle(target.contact_id, 'email', 'confirmed@example.test', source='auto:scoped-name', verified=True)
    await store.add_handle(target.contact_id, 'sms', '+12125550101', source='auto:sender')
    db = store._require_db()
    await db.execute("DELETE FROM schema_version WHERE version='005'")
    await db.commit()
    await store.close()
    await store.connect()
    assert {h.address for h in await store.get_handles(target.contact_id)} == {'confirmed@example.test', '+12125550101'}
    candidate = await store.propose_handle_link(target.contact_id, 'email', 'guess@example.test')
    assert candidate['status'] == 'pending'
    resolved = await ParticipantResolver(store).resolve(platform='email', user_id='guess@example.test', display_name='Legacy')
    assert resolved.contact_id and resolved.contact_id != target.contact_id
    await store.close()
    await store.connect()
    assert (await store.propose_handle_link(target.contact_id, 'email', 'guess@example.test'))['candidate_id'] == candidate['candidate_id']


async def test_pending_correction_reconciles_after_restart_without_another_queue(store, tmp_path):
    from colony_sidecar.turns.idempotency import TurnIdempotencyLedger
    from colony_sidecar.turns.source_attribution import correct
    old, new = await store.create(display_name='First'), await store.create(display_name='Second')
    await store.add_handle(old.contact_id, 'email', 'reply@example.test')
    args = dict(operation_id='resume', performed_by='owner-test', gateway='email', address='reply@example.test',
        expected_contact_id=old.contact_id, contact_id=new.contact_id, evidence_refs=['source:confirm'],
        affected_source_ids=['direct'])
    first = await store.correct_handle_identity(**args)
    await store.close()
    await store.connect()
    assert await store.pending_identity_reconciliations() == [first]
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    ledger.record_source('direct', contact_id=old.contact_id, session_id='session',
                         messages=[{'role': 'user', 'content': 'The bicycle is blue.'}])
    corrected = correct(ledger, operation_id='resume', performed_by='owner-test', old_contact_id=old.contact_id,
        contact_id=new.contact_id, source_ids=['direct'], evidence_refs=['source:confirm'])
    with pytest.raises(ValueError, match='reconciliation_mismatch'):
        await store.mark_sources_reconciled('resume', corrected | {'source_ids': ['wrong']})
    assert await store.pending_identity_reconciliations() == [first]
    result = await store.mark_sources_reconciled('resume', corrected)
    assert await store.pending_identity_reconciliations() == []
    assert result == await store.mark_sources_reconciled('resume', corrected)
    assert result == await store.correct_handle_identity(**args)
