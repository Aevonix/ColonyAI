"""Reply occurrence, not canonical callback ordering, determines eligibility."""
import pytest

from test_reply_forecasts import (
    runtime, ingress, source_app, coverage, dispatch, reply, history, headers,
    TemporalFollowups, forecasts, iso, PREFIX,
)
from test_transport_ingress_api import admit, capture


@pytest.mark.asyncio
async def test_preissue_reply_does_not_hide_subsequent_prospective_reply(runtime):
    r = runtime
    await coverage(r)
    await dispatch(r)
    origin = r.now[0]
    r.now[0] += 20
    await reply(r, occurred=origin-1)
    assert history(r)['outcomes'][-1]['status'] == 'unresolved'
    r.now[0] += 1
    await reply(r, suffix='second', sequence=2)
    assert history(r)['outcomes'][-1]['value'] is True
    assert len(history(r)['outcomes']) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('stop', ['cancel', 'expire', 'followup', 'parent_closed'])
@pytest.mark.parametrize('censor_first', [False, True])
async def test_reply_before_stop_survives_delayed_canonical_settlement(runtime, stop, censor_first):
    r = runtime
    await coverage(r)
    await dispatch(r)
    origin = r.now[0]
    waits = TemporalFollowups(r.commitments, clock=lambda:r.now[0])
    r.now[0] += 40
    if stop == 'cancel':
        waits.cancel(r.wait_id, evidence_ref='owner-stopped-task')
    elif stop == 'followup':
        waits.mark_followup_dispatched(r.wait_id, action_digest='actual-action', receipt_ref='actual-followup')
    elif stop == 'parent_closed':
        r.commitments.update(r.initial['commitment_id'], status='fulfilled', fulfilled_at=iso(r.now[0]))
    else:
        r.now[0] += 7200
    if censor_first:
        forecasts.reconcile(r.wait_id)
        assert history(r)['outcomes'][-1]['status'] == 'censored'
    # Trusted receipt occurrence was before the stop; canonical admission is later.
    await reply(r, occurred=origin+20)
    result = history(r)['outcomes'][-1]
    assert result['status'] == 'observed' and result['value'] is True
    assert result['observed_at'] == origin+20
    if censor_first:
        assert history(r)['outcomes'][0]['status'] == 'censored'


@pytest.mark.asyncio
@pytest.mark.parametrize('later_parent_close', [False, True])
async def test_reply_after_cancellation_cannot_gain_forecast_credit(runtime, later_parent_close):
    r = runtime
    await coverage(r)
    await dispatch(r)
    r.now[0] += 10
    TemporalFollowups(r.commitments, clock=lambda:r.now[0]).cancel(r.wait_id, evidence_ref='owner-stop')
    if later_parent_close:
        r.now[0] += 20
        r.commitments.update(r.initial['commitment_id'], status='fulfilled', fulfilled_at=iso(r.now[0]))
        # The reply preceded parent closure, but followed the earlier cancel.
        await reply(r, occurred=r.now[0]-10)
        assert history(r)['outcomes'][-1]['status'] == 'censored'
        return
    forecasts.reconcile(r.wait_id)
    r.now[0] += 10
    await reply(r)
    assert history(r)['outcomes'][-1]['status'] == 'censored'


@pytest.mark.asyncio
async def test_native_batch_settlement_reconciles_qualifying_sibling(runtime):
    r = runtime
    await coverage(r)
    await dispatch(r)
    r.now[0] += 20
    unrelated = await admit(r, occurred_at=r.now[0])
    qualifying = await reply(r, canonical=False, sequence=2)
    body = {'receipt_ids':[unrelated['receipt_id'], qualifying['receipt_id']], 'batch_id':'two-events'}
    response = await r.client.post(PREFIX+'/handoff', headers=headers(), json=body)
    assert response.status_code == 200, response.text
    native = {'session_id':'batch-session', 'task_id':'batch-task', 'turn_id':'batch-source'}
    response = await r.client.post(PREFIX+'/handoff', headers=headers(), json={**body, 'native_turn':native})
    assert response.status_code == 200, response.text
    response = await capture(r, native)
    assert response.status_code == 201, response.text
    # No follow-up receipt poll or owner read is allowed to close this test.
    assert history(r)['outcomes'][-1]['value'] is True
