"""Real native owner conversations inspect/control a disposable judgment ledger."""
import asyncio
import importlib.util
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from conftest import ROOT, run_python


PROBE = r'''
import json, os, sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
sys.path.insert(0, sys.argv[1])
home = Path(os.environ['HERMES_HOME']); home.mkdir(mode=0o700)
Path(os.environ['HERMES_BUNDLED_PLUGINS']).mkdir()
(home / 'config.yaml').write_text(json.dumps({'plugins': {'enabled': ['colony'], 'colony': {
    'url': sys.argv[2], 'owner_contact_id': 'owner', 'attested_system_platforms': ['cli', 'cron'],
    'turn_outbox_path': str(home / 'turns.sqlite3')}}}))
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.lifecycle import invoke_hook
from model_tools import handle_function_call
manager = get_plugin_manager(); manager.discover_and_load()
assert manager._plugins['colony'].enabled, manager._plugins['colony'].error
assert 'colony_judgments' in manager._plugins['colony'].tools_registered
import colony_hermes
schema = next(s for s in colony_hermes._TOOL_SCHEMAS if s['name'] == 'colony_judgments')
assert set(schema['parameters']['properties']) == {'operation', 'judgment_id', 'source_id'}
from run_agent import AIAgent
def response(content='', args=None, ordinal=0):
    calls = None if args is None else [NS(id='c'+str(ordinal), type='function',
        function=NS(name='colony_judgments', arguments=json.dumps(args)))]
    return NS(choices=[NS(message=NS(content=content, tool_calls=calls),
        finish_reason='tool_calls' if calls else 'stop')], model='controlled/model', usage=None)
def conversation(text, calls):
    with patch('run_agent.OpenAI'), patch('run_agent.get_tool_definitions', return_value=[{'type':'function','function':schema}]), patch('run_agent.check_toolset_requirements', return_value={}):
        agent = AIAgent(api_key='fixture', base_url='http://127.0.0.1:1/v1', provider='openai', model='controlled/model',
            max_iterations=5, quiet_mode=True, skip_context_files=True, skip_memory=True, platform='sms')
    agent._user_id = 'owner'; agent._cached_system_prompt = 'Controlled native tool exercise.'
    agent._use_prompt_caching = False; agent.compression_enabled = False; agent.save_trajectories = False
    agent.client = MagicMock()
    agent.client.chat.completions.create.side_effect = [response(args=args, ordinal=i) for i,args in enumerate(calls)] + [response('Recorded.')]
    result = agent.run_conversation(text)
    agent.close()
    assert result['final_response'] == 'Recorded.', result
    return [json.loads(m['content']) for m in result['messages'] if m.get('role') == 'tool']
first = conversation('Withdraw this checkpoint judgment until I request reconsideration.',
    [{'operation':'inspect'}, {'operation':'withdraw','judgment_id':1}])
assert first[0]['judgments'][0]['id'] == 1
assert first[1]['accepted'] and first[1]['judgment']['status'] == 'withdrawn', first
withdrawn = first[1]['judgment']['revision_id']
second = conversation('Reconsider the withdrawn checkpoint view using the retained observation.',
    [{'operation':'reconsider','judgment_id':withdrawn,'source_id':'evidence-b'}])
assert second[0]['accepted'] and second[0]['judgment']['status'] == 'reconsidering', second
for platform, sender in [('sms','guest'), ('cron','')]:
    turn = 'denied-'+platform
    invoke_hook('pre_llm_call', session_id=turn, task_id=turn, turn_id=turn,
                platform=platform, sender_id=sender, user_message='Withdraw it')
    denied = json.loads(handle_function_call('colony_judgments', {'operation':'withdraw','judgment_id':second[0]['judgment']['revision_id']},
        session_id=turn, task_id=turn, turn_id=turn))
    assert 'error' in denied, denied
print(json.dumps({'native_owner_controls':True,'guest_and_cron_denied':True}))
'''


def test_native_owner_judgment_control(artifacts, tmp_path, monkeypatch):
    if importlib.util.find_spec('hermes_cli') is None:
        pytest.skip('Install qualified Hermes to exercise native judgment controls')
    monkeypatch.syspath_prepend(str(ROOT / 'sidecar'))
    monkeypatch.setenv('COLONY_OWNER_CONTACT_ID', 'owner')
    from colony_sidecar.self_model.judgments import SelfJudgments
    from colony_sidecar.turns import TurnIdempotencyLedger
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    state = SelfJudgments(ledger, owner_id='owner')
    for turn, text in [('evidence-a', 'A long local export recovered useful work from phase checkpoints.'),
                       ('evidence-b', 'Short local exports showed larger checkpoint overhead.')]:
        ledger.record_source(turn, contact_id='owner', session_id=turn, messages=[{'role':'user','content':text}])
    class Processor:
        supports_function_routing = True
        def function_deadline_seconds(self, **kwargs): return 60
        async def complete(self, **kwargs):
            payload = json.loads(kwargs['messages'][-1]['content'])
            prior = payload['previous_judgments']
            return SimpleNamespace(content=json.dumps({'action':'revise','topic':'checkpointing',
                'supersedes':prior[0]['id'] if prior else None, 'stance':'I favor phase checkpoints for long work.',
                'reason':'The reported recovery supports their use when overhead is modest.',
                'certainty':'tentative','support':[payload['evidence'][0]['handle']],'contrary':[]}),raw=None)
    asyncio.run(state.process_one(Processor()))
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, value):
            raw = json.dumps(value).encode()
            self.send_response(200); self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def do_GET(self):
            if self.path.startswith('/v1/host/contacts/resolve?'):
                self.reply({'contact_id':'guest' if 'address=guest' in self.path else 'owner'})
            else:
                assert self.path == '/v1/host/self'
                self.reply({'perspective':{'judgments':state.revisions(),'judgment_history':state.revisions(history=True),
                                         'judgment_processing':state.processing()}})
        def do_POST(self):
            assert self.path == '/v1/host/learning/correction'
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert body['context']['contact_id'] == 'owner' and body['context']['turn_id']
            assert body['correction'] in {'Withdraw this checkpoint judgment until I request reconsideration.',
                                        'Reconsider the withdrawn checkpoint view using the retained observation.'}
            calls.append(body)
            result = state.correct(body['judgment_id'], action=body['judgment_action'], correction_id=body['correction_id'],
                                   reason=body['correction'], source_id=body['source_id'])
            self.reply({'accepted':True,'judgment':result,'authority_changed':False})
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    _, _, _, installed = artifacts
    env = {key:os.environ[key] for key in ('PATH','HOME','TMPDIR','LANG') if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path/'profile'), HERMES_BUNDLED_PLUGINS=str(tmp_path/'bundled'),
        COLONY_GENERAL_PLUGIN_ACTIVE='1', COLONY_MEMORY_WORKER_TOOLS='0', COLONY_MEMORY_TURN_WRITER='disabled')
    try:
        result = run_python('-I','-c',PROBE,installed,'http://127.0.0.1:'+str(server.server_port),cwd=tmp_path,env=env)
        assert json.loads(result.stdout.splitlines()[-1])['native_owner_controls']
        assert len(calls) == 2 and state.revisions() == []
        asyncio.run(state.process_one(Processor()))
        assert state.revisions()[0]['supersedes'] == 3
        with ledger._connect() as db:
            assert db.execute('SELECT count(*) FROM turn_sources').fetchone()[0] == 2
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
