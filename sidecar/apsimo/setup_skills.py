"""Install owned bundled skills into one native Hermes catalog."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


SKILL_NAME = 'apsimo-deep-research'
BUNDLE_PREFIX = 'apsimo_hermes/bundled_skills/'
SKILL_RESOURCE = f'{BUNDLE_PREFIX}{SKILL_NAME}/SKILL.md'


def _read_owned_path(path):
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError('Bundled skill destination is symlinked; existing files were preserved')
    return path.read_bytes() if path.exists() else None


def prepare(home, resources, *, refresh=False):
    """Validate all packaged skill preimages before any setup writes/probe."""
    updates = []
    for path, content in sorted(resources.items()):
        if not path.startswith(BUNDLE_PREFIX) or not path.endswith('/SKILL.md'):
            continue
        name = path[len(BUNDLE_PREFIX):-len('/SKILL.md')]
        if not re.fullmatch(r'apsimo-[a-z0-9-]{1,56}', name):
            raise ValueError('Bundled skill must have a canonical apsimo- name')
        updates.extend(_prepare_skill(home, name, content, refresh=refresh))
    if not updates:
        raise ValueError('Selected adapter has no bundled skills')
    return updates


def _prepare_skill(home, name, content, *, refresh):
    if not content:
        raise ValueError('Selected adapter has an empty bundled skill')
    directory = Path(home)/'skills'/name
    skill, marker = directory/'SKILL.md', directory/'.apsimo-owned.json'
    previous, recorded = _read_owned_path(skill), _read_owned_path(marker)
    digest = hashlib.sha256(content).hexdigest()
    if recorded is None:
        if directory.exists():
            raise ValueError('Unowned bundled skill destination was preserved; choose another profile or move your copy')
    else:
        try:
            record = json.loads(recorded)
            owned = (record['owner'] == 'apsimo-hermes' and record['version'] == 1
                     and previous is not None
                     and record['sha256'] == hashlib.sha256(previous).hexdigest())
        except (ValueError, KeyError, TypeError):
            owned = False
        if not owned:
            raise ValueError('Locally modified bundled skill was preserved; keep your copy or reconcile it before refreshing')
        if previous != content and not refresh:
            raise ValueError('Bundled skill update requires explicit apsimo init --skills-only')
    after = (json.dumps({'owner': 'apsimo-hermes', 'version': 1, 'sha256': digest},
                        sort_keys=True, indent=2)+'\n').encode()
    return [(skill, previous, content), (marker, recorded, after)]


def install(updates):
    """Replace only previously verified owned bytes; retain local edits on conflict."""
    from .setup import _atomic_hermes_config_write
    for path, before, _ in updates:
        if _read_owned_path(path) != before:
            raise ValueError('Bundled skill changed during installation; current files were preserved')
    completed = []
    try:
        for path, before, after in updates:
            if before != after:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _atomic_hermes_config_write(path, before, after)
                completed.append((path, before, after))
    except Exception:
        for path, before, after in reversed(completed):
            if _read_owned_path(path) == after:
                if before is None:
                    path.unlink()
                else:
                    _atomic_hermes_config_write(path, after, before)
        # A failed first installation leaves no false ownership claim.
        for directory in {path.parent for path, _, _ in updates}:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        raise
    names = sorted({path.parent.name for path, _, _ in updates})
    print('Bundled skills available: '+', '.join(names)+'. Use a fresh Hermes process and session to refresh the skill index.')
