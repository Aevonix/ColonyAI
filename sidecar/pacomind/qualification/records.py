"""Small versioned records. Inputs and independent oracles stay separate."""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

SCHEMA = 1
BOUNDARIES = {'role_completion', 'cognition_consumer', 'native_hermes', 'retrieval', 'speech', 'media_consumer'}


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + '\n').encode()


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def read(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('Invalid qualification record')
    return json.loads(path.read_bytes())


def write_once(path, value):
    """Publish complete bytes without replacing an earlier outcome."""
    publish(path, encode(value))


def publish(path, raw):
    """Atomically publish a new private record or derived text report."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.record-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


@dataclass(frozen=True)
class CaseSpec:
    id: str
    version: str
    role: str
    boundary: str
    consumer: str
    evaluator: str
    inputs: dict
    oracle: dict
    required_capabilities: tuple[str, ...] = ()
    timeout_seconds: float = 60
    max_output_bytes: int = 262144
    provenance: str = 'public'
    target_tasks: tuple[str, ...] = ()

    def record(self):
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}', self.id):
            raise ValueError('Invalid case ID')
        if self.boundary not in BOUNDARIES or self.provenance not in {'public', 'private'}:
            raise ValueError('Invalid case boundary/provenance')
        if not self.version or not self.role or not self.consumer or not self.evaluator:
            raise ValueError('Case identity is incomplete')
        if isinstance(self.timeout_seconds, bool) or not .01 <= self.timeout_seconds <= 600:
            raise ValueError('Case deadline must be .01..600 seconds')
        if not 1 <= self.max_output_bytes <= 1024 * 1024:
            raise ValueError('Case output bound must be 1..1048576 bytes')
        if not isinstance(self.inputs, dict) or not isinstance(self.oracle, dict):
            raise ValueError('Case input and oracle must be objects')
        if (not isinstance(self.target_tasks, tuple) or len(self.target_tasks) > 16
                or any(not isinstance(task, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,99}', task)
                       for task in self.target_tasks)
                or len(set(self.target_tasks)) != len(self.target_tasks)):
            raise ValueError('Target tasks must be distinct bounded task names')
        value = asdict(self)
        value['required_capabilities'] = list(self.required_capabilities)
        value['target_tasks'] = list(self.target_tasks)
        return {**value, 'sha256': digest(value), 'inputs_sha256': digest(self.inputs),
                'oracle_sha256': digest(self.oracle)}
