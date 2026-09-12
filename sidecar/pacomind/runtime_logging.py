"""Process-owned rotating logs and measured suppression of routine HTTP polls."""
from contextvars import ContextVar
from copy import copy
from datetime import datetime, timezone
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading
import time

DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_BACKUPS = 4
SLOW_SECONDS = 1.0
MAX_RECORD_CHARS = 65536
POLL_ROUTES = frozenset({
    '/v1/host/health', '/v1/host/transport/ingress/receipts',
    '/v1/host/memory/sources/erasures', '/v1/host/queue/jobs/pending',
    '/v1/host/queue/stats',
})
_started = ContextVar('pacomind_http_log_started', default=None)
_handler = None


class RequestLogTiming:
    """Supply elapsed time to the access record at response-header emission."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        token = _started.set(time.monotonic())
        try:
            return await self.app(scope, receive, send)
        finally:
            _started.reset(token)


class RoutinePollFilter(logging.Filter):
    def filter(self, record):
        args = record.args
        if record.name != 'uvicorn.access' or not isinstance(args, tuple) or len(args) != 5:
            return True
        _, method, path, _, status = args
        started = _started.get()
        return not (method == 'GET' and isinstance(path, str)
            and path.partition('?')[0] in POLL_ROUTES and type(status) is int
            and 200 <= status < 300 and started is not None
            and 0 <= time.monotonic() - started < SLOW_SECONDS)


class RuntimeFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record):
        record = copy(record)
        args = record.args
        if record.name == 'uvicorn.access' and isinstance(args, tuple) and len(args) == 5:
            client, method, path, version, status = args
            if (method == 'GET' and isinstance(path, str)
                    and path.partition('?')[0] == '/v1/host/transport/ingress/receipts'
                    and '?' in path and type(status) is int and 200 <= status < 300):
                record.args = (client, method, path.partition('?')[0]+'?<omitted>', version, status)
        value = super().format(record)
        started = _started.get()
        if record.name == 'uvicorn.access' and started is not None:
            value += f' [elapsed_ms={max(0, (time.monotonic()-started)*1000):.1f}]'
        if len(value) > MAX_RECORD_CHARS:
            value = value[:MAX_RECORD_CHARS] + ' [log record truncated; source data unchanged]'
        return value


class RuntimeFileHandler(RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        os.chmod(self.baseFilename, 0o600)
        return stream


class _LoggedStream(io.TextIOBase):
    """Keep Python stdout/stderr in the same bounded sink as library logging."""
    def __init__(self, name, level):
        self.logger = logging.getLogger(name)
        self.level = level
        self.pending = ''
        self.busy = False
        self.lock = threading.RLock()

    @property
    def encoding(self):
        return 'utf-8'

    def writable(self):
        return True

    def write(self, value):
        with self.lock:
            return self._write(value)

    def _write(self, value):
        if self.busy:
            return len(value)  # Do not recurse if a logging handler itself fails.
        self.busy = True
        try:
            self.pending += str(value)
            while '\n' in self.pending or len(self.pending) >= MAX_RECORD_CHARS:
                end = self.pending.find('\n')
                end = min(end if end >= 0 else MAX_RECORD_CHARS, MAX_RECORD_CHARS)
                line, self.pending = self.pending[:end], self.pending[end:]
                if self.pending.startswith('\n'):
                    self.pending = self.pending[1:]
                if line:
                    self.logger.log(self.level, '%s', line)
        finally:
            self.busy = False
        return len(value)

    def flush(self):
        with self.lock:
            if self.pending and not self.busy:
                self._write('\n')


def runtime_log_directory():
    """Return the selected writer directory, including a deployment override."""
    if _handler is not None:
        return Path(_handler.baseFilename).parent
    from pacomind import get_state_dir
    path = os.environ.get('PACOMIND_LOG_PATH') or os.environ.get('PACOMIND_LOG_PATH')
    return Path(path).expanduser().absolute().parent if path else get_state_dir()/'service'


def configure_runtime_logging(path=None, *, max_bytes=None, backups=None, redirect_stdio=False):
    """Configure one server process, with no extra worker, timer or supervisor.

    Standard rotation bounds each file to the configured size plus one formatted
    record. Python stdio can share the sink; raw native writes to OS descriptors
    remain the service manager's responsibility.
    """
    global _handler
    from pacomind import get_state_dir
    path = Path(path or os.environ.get('PACOMIND_LOG_PATH') or os.environ.get('PACOMIND_LOG_PATH')
                or get_state_dir()/'service/sidecar.log').expanduser().absolute()
    max_bytes = int(max_bytes if max_bytes is not None else os.environ.get('PACOMIND_LOG_MAX_BYTES', DEFAULT_MAX_BYTES))
    backups = int(backups if backups is not None else os.environ.get('PACOMIND_LOG_BACKUPS', DEFAULT_BACKUPS))
    if max_bytes < 1024 or not 1 <= backups <= 100:
        raise ValueError('Runtime logs require a positive size bound and 1..100 retained files')
    if _handler is not None:
        if (Path(_handler.baseFilename), _handler.maxBytes, _handler.backupCount) != (path, max_bytes, backups):
            raise ValueError('Runtime logging is already configured differently')
        if redirect_stdio and not isinstance(sys.stdout, _LoggedStream):
            raise ValueError('Runtime logging must select stdio capture on its first configuration')
        return _handler
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = RuntimeFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding='utf-8')
    os.chmod(path, 0o600)
    handler.setFormatter(RuntimeFormatter('%(asctime)sZ %(levelname)s %(name)s: %(message)s'))
    handler.addFilter(RoutinePollFilter())
    # Uvicorn configures its stream handlers before importing an ASGI app.
    # Replace those streams as well as the root sink, avoiding duplicate output.
    for name in ('', 'uvicorn', 'uvicorn.error', 'uvicorn.access'):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
    _handler = handler
    if redirect_stdio:
        sys.stdout = _LoggedStream('pacomind.stdout', logging.INFO)
        sys.stderr = _LoggedStream('pacomind.stderr', logging.ERROR)
    policy = {'schema': 'PacoMindRuntimeLoggingV1', 'path': str(path),
        'writer_pid': os.getpid(), 'configured_at': datetime.now(timezone.utc).isoformat(),
        'handler': 'logging.handlers.RotatingFileHandler', 'max_bytes': max_bytes,
        'backup_count': backups, 'max_record_chars': MAX_RECORD_CHARS,
        'python_stdio': bool(redirect_stdio), 'routine_poll_routes': sorted(POLL_ROUTES),
        'slow_request_seconds': SLOW_SECONDS,
        'coverage': 'Process-start handler configuration; not a process-liveness attestation.'}
    metadata = path.with_name(path.name+'.runtime.json')
    temporary = metadata.with_name(metadata.name+'.tmp')
    with open(temporary, 'w', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        json.dump(policy, stream, sort_keys=True)
        stream.write('\n')
    os.replace(temporary, metadata)
    logging.getLogger(__name__).info('Runtime logging configured: %d bytes, %d retained files', max_bytes, backups)
    return handler
