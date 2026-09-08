"""Manually installed feeds preserve the real Hermes per-task conversation."""
import importlib.util
import os

import pytest
from conftest import ROOT, run_python


PROBE = r'''
import asyncio, importlib.util, os, sys
from pathlib import Path
import yaml
from gateway.session_context import set_session_vars, clear_session_vars

spec = importlib.util.spec_from_file_location('fixture_feeds', sys.argv[1])
feeds = importlib.util.module_from_spec(spec); spec.loader.exec_module(feeds)
directory = Path(sys.argv[2])
feeds._cfg = lambda: {'specs_dir': str(directory)}
calls = []
feeds._cli = lambda *args, **kwargs: (calls.append(args) or (0, 'fixture only'))
os.environ['HERMES_SESSION_PLATFORM'] = 'stale-process-channel'
os.environ['HERMES_SESSION_CHAT_ID'] = 'stale-process-chat'

async def main():
    barrier = asyncio.Barrier(2)
    async def create(name, channel, chat):
        tokens = set_session_vars(platform=channel, chat_id=chat, session_id=name)
        try:
            await barrier.wait()
            assert feeds._session_deliver_target({}) == f'{channel}:{chat}'
            result = feeds._tool_feed_create({'spec_yaml': f'name: {name}\ntopic: neutral fixture'})
            assert 'created' in result, result
            await asyncio.sleep(0)
            assert feeds._session_deliver_target({}) == f'{channel}:{chat}'
        finally:
            clear_session_vars(tokens)
        assert feeds._session_deliver_target({}) == 'origin'
    await asyncio.gather(create('alpha', 'sms', 'fixture-alpha'),
                         create('beta', 'whatsapp', 'fixture-beta'))
    for name, target in [('alpha', 'sms:fixture-alpha'), ('beta', 'whatsapp:fixture-beta')]:
        assert yaml.safe_load((directory / (name + '.yaml')).read_text())['destination'] == {
            'kind': 'deliver', 'deliver': target}
    # An explicit destination never reads a default origin.
    feeds._session_deliver_target = lambda context: (_ for _ in ()).throw(AssertionError('must not infer destination'))
    result = feeds._tool_feed_create({'spec_yaml': 'name: explicit\ndestination:\n  kind: file\n  path: fixture.md\n'})
    assert 'created' in result, result
    assert yaml.safe_load((directory / 'explicit.yaml').read_text())['destination'] == {
        'kind': 'file', 'path': 'fixture.md'}
    assert [args[0] for args in calls] == ['validate', 'create'] * 3

asyncio.run(main())
print('native feeds: concurrent origins, cleared context and explicit destination passed; no dispatch')
'''


def test_native_concurrent_feed_origins_and_explicit_destination(tmp_path):
    if importlib.util.find_spec("hermes_cli") is None:
        pytest.skip("Install qualified Hermes to exercise native session context")
    env = {key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ}
    env.update(HOME=str(tmp_path), HERMES_HOME=str(tmp_path / "hermes"),
               PYTHON_DOTENV_DISABLED="1")
    run_python("-c", PROBE, ROOT / "plugins/feeds-manage/__init__.py",
               tmp_path / "specs", cwd=tmp_path, env=env)
