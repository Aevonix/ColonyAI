from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
import hashlib

import pytest

from pacomind.commitments.store import CommitmentStore
from pacomind.initiatives.temporal_followup import TemporalFollowups, encoded


def fixture(tmp_path, **options):
    now = [2000000000.]
    store = CommitmentStore(tmp_path/'commitments.db')
    parent = store.create(person_id='owner',description='Obtain task result')
    ledger = TemporalFollowups(store,clock=lambda:now[0])
    params = dict(wait_id='wait-one',commitment_id=parent['id'],work_id='task-one',contact_id='person-one',outbound_ref='message:out',source_refs=['task:one'],source_versions={'task:one':'v1'},expected_after_seconds=60,expires_at=now[0]+86400)
    params.update(options)
    row = ledger.expect_reply(**params)
    return now,store,ledger,row,params


def match(**changes):
    return {'status':'matched','contact_id':'person-one','outbound_ref':'message:out',
            'matches':[{'external_ref':'message:reply','reply_to_ref':'message:out','receipt_ref':'receipt:reply',
                        'ts':2000000070.,'channel':'other-channel','reaction':None}],**changes}


def ack(now, ledger):
    ledger.acknowledge_dispatch('wait-one',receipt_ref='receipt:sent',occurred_at=now[0])
    now[0] += 70


def test_clock_requires_receipt_and_reply_before_ack_survives_restart(tmp_path):
    now, store, first, row, params = fixture(tmp_path)
    now[0] += 70
    assert first.due() == [] and first.preflight(row['wait_id'])['reason'] == 'awaiting_dispatch_evidence'
    first.apply_reply('wait-one',match())
    second = TemporalFollowups(store,clock=lambda:now[0])
    after = second.acknowledge_dispatch('wait-one',receipt_ref='receipt:sent',occurred_at=2000000000.)
    assert after['state'] == 'resolved' and after['expected_at'] == 2000000060.
    assert second.preflight('wait-one')['review_allowed'] is False
    assert second.list_for_context(contact_id='person-one')[0]['reply']['matches'][0]['channel'] == 'other-channel'


def test_exact_reply_and_cross_session_cancel_share_one_wait(tmp_path):
    now, store, first, row, params = fixture(tmp_path)
    ack(now,first)
    second = TemporalFollowups(store,clock=lambda:now[0])
    assert first.preflight('wait-one')['review_allowed']
    for invalid in (match(contact_id='stranger'),match(outbound_ref='unrelated'),match(matches=[{'external_ref':'message:reply','reply_to_ref':'message:out','ts':now[0]}])):
        assert second.apply_reply('wait-one',invalid)['state'] == 'open'
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda ledger:ledger.apply_reply('wait-one',match()),[first,second]))
    assert all(r['state'] == 'resolved' for r in results)
    assert len(first.get('wait-one')['reply']['matches']) == 1
    assert not first.preflight('wait-one')['dispatch_allowed']


def test_deadline_and_late_reply_remain_distinct(tmp_path):
    now, store, ledger, row, _ = fixture(tmp_path,expires_at=2000000065.)
    ack(now,ledger)
    assert ledger.preflight('wait-one')['reason'] == 'expired'
    value = ledger.apply_reply('wait-one',match())
    assert value['state'] == 'expired' and value['reply']


def test_quiet_availability_and_dst_are_evaluated_from_actual_local_time(tmp_path):
    now,store,ledger,row,_ = fixture(tmp_path,timezone_name='America/New_York',quiet_start='22:00',quiet_end='07:00',availability_start='08:00',availability_end='18:00',expires_at=2100000000.)
    ack(now,ledger)
    # Both occurrences of 01:30 during the DST fold are quiet, not an extra send.
    for instant in ('2033-11-06T01:30:00-04:00','2033-11-06T01:30:00-05:00'):
        now[0] = datetime.fromisoformat(instant).timestamp()
        assert ledger.preflight('wait-one')['reason'] == 'quiet_window'
    now[0] = datetime.fromisoformat('2033-11-06T07:30:00-05:00').timestamp()
    assert ledger.preflight('wait-one')['reason'] == 'outside_availability'
    now[0] += 3600
    assert ledger.preflight('wait-one')['review_allowed']


def test_defer_parent_resolution_and_source_correction_cancel_pending_work(tmp_path):
    now,store,ledger,row,_ = fixture(tmp_path)
    ack(now,ledger)
    ledger.defer('wait-one',until=now[0]+60,evidence_ref='receipt:owner-snooze')
    assert ledger.due() == []
    now[0] += 61
    assert len(ledger.due()) == 1
    assert ledger.invalidate_sources(['task:one'],evidence_ref='receipt:correction') == ['wait-one']
    assert ledger.preflight('wait-one')['reason'] == 'cancelled'
    assert ledger.invalidate_sources(['task:one'],evidence_ref='receipt:correction') == []


def test_native_binding_survives_reply_attachment_race_and_closes_scan(tmp_path):
    now,store,ledger,row,_ = fixture(tmp_path)
    ack(now,ledger)
    ledger.apply_reply('wait-one',match())
    assert ledger.bind_native_task('wait-one',native_task_id='native-one')['state'] == 'resolved'
    assert len(ledger.due()) == 1
    ledger.observe_native_terminal('wait-one',native_task_id='native-one',native_status='archived')
    assert ledger.due() == []


def test_existing_consumed_task_authority_and_outbox_truth_are_both_required(tmp_path):
    scope = {'task':'task-one','recipient':'person-one','channels':['channel-a'],'purpose':'obtain result','max_followups':1}
    digest = hashlib.sha256(encoded(scope).encode()).hexdigest()
    now,store,ledger,row,_ = fixture(tmp_path,authority_scope=scope)
    ack(now,ledger)
    binding = SimpleNamespace(scope=scope,scope_digest=digest,action_digest='action-one')
    grant = {'grant_id':'grant-one','status':'exhausted','scope':scope}
    use = {'grant_id':'grant-one','scope_digest':digest,'grant_status':'exhausted','grant_expires_at':None}
    authority = SimpleNamespace(get_grant_use=lambda d:use if d=='action-one' else None,list_grants=lambda **kw:[grant])
    assert not ledger.preflight('wait-one',outbox_state='absent')['dispatch_allowed']
    for state in ('unknown','prepared','submitted','acknowledged','uncertain'):
        assert not ledger.preflight('wait-one',authority=authority,binding=binding,outbox_state=state)['dispatch_allowed']
    assert ledger.preflight('wait-one',authority=authority,binding=binding,outbox_state='absent')['dispatch_allowed']
    grant['status']='revoked'
    assert not ledger.preflight('wait-one',authority=authority,binding=binding,outbox_state='absent')['dispatch_allowed']
    grant['status']='exhausted'
    ledger.mark_followup_dispatched('wait-one',action_digest='action-one',receipt_ref='receipt:followup')
    assert ledger.preflight('wait-one',authority=authority,binding=binding,outbox_state='absent')['reason'] == 'followup_already_recorded'


def test_foreign_context_and_duplicate_condition_bounds(tmp_path):
    now,store,ledger,row,params = fixture(tmp_path)
    assert ledger.list_for_context(contact_id='other') == []
    assert ledger.expect_reply(**params)['wait_id'] == row['wait_id']
    with pytest.raises(ValueError,match='identity conflict'):
        ledger.expect_reply(**{**params,'expected_after_seconds':120})
    with pytest.raises(ValueError,match='offset'):
        ledger.acknowledge_dispatch('wait-one',receipt_ref='receipt:sent',occurred_at='2033-05-18T03:33:00')


def test_reply_learned_after_later_ack_keeps_earlier_provider_time(tmp_path):
    now,store,ledger,row,_ = fixture(tmp_path)
    now[0] += 100
    ledger.acknowledge_dispatch('wait-one',receipt_ref='receipt:delivery-read',occurred_at=2000000090.)
    # The accepted ACK was lost. The provider-linked reply happened at t+70,
    # before the delivery/read receipt we learned first at t+90.
    resolved = ledger.apply_reply('wait-one',match())
    assert resolved['state'] == 'resolved'
    assert resolved['reply']['matches'][0]['ts'] == 2000000070.
    assert resolved['dispatch_occurred_at'] == 2000000090.


def test_exact_reply_before_wait_registration_uses_parent_obligation_origin(tmp_path):
    now,store,ledger,row,_ = fixture(tmp_path)
    # The durable parent predates the wait; the provider supplied an exact
    # parent reference for a fast response learned only after registration.
    earlier = match(matches=[{'external_ref':'message:fast-reply','reply_to_ref':'message:out',
        'receipt_ref':'receipt:fast-reply','ts':row['created_at']-1,'channel':'other-channel','reaction':None}])
    resolved = ledger.apply_reply('wait-one',earlier)
    assert resolved['state'] == 'resolved'
    assert resolved['reply']['matches'][0]['ts'] < row['created_at']
