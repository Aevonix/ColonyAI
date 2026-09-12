"""Built generic task storage, existing-row adoption and exact lifecycle races.

These checks exercise SQLite and source-policy injection without starting a
Hermes executor. Native channel/task wiring is a separate integration boundary.
"""
import pytest

from conftest import run_python


PROBE = r'''
import copy, json, socket, sqlite3, sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier
sys.path.insert(0, sys.argv[1])
def no_network(*a, **kw): raise AssertionError('Task store qualification cannot contact a service')
socket.socket.connect = no_network
socket.create_connection = no_network
from apsimo_hermes.task_handoffs import TaskHandoffs, TaskHandoffError, erase_task_handoffs

path = Path('existing-state.sqlite3')
@contextmanager
def database():
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        with db: yield db
    finally: db.close()

sources = {}
owners = {}
grants = {}
erased = set()
def source(session='call-α', principal='voice-adapter', contact='person', origin=None):
    key = 'source-one' if session == 'call-α' else 'source-' + session
    value = {'version':1, 'principal':principal, 'source_session_id':session,
        'input_refs':[{'source_id':key, 'input_message_hash':'a'*64}],
        'source_refs':[{'source_id':key, 'source_version':'b'*64}],
        'watermark':7, 'contact_id':contact}
    if origin is not None: value['origin'] = origin
    sources[session] = copy.deepcopy(value)
    owners[session] = contact
    grants[session] = True
    return copy.deepcopy(value)

def resolve_owner(value, *, require_task_grant):
    sid = value['source_session_id']
    if value['principal'] != sources[sid]['principal']:
        raise TaskHandoffError('Source principal changed')
    if require_task_grant and not grants[sid]:
        raise TaskHandoffError('Current task grant unavailable')
    return owners[sid]

def resolve_source(value, dependencies=None):
    saved = sources[value['source_session_id']]
    if (value['principal'] != saved['principal'] or value['contact_id'] != saved['contact_id']
            or owners[value['source_session_id']] != saved['contact_id']):
        raise TaskHandoffError('Source owner changed')
    for parent in (saved, dependencies or {}):
        if any(ref['source_id'] in erased for ref in parent.get('input_refs', [])):
            raise TaskHandoffError('Source erased')
    # The transport resolves the enrolled source. Untrusted optional origin is
    # deliberately not copied from the incoming request.
    return copy.deepcopy(saved)

def store(**kwargs): return TaskHandoffs(database, resolve_source, resolve_owner, **kwargs)
def admitted(s=None, request_id='request-α'):
    return store().admit(request_id=request_id, request='Inspect the café calibration notes.',
                         source_input=s or source())
native = {'session_id':'native-session', 'task_id':'native-task', 'turn_id':'native-turn'}
def receipt(row):
    return {**native, 'input_refs':row['source']['input_refs'], 'source_refs':row['source']['source_refs']}
def fails(call, match):
    try: call()
    except TaskHandoffError as error: assert match in str(error), str(error)
    else: raise AssertionError('Expected task boundary failure: ' + match)

def legacy_adoption():
    s = source()
    identity = '0e3a8b610de4235429cf99f6d10f05733b833adbfb435dfecf0576b4c8afa898'
    encoded = json.dumps(s, sort_keys=True, separators=(',', ':'))
    with database() as db:
        db.execute("""CREATE TABLE native_voice_handoffs (
            id TEXT PRIMARY KEY,request_id TEXT NOT NULL UNIQUE,request TEXT NOT NULL,
            source_json TEXT NOT NULL,created REAL NOT NULL,origin_session_id TEXT,
            native_session_id TEXT,native_task_id TEXT,native_turn_id TEXT,
            dependencies_json TEXT,response_json TEXT,notice_json TEXT)""")
        db.execute('INSERT INTO native_voice_handoffs(id,request_id,request,source_json,created) VALUES(?,?,?,?,?)',
            (identity, 'request-α', 'Inspect the café calibration notes.', encoded, 123.0))
    row = admitted({**s, 'watermark':999})
    assert row['id'] == identity and row['created'] == 123.0 and row['source'] == s
    assert store().pending() == [identity]
    with database() as db:
        assert db.execute('SELECT source_json FROM native_voice_handoffs').fetchone()[0] == encoded
        assert {'stop_json','terminal_json'} <= {r['name'] for r in db.execute('PRAGMA table_info(native_voice_handoffs)')}
        assert {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {
            'native_voice_handoffs', 'native_voice_updates'}
    fails(lambda: store().admit(request_id='request-α', request='Rebound content', source_input=s), 'cannot be rebound')
    assert store().count() == 1
    class AdapterError(ValueError): pass
    try: store(error_type=AdapterError).get('invalid')
    except AdapterError: pass
    else: raise AssertionError('The compatibility adapter exception type was lost')

def cross_channel():
    origin = {'platform':'whatsapp','sender_id':'enrolled-handle','session_id':'text-one','turn_id':'turn-one'}
    s = source('text-one', 'native-channel', origin=origin)
    row = admitted({**s, 'origin':{'platform':'pretend-voice'}})
    assert row['source']['origin'] == origin
    assert row['source']['source_session_id'] == 'text-one'
    update = source('text-two','another-channel',origin={**origin,'platform':'sms','session_id':'text-two'})
    first = store().admit_update(row['id'],instruction='Use a checklist.',source_input=update,principal='another-channel')
    replay = store().admit_update(row['id'],instruction='Use a checklist.',
                                  source_input={**update,'watermark':999},principal='another-channel')
    assert first == replay and store().count() == 1
    assert store().get(row['id'])['source']['origin'] == origin
    assert first['source']['origin']['platform'] == 'sms'
    assert store().update_authorized(row['id'], first['id'])
    fails(lambda: store().admit_update(row['id'],instruction='Spoofed caller',source_input=update,principal='native-channel'),
          'authenticated source')
    other = source('guest','guest-channel','other-person')
    fails(lambda: store().admit_update(row['id'],instruction='Foreign instruction',source_input=other,principal='guest-channel'),
          'current task owner')
    grants['text-two'] = False
    fails(lambda: store().update_authorized(row['id'], first['id']), 'grant unavailable')
    grants['text-two'] = True
    owners['text-one'] = 'different-person'
    fails(lambda: store().control(row['id']), 'ownership changed')

def stop_after_erasure():
    row = admitted()
    store().bind(row['id'],native)
    erased.add('source-one')
    grants['call-α'] = False
    fails(lambda: store().resolve(row['id']), 'Source erased')
    fails(lambda: store().request_stop(row['id'],principal='different-adapter'), 'Unknown native task')
    first = store().request_stop(row['id'],principal='voice-adapter')
    assert store().request_stop(row['id'],principal='voice-adapter')['stop'] == first['stop']
    assert store().stop_view(first)['status'] == 'stopping'
    store().observe_stop(row['id'],control_acknowledged=True)
    view = store().stop_view(store().get(row['id']))
    assert view['status'] == 'stopping' and view['stop']['native_control_acknowledged']
    assert view['stop']['native_turn_termination'] is None and view['stop']['process_cleanup'] == 'unobserved'
    store().observe_terminal(row['id'],{**native,'interrupted':True})
    assert store().stop_view(store().get(row['id']))['status'] == 'cancelled'
    owners['call-α'] = 'new-owner'
    fails(lambda: store().control(row['id']), 'ownership changed')

def generation_fencing():
    row = admitted()
    store().bind(row['id'],native)
    store().observe_terminal(row['id'],{**native,'interrupted':True})
    successor = {**native,'turn_id':'successor'}
    store().bind(row['id'],successor)
    assert store().get(row['id'])['terminal'] is None
    store().request_stop(row['id'])
    store().observe_terminal(row['id'],{**native,'interrupted':True})
    assert store().stop_view(store().get(row['id']))['status'] == 'stopping'
    store().observe_terminal(row['id'],{**successor,'interrupted':True})
    view = store().stop_view(store().get(row['id']))
    assert view['status'] == 'cancelled'
    assert view['stop']['native_turn_termination']['turn_id'] == 'successor'
    fails(lambda: store().complete_source(row['id'],receipt(row)), 'stopped')

def reply_ordering():
    for stop_first in (True, False):
        row = admitted(request_id=str(stop_first))
        task = store(reply_effect='retained_for_voice_transport')
        task.bind(row['id'],native)
        task.complete_source(row['id'],receipt(row))
        if stop_first:
            task.request_stop(row['id'])
            fails(lambda: task.retain_reply(row['id'],'Too late'), 'stopped')
            assert task.get(row['id'])['response'] is None
        else:
            response = task.retain_reply(row['id'],'Finished')
            assert task.request_stop(row['id'])['stop'] is None
            assert task.retain_reply(row['id'],'Finished') == response
            assert store().get(row['id'])['response'] == response
            assert response['effect'] == 'retained_for_voice_transport'
            fails(lambda: task.retain_reply(row['id'],'Different result'), 'different reply')

def concurrent_stop_reply():
    row = admitted()
    store().bind(row['id'],native)
    store().complete_source(row['id'],receipt(row))
    ready = Barrier(2)
    def stop():
        ready.wait(); store().request_stop(row['id'])
    def reply():
        ready.wait()
        try: store().retain_reply(row['id'],'Concurrent result')
        except TaskHandoffError as error: assert 'stopped' in str(error)
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(stop),pool.submit(reply)]
        for result in futures: result.result()
    saved = store().get(row['id'])
    assert bool(saved['stop']) != bool(saved['response'])

def update_consumption_and_erasure():
    row = admitted()
    unrelated = admitted(source('independent'),request_id='independent')
    u = source('update','text-channel')
    foreign = source('foreign','foreign-channel','foreign-person')
    foreign.update(input_refs=u['input_refs'],source_refs=u['source_refs'])
    sources['foreign'] = copy.deepcopy(foreign)
    same_refs_foreign = admitted(foreign,request_id='foreign')
    update = store().admit_update(row['id'],instruction='Use the updated source.',source_input=u,principal='text-channel')
    store().bind(row['id'],native)
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda _: store().claim_update_dispatch(row['id'],update['id']),range(2)))
    assert sorted(claims) == [False,True]
    assert store().pending_updates() == []
    assert store().update_view(store().get_update(row['id'],update['id']))['dispatch_unknown']
    assert store().get(row['id'])['dependencies'] is None
    observed = {**native,'update_id':update['id'],'stage':'middleware_visible','request_sha256':'c'*64}
    store().observe_update(row['id'],update['id'],observed)
    store().observe_update(row['id'],update['id'],observed)
    saved = store().get(row['id'])
    assert saved['dependencies']['input_refs'] == [*row['source']['input_refs'],*u['input_refs']]
    view = store().update_view(store().get_update(row['id'],update['id']))
    assert view['middleware_visible'] and not view['native_request_visible']
    assert view['provider_delivery'] == view['behavior_applied'] == 'unobserved'
    store().request_stop(row['id'])
    erased.add(u['input_refs'][0]['source_id'])
    rules = [{'turn_id':u['input_refs'][0]['source_id'],'session_id':'update','message_hashes':[],
              'whole_source':True}]
    erase_task_handoffs(database,'person',rules)
    saved = store().get(row['id'])
    assert saved['request'] == '' and saved['stop'] and saved['native_session_id'] == native['session_id']
    assert store().get_update(row['id'],update['id'])['instruction'] == ''
    assert store().get(unrelated['id'])['request'] and store().get(same_refs_foreign['id'])['request']
    fails(lambda: store().resolve(row['id']), 'Source erased')
    assert store().control(row['id'])['stop']  # Erasure does not erase established stop ownership.

def recovery_receipts():
    row = admitted()
    store().request_stop(row['id'])
    assert row['id'] in store().pending()
    store().observe_stop(row['id'],admission_blocked=True)
    assert row['id'] not in store().pending()
    assert store().stop_view(store().get(row['id']))['stop']['cancellation_basis'] == 'native_admission_blocked'
    running = admitted(request_id='running')
    store().bind(running['id'],native)
    store().request_stop(running['id'])
    store().observe_stop(running['id'],resume_session_id='unrelated-session')
    assert store().stop_view(store().get(running['id']))['status'] == 'stopping'
    store().observe_stop(running['id'],resume_session_id=native['session_id'])
    view = store().stop_view(store().get(running['id']))
    assert view['status'] == 'cancelled'
    assert view['stop']['cancellation_basis'] == 'native_startup_resume_suppressed'
    assert view['stop']['native_turn_termination'] is None and view['stop']['process_cleanup'] == 'unobserved'
    assert running['id'] not in store().pending()

globals()[sys.argv[2]]()
print(json.dumps({'case':sys.argv[2],'passed':True,'execution_started':False}))
'''


@pytest.mark.parametrize('case', [
    'legacy_adoption', 'cross_channel', 'stop_after_erasure', 'generation_fencing',
    'reply_ordering', 'concurrent_stop_reply', 'update_consumption_and_erasure',
    'recovery_receipts',
])
def test_generic_handoff_store(artifacts, tmp_path, case):
    installed = artifacts[3]
    run_python('-c', PROBE, installed, case, cwd=tmp_path)
