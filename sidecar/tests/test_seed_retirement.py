"""Retired built-in descriptions cannot write knowledge during seed or setup."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest

from apsimo import cli, seed, setup
from apsimo.api.routers import host


RETIRED = {
    'memories': 0, 'entities': 0, 'skills': 0, 'insights': 0,
    'errors': [], 'skipped': ['builtin_self_knowledge_retired'],
}


class UntouchedStore:
    def __getattr__(self, name):
        pytest.fail(f'Retired seeding accessed store method {name}')


@pytest.mark.parametrize('force', [False, True])
async def test_seed_returns_stable_disposition_without_accessing_stores(force):
    store = UntouchedStore()
    assert await seed.seed_self_knowledge(
        graph=store, contacts_store=store, goals_store=store,
        world_store=store, skills_registry=store, force=force,
    ) == RETIRED


@pytest.mark.parametrize('force', [False, True])
async def test_seed_http_preserves_shape_without_opening_attached_stores(force, monkeypatch):
    for name in ('_graph', '_world_store', '_skills_registry'):
        monkeypatch.setattr(host, name, UntouchedStore())
    app = FastAPI()
    app.include_router(host.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/v1/host/seed', params={'force': str(force).lower()})
    assert response.status_code == 200
    assert response.json() == RETIRED


@pytest.mark.parametrize('extra', [[], ['--force'], ['--verify']])
def test_seed_cli_reports_retirement(extra, monkeypatch, capsys):
    monkeypatch.setattr(cli, '_load_dotenv', lambda: None)
    monkeypatch.setattr(cli.sys, 'argv', ['colony', 'seed', *extra])
    def no_request(*args, **kwargs):
        pytest.fail('Retired CLI must not ask an older server to seed')
    monkeypatch.setattr(httpx, 'post', no_request)
    monkeypatch.setattr(httpx, 'get', no_request)
    try:
        cli.main()
    except SystemExit as exc:
        assert exc.code == 0
    output = capsys.readouterr().out.lower()
    assert 'retired' in output
    assert 'already seeded' not in output
    assert 'seeding verification complete' not in output


@pytest.mark.parametrize('check', ['world_history', 'seed_request'])
def test_legacy_init_preserves_world_history_and_does_not_request_seed(check, tmp_path, monkeypatch):
    from apsimo.world_model.store import WorldModelStore
    from apsimo.world_model.entities import BaseEntity

    monkeypatch.setattr(os, 'environ', dict(os.environ))
    monkeypatch.setenv('LITELLM_LOCAL_MODEL_COST_MAP', 'True')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.setenv('COLONY_STATE_DIR', str(tmp_path))
    database = tmp_path/'world.db'
    monkeypatch.setenv('WORLD_MODEL_SQLITE_PATH', str(database))
    async def existing_history():
        world = WorldModelStore()
        await world.connect()
        try:
            await world.upsert_entity(BaseEntity(id='retained-project', name='Existing project',
                entity_type='project', properties={'record': 'retained observation'}))
        finally:
            await world.close()
    asyncio.run(existing_history())
    before = database.read_bytes()
    (tmp_path/'.env').write_text('COLONY_EMBED_PROVIDER=skip\nCOLONY_EMBED_MODEL=unused\n')
    monkeypatch.setattr(setup, '_check_python', lambda: (True, '3.12'))
    monkeypatch.setattr(setup, '_check_docker', lambda: (None, 'unavailable'))
    monkeypatch.setattr(setup, '_handle_docker_setup', lambda *args: False)
    monkeypatch.setattr(setup, '_check_neo4j', lambda: (False, 'unavailable'))
    monkeypatch.setattr(setup, '_check_port', lambda *args: False)
    monkeypatch.setattr(setup, '_prompt', lambda prompt, default, *args: '' if 'password' in prompt.lower() else default)
    monkeypatch.setattr(setup, 'run_autonomy_step', lambda *args: {})
    monkeypatch.setattr(setup, 'run_workers_step', lambda *args: None)
    monkeypatch.setattr(setup, '_offer_doctor_run', lambda *args, **kwargs: None)
    monkeypatch.setattr(setup.time, 'sleep', lambda *args: None)
    commands = []
    def run(command, **kwargs):
        assert command[1:4] in (['-m', 'pip', 'install'], ['-m', 'colony_sidecar', 'start'], ['-m', 'colony_sidecar', 'doctor'])
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(setup.subprocess, 'run', run)
    posts = []
    def post(url, **kwargs):
        posts.append(url)
        return httpx.Response(400, json={})
    monkeypatch.setattr(httpx, 'post', post)
    monkeypatch.setattr(httpx, 'get', lambda *args, **kwargs: httpx.Response(200, json={'capabilities': []}))
    args = SimpleNamespace(no_harness=True, non_interactive=True, mcp_harnesses=None,
        agent_harness=None, host_framework=None, contact_name=None, tier=None,
        bind='127.0.0.1', port=7777)
    assert setup.run_init(str(tmp_path), args) == 0
    assert any(command[3] == 'start' for command in commands)
    if check == 'seed_request':
        assert not any(url.endswith('/seed') for url in posts)
    else:
        assert database.read_bytes() == before
