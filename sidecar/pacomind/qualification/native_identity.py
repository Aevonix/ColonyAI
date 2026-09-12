"""Read selected Hermes distribution bytes without importing Hermes or its profile."""
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import sys


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def inspect_runtime():
    distribution = importlib.metadata.distribution('hermes-agent')
    names = set((distribution.read_text('top_level.txt') or '').split())
    if not {'run_agent', 'agent', 'hermes_cli', 'hermes_constants'} <= names:
        raise ValueError('Hermes distribution must identify its native modules')
    modules = {}
    # Resolve declared top-level names in this interpreter, rather than hashing
    # RECORD alone: an editable installation's RECORD omits its actual sources.
    for name in sorted(names):
        if not name.isidentifier():
            raise ValueError('Invalid native module inventory')
        spec = importlib.util.find_spec(name)
        if spec is None:
            raise ModuleNotFoundError(name)
        if spec.submodule_search_locations is not None:
            files = {}
            for index, location in enumerate(spec.submodule_search_locations):
                root = Path(location)
                for path in sorted(root.rglob('*')):
                    relative = path.relative_to(root)
                    if (path.is_file() and '__pycache__' not in relative.parts
                            and path.suffix not in {'.pyc', '.pyo'}):
                        files[f'{index}/{relative.as_posix()}'] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif spec.origin and Path(spec.origin).is_file():
            files = {name: hashlib.sha256(Path(spec.origin).read_bytes()).hexdigest()}
        else:
            raise ValueError('Native module has no inspectable payload')
        modules[name] = {'sha256': _digest(files), 'files': len(files)}
    metadata = distribution.read_text('METADATA') or ''
    return {'python': sys.executable, 'python_version': sys.version.split()[0],
        'distribution': distribution.metadata['Name'], 'distribution_version': distribution.version,
        'distribution_metadata_sha256': hashlib.sha256(metadata.encode()).hexdigest(),
        'native_modules': modules, 'native_payload_sha256': _digest(modules),
        'basis': 'selected Hermes distribution module and package bytes; excludes bytecode caches and third-party dependencies'}


if __name__ == '__main__':
    print(json.dumps(inspect_runtime()))
