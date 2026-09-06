"""User-manager commands preserve sibling instances and never claim HTTP readiness early."""
import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess

import pytest
import yaml

from colony_sidecar.services.instance import InstanceService, ServiceError


class Manager:
    def __init__(self):
        self.units = {}
        self.calls = []
        self.fail_enable = False

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        output, code = '', 0
        if args[0] == 'systemctl':
            command = args[2]
            if command not in {'show-environment', 'daemon-reload'}:
                row = self.units.setdefault(args[3], {'loaded': False, 'running': False, 'enabled': False})
                if command == 'show':
                    output = '\n'.join(['LoadState=' + ('loaded' if row['loaded'] else 'not-found'),
                        'ActiveState=' + ('active' if row['running'] else 'inactive'),
                        'SubState=' + ('running' if row['running'] else 'dead'),
                        'MainPID=' + ('321' if row['running'] else '0'),
                        'UnitFileState=' + ('enabled' if row['enabled'] else 'disabled')])
                elif command == 'enable':
                    if self.fail_enable:
                        self.fail_enable = False
                        code = 1
                    else:
                        row.update(enabled=True, loaded=True)
                elif command == 'disable':
                    row.update(enabled=False, loaded=False)
                elif command in {'start', 'restart'}:
                    row.update(running=True, loaded=True)
                elif command == 'stop':
                    row['running'] = False
        else:
            command = args[1]
            target = args[2]
            if command == 'bootstrap':
                target += '/' + plistlib.loads(Path(args[3]).read_bytes())['Label']
            if command != 'print' or target.count('/') == 2:
                row = self.units.setdefault(target, {'loaded': False, 'running': False})
                if command == 'print':
                    code = 0 if row['loaded'] else 113
                    output = '  pid = 321\n' if row['running'] else ''
                elif command in {'bootstrap', 'kickstart'}:
                    row.update(loaded=True, running=True)
                elif command == 'bootout':
                    row.update(loaded=False, running=False)
        return subprocess.CompletedProcess(args, code, output, '')


@pytest.fixture
def service_factory(tmp_path, monkeypatch):
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: (_ for _ in ()).throw(ConnectionRefusedError()))
    manager = Manager()
    def make(name='one', platform='linux'):
        service = InstanceService(tmp_path/name, tmp_path/(name+'-hermes'),
            python=str(tmp_path/'venv/bin/python'), home=tmp_path/'user', platform=platform, runner=manager)
        return service
    return make, manager


@pytest.mark.parametrize('platform', ['linux', 'darwin'])
def test_exact_instance_install_start_stop_and_uninstall(service_factory, platform, monkeypatch):
    make, manager = service_factory
    first, other = make('one', platform), make('two', platform)
    for service in [first, other]:
        installed = service.install()
        assert installed['installed'] and not installed['running']
        assert service.definition.stat().st_mode & 0o777 == 0o600
        assert service.log.stat().st_mode & 0o777 == 0o600
        assert b'COLONY_API_KEY' not in service.definition.read_bytes()
        monkeypatch.setattr(service, 'healthy', lambda: True)
        assert service.start()['ready']
    other_bytes = other.definition.read_bytes()
    first.state.joinpath('memory.db').write_bytes(b'private memory')
    assert not first.stop()['running']
    assert other.status()['running']
    assert first.uninstall()['installed'] is False
    assert first.state.joinpath('memory.db').read_bytes() == b'private memory'
    assert first.definition.is_file() and first.log.is_file()
    assert other.definition.read_bytes() == other_bytes and other.status()['running']
    assert not any('sudo' in args or 'linger' in ' '.join(args) for args in manager.calls)


def test_failed_environment_update_restores_previous_definition(service_factory):
    make, manager = service_factory
    service = make()
    service.install()
    before = service.definition.read_bytes()
    service.python = '/different/venv/bin/python'
    manager.fail_enable = True
    with pytest.raises(ServiceError, match='command failed'):
        service.install()
    assert service.definition.read_bytes() == before
    assert service.backup.read_bytes() == before
    assert service.link.resolve() == service.definition
    assert not service.status()['running']


def test_failed_fresh_install_cleans_owned_registration(service_factory):
    make, manager = service_factory
    service = make()
    manager.fail_enable = True
    with pytest.raises(ServiceError):
        service.install()
    assert not service.link.is_symlink() and not service.definition.exists()
    assert not manager.units[service.name]['enabled']


def test_unowned_definition_and_live_environment_change_are_preserved(service_factory, monkeypatch):
    make, manager = service_factory
    service = make()
    service.link.parent.mkdir(parents=True)
    service.link.write_bytes(b'other application')
    with pytest.raises(ServiceError, match='unowned'):
        service.install()
    assert service.link.read_bytes() == b'other application'
    assert not service.definition.exists()
    service.link.unlink()
    service.install()
    monkeypatch.setattr(service, 'healthy', lambda: True)
    service.start()
    before = service.definition.read_bytes()
    service.python = '/new/python'
    with pytest.raises(ServiceError, match='Stop this instance'):
        service.install()
    assert service.definition.read_bytes() == before


def test_running_without_http_readiness_is_not_success(service_factory, monkeypatch):
    make, manager = service_factory
    service = make()
    service.install()
    monkeypatch.setattr(service, 'healthy', lambda: False)
    with pytest.raises(ServiceError, match='HTTP-ready'):
        service.start(timeout=.01)
    assert service.status()['running']


def test_launchd_stop_waits_for_native_bootout_completion(service_factory, monkeypatch):
    make, _ = service_factory
    service = make(platform='darwin')
    service.install()
    monkeypatch.setattr(service, 'healthy', lambda: True)
    service.start()
    real_status = service.status
    observations = []
    def delayed():
        observations.append(1)
        result = real_status()
        if len(observations) <= 2:
            result.update(loaded=True, running=True, pid=321)
        return result
    monkeypatch.setattr(service, 'status', delayed)
    assert not service.stop()['running']
    assert len(observations) == 3


def test_occupied_port_is_not_started_or_stopped(service_factory, monkeypatch):
    make, manager = service_factory
    service = make()
    service.install()
    class Occupant:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: Occupant())
    before = list(manager.calls)
    with pytest.raises(ServiceError, match='occupied'):
        service.start()
    assert not any(args[2] in {'start', 'stop', 'restart'} for args in manager.calls[len(before):])


def test_selected_instance_and_managed_foreground_use_same_binding(tmp_path, monkeypatch):
    from colony_sidecar import cli
    state, home = tmp_path/'instance', tmp_path/'hermes'
    state.mkdir(); home.mkdir()
    (state/'instance.json').write_text(json.dumps({'version': 1, 'profile': 'local', 'hermes_home': str(home)}))
    (home/'config.yaml').write_text(yaml.safe_dump({'plugins': {'colony': {'instance_dir': str(state)}}}))
    (state/'.env').write_text(f'COLONY_STATE_DIR={state}\nCOLONY_INSTALL_PROFILE=local\nCOLONY_API_KEY=private-secret\n')
    monkeypatch.setattr(os, 'environ', {'COLONY_STATE_DIR': str(state), 'COLONY_INSTANCE_SELECTED': '1'})
    service = InstanceService.selected()
    assert service.hermes_home == home and service.state == state
    os.environ['COLONY_INSTANCE_SERVICE'] = service.label
    assert cli._is_service_loaded() is False
    assert b'private-secret' not in service.render()
    (state/'instance.json').unlink()
    with pytest.raises(ValueError, match='no legacy fallback'):
        InstanceService.selected()


def test_systemd_parser_accepts_spaces_specifiers_and_literal_dollar(tmp_path):
    if not shutil.which('systemd-analyze'):
        pytest.skip('systemd parser is unavailable')
    service = InstanceService(tmp_path/'private space%name$dollar', tmp_path/'hermes home',
                              platform='linux', python='/usr/bin/python3', home=tmp_path)
    service.state.mkdir()
    definition = tmp_path/service.name
    definition.write_bytes(service.render())
    env = {**os.environ, 'SYSTEMD_UNIT_PATH': str(tmp_path)+':/usr/lib/systemd/user:/usr/share/systemd/user'}
    result = subprocess.run(['systemd-analyze', '--user', 'verify', str(definition)],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'ExecStart=:"/usr/bin/python3"' in definition.read_text()
    assert 'space%%name$dollar' in definition.read_text()


def test_launchd_preserves_venv_and_literal_paths(service_factory):
    make, _ = service_factory
    service = make('space%name$dollar', 'darwin')
    payload = plistlib.loads(service.render())
    assert payload['ProgramArguments'] == [service.python, '-m', 'colony_sidecar', '--instance', str(service.state), 'start']
    assert payload['EnvironmentVariables']['HERMES_HOME'] == str(service.hermes_home)
    assert payload['RunAtLoad'] and payload['KeepAlive'] and payload['Umask'] == 0o077
