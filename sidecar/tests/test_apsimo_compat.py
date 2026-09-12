"""Canonical package entry points and explicit environment scope."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from apsimo.environment import normalize_environment


def child(tmp_path, code, *arguments):
    root = Path(__file__).resolve().parents[2]
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('COLONY_', 'APSIMO_'))}
    env.update(HOME=str(tmp_path), COLONY_SKIP_DOTENV='1',
               PYTHONPATH=os.pathsep.join([str(root/'sidecar'), str(root/'hostworker')]))
    return subprocess.run([sys.executable, '-c', code, *arguments], env=env,
        cwd=tmp_path, capture_output=True, text=True, timeout=20)


def test_canonical_imports_share_ledger_cache(tmp_path):
    result = child(tmp_path, """
from apsimo.turns.idempotency import get_turn_idempotency_ledger
import sys
assert get_turn_idempotency_ledger(sys.argv[1]) is get_turn_idempotency_ledger(sys.argv[1])
print('same ledger')
""", str(tmp_path/'state'))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'same ledger'


def test_environment_copy_and_conflicts_do_not_expose_values():
    original = {'APSIMO_STATE_DIR': '/selected', 'APSIMO_API_KEY': 'private-a',
                'COLONY_TIMEOUT': '12', 'UNRELATED': 'keep'}
    result = normalize_environment(original)
    assert result == {**original, 'COLONY_STATE_DIR': '/selected', 'COLONY_API_KEY': 'private-a'}
    assert 'COLONY_STATE_DIR' not in original
    with pytest.raises(ValueError) as caught:
        normalize_environment({'APSIMO_API_KEY': 'private-a', 'COLONY_API_KEY': 'private-b'})
    assert str(caught.value) == 'Conflicting environment names: APSIMO_API_KEY and COLONY_API_KEY'


def test_generated_aliases_follow_reselection_and_preserve_explicit_changes(tmp_path):
    result = child(tmp_path, '''
import os
from apsimo.environment import apply_environment_aliases,clear_environment_aliases
os.environ['APSIMO_STATE_DIR']='first'
apply_environment_aliases()
assert os.environ['COLONY_STATE_DIR']=='first'
os.environ['APSIMO_STATE_DIR']='second'
apply_environment_aliases()
assert os.environ['COLONY_STATE_DIR']=='second'
del os.environ['APSIMO_STATE_DIR']
apply_environment_aliases()
assert 'COLONY_STATE_DIR' not in os.environ
os.environ['APSIMO_API_KEY']='original'
apply_environment_aliases()
os.environ['COLONY_API_KEY']='explicit-change'
clear_environment_aliases()
assert os.environ['COLONY_API_KEY']=='explicit-change'
try:
    apply_environment_aliases()
except ValueError as error:
    assert 'original' not in str(error) and 'explicit-change' not in str(error)
else:
    raise AssertionError('Explicit conflicts must not select a credential silently')
print('reselection preserved')
''')
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('module', ['apsimo', 'apsimo.cli'])
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
    assert 'usage: apsimo ' in result.stdout


def test_container_defaults_do_not_override_either_selected_state_name(tmp_path):
    entry = Path(__file__).resolve().parents[1]/'docker-entrypoint.sh'
    for selected in ({}, {'COLONY_STATE_DIR': '/legacy'}, {'APSIMO_STATE_DIR': '/current'}):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('COLONY_', 'APSIMO_'))}
        env.update(selected)
        result = subprocess.run(['sh', str(entry), sys.executable, '-c',
            'import os,json;print(json.dumps({k:v for k,v in os.environ.items() if k.endswith("STATE_DIR")}))'],
            env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == (selected or {'APSIMO_STATE_DIR': '/var/lib/colony'})


def test_concurrent_state_reads_never_rewrite_process_environment(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import apsimo
    selected = tmp_path / 'selected'
    monkeypatch.setenv('APSIMO_STATE_DIR', str(selected))
    monkeypatch.setenv('COLONY_STATE_DIR', str(selected))
    def unexpected_mutation():
        raise AssertionError('State reads must not normalize process configuration')
    monkeypatch.setattr(apsimo, 'apply_environment_aliases', unexpected_mutation)
    before = dict(os.environ)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: apsimo.get_state_dir(), range(128)))
    assert results == [selected] * 128
    assert dict(os.environ) == before
