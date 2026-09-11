"""Stopped observation windows do not become reply misses."""
import pytest

from test_reply_forecasts import (
    runtime, ingress, source_app, coverage, dispatch, reply, view, history, headers,
    TemporalFollowups, ExpectationEngine,
)


@pytest.mark.asyncio
@pytest.mark.parametrize('stop', ['cancel', 'expire', 'followup', 'parent_closed'])
async def test_pending_reply_forecast_is_censored_when_wait_stops(runtime, stop):
    r = runtime
    first = await coverage(r)
    await dispatch(r)
    waits = TemporalFollowups(r.commitments, clock=lambda: r.now[0])
    r.now[0] += 20
    if stop == 'cancel':
        result = await r.client.post('/v1/host/temporal-followups/'+r.wait_id+'/change',
            headers=headers('owner-agent'), json={'contact_id':'owner',
                'operation':'cancel', 'evidence_ref':'owner-stopped-task'})
        assert result.status_code == 200, result.text
    elif stop == 'followup':
        waits.mark_followup_dispatched(r.wait_id, action_digest='actual-action',
                                      receipt_ref='actual-followup-receipt')
    elif stop == 'parent_closed':
        r.commitments.update(r.initial['commitment_id'], status='fulfilled')
    else:
        # Expiry is due even if no context read/tick has refreshed the wait row.
        r.now[0] += 7200
    r.now[0] += 61
    await coverage(r, connected_since=first['connected_since'])
    result = await view(r)
    observed = history(r)
    assert observed['outcomes'][-1]['status'] == 'censored'
    assert observed['outcomes'][-1]['value'] is None
    assert result['status'] == {'cancel':'cancelled', 'expire':'expired',
                                 'followup':'intervened', 'parent_closed':'cancelled'}[stop]
    assert ExpectationEngine(r.store).calibration_report()['resolved_n'] == 0
    await coverage(r, connected_since=first['connected_since'])
    assert history(r) == observed


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['positive', 'negative'])
async def test_later_wait_cancellation_preserves_already_observed_reply_outcome(runtime, outcome):
    r = runtime
    first = await coverage(r)
    await dispatch(r)
    r.now[0] += 20 if outcome == 'positive' else 61
    if outcome == 'positive':
        await reply(r)
    else:
        await coverage(r, connected_since=first['connected_since'])
    original = history(r)
    assert original['outcomes'][-1]['status'] == 'observed'
    r.now[0] += 10
    TemporalFollowups(r.commitments, clock=lambda:r.now[0]).cancel(
        r.wait_id, evidence_ref='later-owner-stop')
    await coverage(r, connected_since=first['connected_since'])
    await view(r)
    assert history(r) == original
    assert ExpectationEngine(r.store).calibration_report()['resolved_n'] == 1
