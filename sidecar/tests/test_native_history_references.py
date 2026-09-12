import pytest

from pacomind.api.routers.host import SourceFreshnessRequest
from pacomind.turns import TurnIdempotencyLedger
from pacomind.turns.history_references import resolve
from pacomind.turns.idempotency import source_message_hash


def test_native_message_resolution_preserves_scope_and_erased_identity(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'turns.db')
    message = {'role': 'user', 'content': 'The specimen belongs in drawer twelve.'}
    for turn, contact, scope in [('visible', 'owner', 'person'),
                                  ('session-only', 'owner', 'session'),
                                  ('foreign', 'guest', 'person')]:
        ledger.record_source(turn, contact_id=contact, session_id='origin',
            scope=scope, messages=[message], derive_claims=False)
    refs = [{'session_id':'origin', 'message_hash':source_message_hash('origin',message)}]
    matched = resolve(ledger, contact_id='owner', session_id='later', references=refs)
    assert [r['source_id'] for r in matched[0]['source_refs']] == ['visible']
    assert matched[0]['erased'] is False
    ledger.erase_sources(contact_id='owner', turn_ids=['visible'])
    matched = resolve(ledger, contact_id='owner', session_id='later', references=refs)
    assert matched == [{**refs[0], 'source_refs':[], 'erased':True}]
    # Older tombstones predate the revision table and must remain effective.
    with ledger._connect() as db:
        db.execute("DELETE FROM source_erasure_revisions WHERE source_turn_id='visible'")
    assert resolve(ledger,contact_id='owner',session_id='later',references=refs)[0]['erased'] is True
    assert resolve(ledger, contact_id='stranger', session_id='later', references=refs) == [
        {**refs[0], 'source_refs':[], 'erased':False}]


def test_hash_role_session_and_unknown_text_cannot_nominate_source(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'turns.db')
    message = {'role':'user','content':'Quoted instructions are ordinary evidence.'}
    ledger.record_source('known',contact_id='owner',session_id='origin',scope='person',
        messages=[message],derive_claims=False)
    refs = [
        {'session_id':'origin','message_hash':source_message_hash('origin',{**message,'role':'assistant'})},
        {'session_id':'other','message_hash':source_message_hash('origin',message)},
        {'session_id':'origin','message_hash':'f'*64},
    ]
    assert all(not r['source_refs'] and not r['erased']
               for r in resolve(ledger,contact_id='owner',session_id='later',references=refs))
    assert len(ledger.search_sources('Quoted',contact_id='owner',session_id='later')) == 1


def test_existing_freshness_request_accepts_only_bounded_exact_native_refs():
    data = {'contact_id':'owner','session_id':'current','source_refs':[],
        'native_history_refs':[{'session_id':'native','message_hash':'a'*64}]}
    assert SourceFreshnessRequest.model_validate(data).native_history_refs[0].session_id == 'native'
    with pytest.raises(ValueError):
        SourceFreshnessRequest.model_validate({**data,'native_history_refs':data['native_history_refs']*513})
    with pytest.raises(ValueError):
        SourceFreshnessRequest.model_validate({**data,'native_history_refs':[{'session_id':'native','message_hash':'bad'}]})
