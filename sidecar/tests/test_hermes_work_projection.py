"""Native cron remains authoritative; shared reads neither recover nor grant."""
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.authority import RequestAuthority
from colony_sidecar.api.routers import executions, host
from colony_sidecar.turns.hermes_work import cron_view


@pytest.fixture
def native(tmp_path, monkeypatch):
    state, home = tmp_path/'state', tmp_path/'profile'
    state.mkdir()
    (home/'cron').mkdir(parents=True)
    monkeypatch.setenv('COLONY_STATE_DIR', str(state))
    monkeypatch.setenv('HERMES_HOME', str(home))
    path = home/'cron'/'executions.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE executions(id TEXT PRIMARY KEY, job_id TEXT, source TEXT, status TEXT, claimed_at TEXT, started_at TEXT, finished_at TEXT, error TEXT)')
        for name, status in [('one','running'), ('two','claimed'), ('three','completed'), ('four','unknown')]:
            conn.execute('INSERT INTO executions VALUES(?,?,?,?,?,?,?,?)', (name, 'job-'+name, 'builtin', status,
                '2026-09-06T12:00:00+00:00', '2026-09-06T12:01:00+00:00',
                '2026-09-06T12:02:00+00:00' if status in {'completed','unknown'} else None, 'private error text'))
    (home/'cron'/'jobs.json').write_text(json.dumps({'jobs': [{'id':'job-one', 'name':'Neutral local check',
        'prompt':'private prompt text', 'script':'private script path'}]}))
    return state, home, path


def test_one_bound_native_snapshot_preserves_state_and_ids_without_private_payload(native, monkeypatch):
    state, home, path = native
    before = path.read_bytes()
    now = datetime(2026,9,6,12,3,tzinfo=timezone.utc).timestamp()
    view = cron_view(limit=1, now=now)
    assert view['available'] and view['complete'] is False
    assert view['source_home_id'] == hashlib.sha256(str(home).encode()).hexdigest()
    assert view['total'] == 2 and view['truncated']
    assert view['items'][0]['execution_id'] == 'two'
    assert view['items'][0]['liveness'] == 'unknown'
    assert view['items'][0]['record_age_seconds'] == 120
    assert view['recent'][0]['status'] == 'completed'
    assert 'private' not in json.dumps(view)
    complete = cron_view(limit=8, now=now)
    assert next(row for row in complete['items'] if row['execution_id']=='one')['name'] == 'Neutral local check'
    assert {row['status'] for row in complete['recent']} == {'unknown','completed'}
    assert before == path.read_bytes()
    # A different explicitly selected profile never sees the first ledger.
    monkeypatch.setenv('HERMES_HOME', str(home.parent/'other'))
    assert cron_view()['reason'] == 'native_ledger_absent'
    assert not (home.parent/'other').exists()
    monkeypatch.delenv('HERMES_HOME')
    assert cron_view()['reason'] == 'profile_not_bound'
    (state/'instance.json').write_text(json.dumps({'version':1,'profile':'local','hermes_home':str(home)}))
    assert cron_view(now=now)['total'] == 2
    monkeypatch.setenv('HERMES_HOME', str(home.parent/'other'))
    assert cron_view()['reason'] == 'invalid_profile_binding'


def test_unavailable_schema_does_not_claim_no_work(native):
    _, _, path = native
    with sqlite3.connect(path) as conn:
        conn.execute('DROP TABLE executions')
    view = cron_view()
    assert view['available'] is False and view['reason'] == 'native_ledger_unavailable'
    assert view['complete'] is False and 'total' not in view


@pytest.mark.asyncio
async def test_only_attested_owner_current_work_and_context_can_read_native_profile(native, monkeypatch):
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID', 'fixture-owner')
    monkeypatch.setattr(host, '_task_queue', None)
    authority = [None]
    app = FastAPI()
    @app.middleware('http')
    async def identity(request, next_call):
        request.state.colony_authority = authority[0]
        return await next_call(request)
    app.include_router(executions.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        for person in ('fixture-guest', 'fixture-owner'):
            authority[0] = RequestAuthority(principal_id='host', credential_id='test',
                scopes=frozenset({'context:read'}), viewer_person_id=person,
                person_ids=frozenset({person}), audiences=frozenset({'viewer'}), authenticated=True)
            response = await client.get('/v1/host/executions', params={'contact_id':person})
            assert response.status_code == 200
            if person == 'fixture-guest':
                assert 'native_cron' not in response.json()
                assert (await client.get('/v1/host/executions', params={'contact_id':'fixture-owner'})).status_code == 403
            else:
                assert response.json()['native_cron']['total'] == 2
                from colony_sidecar.api.schemas.host import ContextAssembleRequest
                body = ContextAssembleRequest(identity={'host_id':'fixture'},
                    context={'contact_id':person,'session_id':'different-session'},
                    incoming_message={'role':'user','content':'What are you doing now?'})
                context = await host.context_assemble(body, SimpleNamespace(state=SimpleNamespace(colony_authority=authority[0])))
                section = next(section for section in context.sections if section.id=='colony-executions')
                assert 'Neutral local check' in section.body and 'unknown' in section.body
                assert 'not a complete process inventory' in section.body
