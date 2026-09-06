"""User-manager autostart for one validated private instance, without a supervisor."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import socket
import subprocess
import sys
import tempfile
import time


class ServiceError(RuntimeError):
    pass


def _private_write(path, content):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.service-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _systemd_quote(value):
    value = str(value)
    if any(c in value for c in '\n\r\0'):
        raise ServiceError('Service paths must not contain line breaks or NUL')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def _systemd_path(value):
    # These scalar settings take the whole path, unlike ExecStart's argv parser.
    value = str(value)
    if any(c in value for c in '\n\r\0') or value.rstrip() != value or value.endswith('\\'):
        raise ServiceError('Unsupported line ending in service path')
    return value.replace('%', '%%')


class InstanceService:
    def __init__(self, state, hermes_home, *, python=None, platform=None, home=None, runner=None):
        self.state = Path(state).resolve()
        self.hermes_home = Path(hermes_home).resolve()
        # Resolving a venv's python symlink would select the system interpreter.
        self.python = os.path.abspath(python or sys.executable)
        self.platform = platform or sys.platform
        self.home = Path(home or Path.home())
        suffix = hashlib.sha256(os.fsencode(self.state)).hexdigest()[:20]
        self.label = 'ai.colony.instance.' + suffix
        if self.platform == 'darwin':
            self.name = self.label + '.plist'
            self.link = self.home / 'Library/LaunchAgents' / self.name
            self.target = f'gui/{os.getuid()}/{self.label}'
        elif self.platform.startswith('linux'):
            self.name = 'colony-' + suffix + '.service'
            config = Path(os.environ.get('XDG_CONFIG_HOME') or self.home / '.config')
            self.link = config / 'systemd/user' / self.name
            self.target = self.name
        else:
            raise ServiceError('Instance autostart supports Linux systemd user services and macOS launchd')
        self.definition = self.state / 'service' / self.name
        self.backup = self.definition.with_name(self.name + '.previous')
        self.log = self.state / 'service' / 'sidecar.log'
        self.runner = runner or subprocess.run

    @classmethod
    def selected(cls):
        from colony_sidecar.util.instance import load_environment
        # A legacy state-dir environment is never sufficient to own a service.
        if not os.environ.get('COLONY_STATE_DIR'):
            load_environment()
        if not os.environ.get('COLONY_STATE_DIR'):
            raise ServiceError('Select a configured private instance with --instance')
        os.environ['COLONY_INSTANCE_SELECTED'] = '1'
        load_environment()
        state = Path(os.environ['COLONY_STATE_DIR'])
        manifest = json.loads((state / 'instance.json').read_text())
        return cls(state, manifest['hermes_home'])

    def _run(self, *args, check=True):
        try:
            result = self.runner(list(args), capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ServiceError(f'User service manager unavailable ({type(exc).__name__})') from None
        if check and result.returncode:
            raise ServiceError(f'User service command failed: {args[0]} {args[1]} (exit {result.returncode})')
        return result

    def _manager_ready(self):
        if self.platform == 'darwin':
            self._run('launchctl', 'print', f'gui/{os.getuid()}')
        else:
            self._run('systemctl', '--user', 'show-environment')

    def _owned(self):
        if not self.link.exists() and not self.link.is_symlink():
            return False
        if not self.link.is_symlink() or self.link.resolve() != self.definition:
            raise ServiceError(f'Refusing an unowned service definition at {self.link}')
        if not self.definition.is_file():
            raise ServiceError('Private service definition is missing; reinstall it before managing the service')
        return True

    def render(self):
        arguments = [self.python, '-m', 'colony_sidecar', '--instance', str(self.state), 'start']
        environment = {'HERMES_HOME': str(self.hermes_home), 'COLONY_INSTANCE_SERVICE': self.label,
                       'PYTHONUNBUFFERED': '1'}
        if self.platform == 'darwin':
            return plistlib.dumps({'Label': self.label, 'ProgramArguments': arguments,
                'WorkingDirectory': str(self.state), 'EnvironmentVariables': environment,
                'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 5,
                'ExitTimeOut': 20, 'Umask': 0o077,
                'StandardOutPath': str(self.log), 'StandardErrorPath': str(self.log)}, sort_keys=True)
        quote = _systemd_quote
        return ('[Unit]\nDescription=Colony private instance ' + self.label + '\n\n[Service]\nType=exec\n'
                'WorkingDirectory=' + _systemd_path(self.state) + '\n'
                # ':' disables dollar-variable substitution; %% escapes specifiers.
                'ExecStart=:' + ' '.join(quote(arg) for arg in arguments) + '\n'
                'Environment=' + ' '.join(quote(key + '=' + value) for key, value in environment.items()) + '\n'
                'Restart=always\nRestartSec=5\nTimeoutStopSec=20\nUMask=0077\n'
                'StandardOutput=append:' + _systemd_path(self.log) + '\n'
                'StandardError=append:' + _systemd_path(self.log) + '\n\n[Install]\nWantedBy=default.target\n').encode()

    def status(self):
        owned = self._owned()
        self._manager_ready()
        result = {'instance': str(self.state), 'manager': 'launchd' if self.platform == 'darwin' else 'systemd-user',
                  'label': self.label, 'definition': str(self.definition), 'installed': owned,
                  'loaded': False, 'running': False, 'pid': None, 'autostart_scope': 'user-session'}
        if self.platform == 'darwin':
            response = self._run('launchctl', 'print', self.target, check=False)
            result['loaded'] = response.returncode == 0
            pid = re.search(r'^\s*pid = ([0-9]+)\s*$', response.stdout, re.MULTILINE)
            if pid:
                result['pid'], result['running'] = int(pid[1]), True
        else:
            response = self._run('systemctl', '--user', 'show', self.name,
                '--property=LoadState,ActiveState,SubState,MainPID,UnitFileState', '--no-pager', check=False)
            fields = dict(line.split('=', 1) for line in response.stdout.splitlines() if '=' in line)
            result.update(state=fields.get('ActiveState', 'unknown'), substate=fields.get('SubState', 'unknown'),
                          enabled=fields.get('UnitFileState') == 'enabled')
            result['loaded'] = fields.get('LoadState') == 'loaded'
            pid = fields.get('MainPID', '0')
            result['pid'] = int(pid) if pid.isdigit() and int(pid) > 0 else None
            result['running'] = bool(result['pid'] and fields.get('ActiveState') in {'active', 'activating'})
        return result

    def install(self):
        content = self.render()  # Validate every path before writing anything.
        self._manager_ready()
        owned = self._owned()
        previous = self.definition.read_bytes() if self.definition.is_file() else None
        if previous != content and owned:
            status = self.status()
            if status['running'] or (self.platform == 'darwin' and status['loaded']):
                raise ServiceError('Stop this instance service before changing its Python environment')
        try:
            if previous is not None and previous != content:
                _private_write(self.backup, previous)
            _private_write(self.definition, content)
            self.link.parent.mkdir(parents=True, exist_ok=True)
            if not owned:
                self.link.symlink_to(self.definition)
            if not self.log.exists():
                _private_write(self.log, b'')
            if self.platform == 'darwin':
                self._run('launchctl', 'enable', self.target)
            else:
                self._run('systemctl', '--user', 'daemon-reload')
                self._run('systemctl', '--user', 'enable', self.name)
        except (ServiceError, OSError):
            # Preserve the previous definition; no instance was started here.
            if previous is not None:
                _private_write(self.definition, previous)
            if not owned and self.link.is_symlink() and self.link.resolve() == self.definition:
                if self.platform != 'darwin':
                    self._run('systemctl', '--user', 'disable', self.name, check=False)
                # A failed enable may have created its exact wanted-by link.
            if not owned and self.link.is_symlink() and self.link.resolve() == self.definition:
                self.link.unlink()
            if previous is None:
                self.definition.unlink(missing_ok=True)
            if self.platform != 'darwin':
                self._run('systemctl', '--user', 'daemon-reload', check=False)
            raise
        return self.status()

    def _require_installed(self):
        if not self._owned():
            raise ServiceError('Service is not installed for this instance; run colony service install')
        self._manager_ready()

    def healthy(self):
        import httpx
        host = os.environ.get('COLONY_SIDECAR_HOST', '127.0.0.1')
        host = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(host, host)
        if ':' in host:
            host = '[' + host + ']'
        key = os.environ.get('COLONY_CLIENT_API_KEY') or os.environ.get('COLONY_API_KEY', '')
        try:
            response = httpx.get(f'http://{host}:{os.environ.get("COLONY_SIDECAR_PORT", "7777")}/v1/host/health',
                headers={'Authorization': 'Bearer ' + key}, timeout=1, trust_env=False, follow_redirects=False)
            return response.status_code == 200 and response.json().get('status') == 'ok'
        except (httpx.HTTPError, ValueError):
            return False

    def start(self, *, restart=False, timeout=30):
        self._require_installed()
        status = self.status()
        if not status['running']:
            host = os.environ.get('COLONY_SIDECAR_HOST', '127.0.0.1')
            host = {'0.0.0.0': '127.0.0.1', '::': '::1'}.get(host, host)
            try:
                with socket.create_connection((host, int(os.environ.get('COLONY_SIDECAR_PORT', '7777'))), timeout=.2):
                    raise ServiceError('Instance port is already occupied; stop its current process before service start')
            except OSError:
                pass
        if self.platform == 'darwin':
            if status['loaded']:
                if restart:
                    self._run('launchctl', 'kickstart', '-k', self.target)
            else:
                self._run('launchctl', 'bootstrap', f'gui/{os.getuid()}', str(self.link))
        else:
            self._run('systemctl', '--user', 'restart' if restart else 'start', self.name)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.status()
            if result['running'] and self.healthy():
                return {**result, 'ready': True}
            time.sleep(.2)
        raise ServiceError(f'Service did not become HTTP-ready; inspect {self.log}. It remains installed for recovery.')

    def stop(self):
        self._require_installed()
        if self.platform == 'darwin':
            if self.status()['loaded']:
                self._run('launchctl', 'bootout', self.target)
        else:
            self._run('systemctl', '--user', 'stop', self.name)
        # launchctl bootout can return while its process is still shutting down.
        # Do not remove the definition or report a stopped service prematurely.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = self.status()
            if not result['running'] and (self.platform != 'darwin' or not result['loaded']):
                return result
            time.sleep(.1)
        raise ServiceError('User manager has not finished stopping this instance; its registration is retained')

    def uninstall(self):
        if not self._owned():
            return self.status()
        self.stop()
        if self.platform != 'darwin':
            self._run('systemctl', '--user', 'disable', self.name)
        # systemctl disable may remove this exact link itself.
        if self.link.is_symlink() and self.link.resolve() == self.definition:
            self.link.unlink()
        if self.platform != 'darwin':
            self._run('systemctl', '--user', 'daemon-reload')
        return self.status()


def manage(action):
    service = InstanceService.selected()
    if action == 'restart':
        result = service.start(restart=True)
    else:
        result = getattr(service, action)()
    if action == 'status':
        result['ready'] = bool(result['running'] and service.healthy())
    print(json.dumps(result, sort_keys=True))
