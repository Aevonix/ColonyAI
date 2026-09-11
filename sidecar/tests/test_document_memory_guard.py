"""Darwin fallback control semantics and real child cleanup under failure."""
import asyncio
import io
import json
import sys
from types import SimpleNamespace

import psutil
import pytest

from apsimo.turns import documents
from apsimo.turns.source_read import read
from test_source_documents import pdf_bytes
from test_source_document_read import original, opened


@pytest.mark.parametrize('platform,flag,as_available,expected', [
    ('darwin', True, False, 'sampled_rss'), ('darwin', False, False, None),
    ('linux', True, False, None), ('linux', False, True, 'address_space')])
def test_child_fallback_requires_darwin_parent_flag_and_retains_cpu_limits(monkeypatch, platform, flag, as_available, expected):
    monkeypatch.setattr(documents.logging, 'disable', lambda _:None)
    calls = []
    def limit(kind, values):
        calls.append((kind, values))
        if kind == 'AS' and not as_available:
            raise ValueError('unsupported AS')
    monkeypatch.setitem(sys.modules, 'resource', SimpleNamespace(RLIMIT_AS='AS', RLIMIT_CPU='CPU',
        RLIMIT_CORE='CORE', setrlimit=limit))
    output = io.BytesIO()
    monkeypatch.setattr(documents, 'sys', SimpleNamespace(platform=platform,
        argv=['documents.py'] + (['--rss-guarded'] if flag else []),
        stdin=SimpleNamespace(buffer=io.BytesIO(b'%PDF-fixture')), stdout=SimpleNamespace(buffer=output)))
    def extract(data):
        assert calls == [('CPU', (10, 10)), ('CORE', (0, 0)),
                         ('AS', (documents.MAX_MEMORY_BYTES, documents.MAX_MEMORY_BYTES))]
        assert data == b'%PDF-fixture'
        return documents.disposition('complete', page_count=1, pages=[{'page':1,'text':'Original page.','status':'text'}])
    monkeypatch.setattr(documents, '_extract', extract)
    documents._child()
    result = json.loads(output.getvalue())
    if expected:
        assert result['memory_control'] == expected
        assert result['hard_limit'] is (expected == 'address_space')
        assert result['memory_sample_interval_ms'] == (25 if expected == 'sampled_rss' else None)
    else:
        assert result['reason'] == 'parser_resource_limits_unavailable' and result['pages'] == []


def test_failed_cpu_limit_never_parses_even_with_guard_flag(monkeypatch):
    monkeypatch.setattr(documents.logging, 'disable', lambda _:None)
    def denied(*args):
        raise OSError('CPU unavailable')
    monkeypatch.setitem(sys.modules, 'resource', SimpleNamespace(RLIMIT_CPU=1, setrlimit=denied))
    output = io.BytesIO()
    monkeypatch.setattr(documents, 'sys', SimpleNamespace(platform='darwin', argv=['documents.py','--rss-guarded'],
        stdin=SimpleNamespace(buffer=io.BytesIO(b'%PDF')), stdout=SimpleNamespace(buffer=output)))
    monkeypatch.setattr(documents, '_extract', lambda _: pytest.fail('CPU guard failed'))
    documents._child()
    assert json.loads(output.getvalue())['reason'] == 'parser_resource_limits_unavailable'


@pytest.fixture
def guarded(monkeypatch):
    spawn = asyncio.create_subprocess_exec
    real_process = psutil.Process
    state = SimpleNamespace(children=[], samples=0, kills=0, fallback_kills=0, kill_failure=False, failure=None, fail_at=1,
                            code='import sys,time; sys.stdin.buffer.read(); time.sleep(30)', sent=[])
    monkeypatch.setattr(documents, 'sys', SimpleNamespace(platform='darwin', executable=sys.executable))
    async def child(*args, **kwargs):
        assert args[-1] == '--rss-guarded'
        process = await spawn(sys.executable, '-I', '-c', state.code, **kwargs)
        process_kill = process.kill
        def fallback_kill():
            state.fallback_kills += 1
            process_kill()
        process.kill = fallback_kill
        communicate = process.communicate
        async def record(data=None):
            state.sent.append(data)
            return await communicate(data)
        process.communicate = record
        state.children.append(process)
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', child)
    class GuardedProcess(real_process):
        def memory_info(self):
            state.samples += 1
            if state.samples >= state.fail_at:
                if state.failure == 'high': return SimpleNamespace(rss=documents.MAX_MEMORY_BYTES+1)
                if state.failure == 'denied': raise psutil.AccessDenied(self.pid)
                if state.failure == 'invalid': return SimpleNamespace(rss=-1)
            return super().memory_info()
        def kill(self):
            state.kills += 1
            if state.kill_failure: raise psutil.AccessDenied(self.pid)
            super().kill()
    monkeypatch.setattr(psutil, 'Process', GuardedProcess)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,fail_at', [('high',1), ('high',3), ('denied',1), ('denied',3), ('invalid',3)])
async def test_monitor_breach_and_failure_discard_output_and_reap(guarded, failure, fail_at):
    guarded.failure = failure
    guarded.fail_at = fail_at
    result = await asyncio.wait_for(documents.extract_document(pdf_bytes()), 3)
    assert result['reason'] == ('parser_memory_limit' if failure == 'high' else 'parser_memory_monitor_failed')
    assert result['pages'] == [] and result['memory_control'] == 'sampled_rss' and result['hard_limit'] is False
    assert guarded.kills == 1 and guarded.children[0].returncode is not None
    assert (any(data is not None for data in guarded.sent)) is (fail_at > 1)


@pytest.mark.asyncio
async def test_failed_monitor_and_psutil_kill_still_discard_and_reap_owned_child(guarded):
    guarded.failure = 'denied'
    guarded.fail_at = 3
    guarded.kill_failure = True
    result = await asyncio.wait_for(documents.extract_document(pdf_bytes()), 3)
    assert result['reason'] == 'parser_memory_monitor_failed' and result['pages'] == []
    assert guarded.kills == guarded.fallback_kills == 1
    assert guarded.children[0].returncode is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['denied', 'invalid', 'gone'])
async def test_only_genuine_child_exit_race_can_suppress_monitor_error(monkeypatch, failure):
    process = SimpleNamespace(returncode=None)
    def memory():
        asyncio.get_running_loop().call_soon(setattr, process, 'returncode', 0)
        if failure == 'denied': raise psutil.AccessDenied(123)
        if failure == 'gone': raise psutil.NoSuchProcess(123)
        return SimpleNamespace(rss=-1)
    target = SimpleNamespace(pid=123, is_running=lambda:True, memory_info=memory)
    if failure == 'gone':
        await documents._watch_rss(process, target)
    else:
        with pytest.raises(documents._MemoryGuardError, match='monitor_failed'):
            await documents._watch_rss(process, target)


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_guarded_timeout_and_cancellation_reap_without_leaked_watcher(guarded, monkeypatch, cancel):
    monkeypatch.setattr(documents, 'MAX_PARSE_SECONDS', 0.1)
    before = asyncio.all_tasks()
    task = asyncio.create_task(documents.extract_document(pdf_bytes()))
    while not guarded.sent:
        await asyncio.sleep(0)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    else:
        result = await task
        assert result['reason'] == 'parser_time_limit' and result['memory_control'] == 'sampled_rss'
    assert guarded.kills == 1 and guarded.children[0].returncode is not None
    assert not [task for task in asyncio.all_tasks() - before if not task.done()]


@pytest.mark.asyncio
@pytest.mark.parametrize('code,reason', [('raise SystemExit(3)', 'parser_process_failed_or_resource_limit'),
                                      ('print("not JSON")', 'invalid_parser_result')])
async def test_guarded_post_start_failures_keep_actual_control_metadata(guarded, code, reason):
    guarded.code = 'import sys; sys.stdin.buffer.read(); ' + code
    result = await documents.extract_document(pdf_bytes())
    assert result['reason'] == reason and result['memory_control'] == 'sampled_rss'
    assert guarded.children[0].returncode is not None


@pytest.mark.asyncio
async def test_missing_monitor_stops_before_spawn(monkeypatch):
    monkeypatch.setattr(documents, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setitem(sys.modules, 'psutil', None)
    assert (await documents.extract_document(pdf_bytes()))['reason'] == 'parser_memory_monitor_unavailable'


@pytest.mark.asyncio
async def test_real_linux_parser_keeps_hard_address_space_control():
    if sys.platform != 'linux': pytest.skip('Linux control qualification')
    result = await documents.extract_document(pdf_bytes())
    assert result['status'] == 'complete' and result['pages'][1]['page'] == 2
    assert result['memory_control'] == 'address_space' and result['hard_limit'] is True


def test_page_read_projects_memory_control_and_revises_when_it_changes(original):
    ledger, ref = original
    with ledger._connect() as conn, conn:
        row = conn.execute('SELECT asset_hash,media_metadata_json FROM source_media').fetchone()
        metadata = json.loads(row['media_metadata_json'])
        metadata['document'].update(documents._memory_metadata('sampled_rss'))
        conn.execute('UPDATE source_media SET media_metadata_json=? WHERE asset_hash=?',
                     (json.dumps(metadata), row['asset_hash']))
    first = opened(original, page=3)
    assert first['document']['memory_control'] == 'sampled_rss' and first['document']['hard_limit'] is False
    with ledger._connect() as conn, conn:
        metadata['document'].update(documents._memory_metadata('address_space'))
        conn.execute('UPDATE source_media SET media_metadata_json=? WHERE asset_hash=?',
                     (json.dumps(metadata), row['asset_hash']))
    with pytest.raises(ValueError, match='restart_at_zero'):
        opened(original, page=3, read_revision=first['read_revision'])
