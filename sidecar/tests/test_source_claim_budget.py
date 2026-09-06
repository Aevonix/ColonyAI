"""Configured extraction deadlines stay inside an owned, renewable job lease."""
import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from colony_sidecar.beliefs.source_claims import extraction_timeout_seconds, extract_claims
from colony_sidecar.beliefs.source_projection import SourceClaimProjection
from colony_sidecar.turns.idempotency import TurnIdempotencyLedger
from test_function_routing import config, endpoint, router
from test_source_claim_projection import claim, Model


@pytest.mark.asyncio
async def test_actual_role_request_uses_configured_outer_deadline(monkeypatch):
    text = 'My office is in Alder.'
    observed = []
    original_wait_for = asyncio.wait_for

    async def inspect_wait_for(awaitable, timeout):
        if getattr(getattr(awaitable, 'cr_code', None), 'co_name', '') == 'complete':
            observed.append(timeout)
        return await original_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, 'wait_for', inspect_wait_for)
    with endpoint(content=json.dumps([claim(text, 'Alder')])) as (url, requests):
        r = router(config(url, url, deadlineSeconds=80))
        rows, _ = await extract_claims(r, {'occurred_at': None}, {'role': 'user', 'content': text}, [])
        assert len(rows) == len(requests) == 1
        assert observed == [85]
        assert extraction_timeout_seconds(r) == 85
        r.configure(config(url, url, deadlineSeconds=120))
        assert extraction_timeout_seconds(r) == 125


@pytest.mark.asyncio
async def test_long_multi_message_job_renews_after_role_reload(tmp_path, monkeypatch):
    from colony_sidecar.beliefs import source_projection as module
    clock = [1000.0]
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: clock[0]))
    ledger = TurnIdempotencyLedger(tmp_path / 'turns.db')
    first, second = 'My office is in Alder.', 'My office is in Birch.'
    ledger.record_source('neutral', contact_id='person', session_id='sms', messages=[
        {'role': 'user', 'content': first}, {'role': 'user', 'content': second}])
    projection, competitor = SourceClaimProjection(ledger), SourceClaimProjection(ledger)
    r = router(config('http://127.0.0.1:1/v1', 'http://127.0.0.1:1/v1', deadlineSeconds=80))
    leases, waits = [], []
    original_wait_for = asyncio.wait_for

    async def inspect_wait_for(awaitable, timeout):
        waits.append(timeout)
        return await original_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, 'wait_for', inspect_wait_for)

    class SlowModel(Model):
        supports_function_routing = True
        function_deadline_seconds = r.function_deadline_seconds

        async def complete(self, messages, **kwargs):
            with sqlite3.connect(ledger.db_path) as conn:
                leases.append(conn.execute('SELECT lease_until FROM source_claim_jobs').fetchone()[0])
            # A simulated 61-second request outlives the old fixed lease. The
            # real SQLite competitor must not steal either active message.
            clock[0] += 61
            assert competitor.claim_job() is None
            r.configure(config('http://127.0.0.1:1/v1', 'http://127.0.0.1:1/v1', deadlineSeconds=120))
            return await super().complete(messages, **kwargs)

    model = SlowModel({first: claim(first, 'Alder'), second: claim(second, 'Birch')})
    assert await projection.process_one(model)
    assert waits == [85, 125]
    assert leases == [1115, 1216]
    assert projection.status('person')[0]['status'] == 'complete'
    assert projection.status('person')[0]['claim_count'] == 2


def test_old_lease_cannot_be_renewed_after_reclaim_or_forget(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path / 'turns.db')
    projection = SourceClaimProjection(ledger)
    ledger.record_source('neutral', contact_id='person', session_id='sms', messages=[
        {'role': 'user', 'content': 'My office is in Alder.'}])
    old = projection.claim_job()
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute('UPDATE source_claim_jobs SET lease_until=0')
    new = projection.claim_job()
    assert not projection.renew_job(old, 85)
    assert projection.renew_job(new, 85)
    ledger.erase_sources(contact_id='person', turn_ids=['neutral'])
    assert not projection.renew_job(new, 85)


def test_legacy_and_old_function_adapters_keep_their_existing_bounds():
    assert extraction_timeout_seconds(SimpleNamespace()) == 20
    assert extraction_timeout_seconds(SimpleNamespace(supports_function_routing=True)) == 40
