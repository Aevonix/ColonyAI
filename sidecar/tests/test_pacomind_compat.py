"""Canonical package entry points and explicit environment scope."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest



def child(tmp_path, code, *arguments):
    root = Path(__file__).resolve().parents[2]
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('PACOMIND_', 'PACOMIND_'))}
    env.update(HOME=str(tmp_path), PACOMIND_SKIP_DOTENV='1',
               PYTHONPATH=os.pathsep.join([str(root/'sidecar'), str(root/'hostworker')]))
    return subprocess.run([sys.executable, '-c', code, *arguments], env=env,
        cwd=tmp_path, capture_output=True, text=True, timeout=20)


def test_canonical_imports_share_ledger_cache(tmp_path):
    result = child(tmp_path, """
from pacomind.turns.idempotency import get_turn_idempotency_ledger
import sys
assert get_turn_idempotency_ledger(sys.argv[1]) is get_turn_idempotency_ledger(sys.argv[1])
print('same ledger')
""", str(tmp_path/'state'))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'same ledger'






@pytest.mark.parametrize('module', ['pacomind', 'pacomind.cli'])
def test_module_entrypoints_have_canonical_help_without_contacting_services(tmp_path, module):
    result = child(tmp_path, '''
import runpy,socket,sys
def no_network(*args,**kwargs): raise AssertionError('CLI help cannot contact services')
socket.create_connection=no_network
socket.socket.connect=no_network
module=sys.argv[1];sys.argv=[module,'--help']
runpy.run_module(module,run_name='__main__')
''', module)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'usage: pacomind ' in result.stdout


def test_container_defaults_preserve_selected_state(tmp_path):
    entry = Path(__file__).resolve().parents[1]/'docker-entrypoint.sh'
    for selected in ({}, {'PACOMIND_STATE_DIR': '/current'}):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('PACOMIND_', 'PACOMIND_'))}
        env.update(selected)
        result = subprocess.run(['sh', str(entry), sys.executable, '-c',
            'import os,json;print(json.dumps({k:v for k,v in os.environ.items() if k.endswith("STATE_DIR")}))'],
            env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == (selected or {'PACOMIND_STATE_DIR': '/var/lib/pacomind'})


def test_concurrent_state_reads_never_rewrite_process_environment(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import pacomind
    selected = tmp_path / 'selected'
    monkeypatch.setenv('PACOMIND_STATE_DIR', str(selected))
    monkeypatch.setenv('PACOMIND_STATE_DIR', str(selected))
    before = dict(os.environ)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: pacomind.get_state_dir(), range(128)))
    assert results == [selected] * 128
    assert dict(os.environ) == before
