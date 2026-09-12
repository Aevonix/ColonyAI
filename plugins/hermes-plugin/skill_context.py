"""Current native skill metadata beside frozen conversation prompts.

Only outgoing request context changes. No stored system prompt or historical
message is rewritten; full instructions still come from native skill_view.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import re
from threading import RLock


def _tool_names(request):
    return {str((tool.get('function') or tool).get('name') or '')
            for tool in request.get('tools', []) if isinstance(tool, dict)}


def _texts(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and isinstance(item.get('text'), str):
                yield item['text']


def _catalog(text):
    """Read native discovery metadata, never interpret source/user prose."""
    frames = re.findall(r'<available_skills>\n(.*?)\n</available_skills>', text, re.S)
    if len(frames) != 1:
        return {}
    entries = {}
    for line in frames[0].splitlines():
        match = re.fullmatch(r'\s+- ([^:\n]+)(?:: (.*))?', line)
        if match:
            entries[match[1]] = match[2] or ''
        elif '[names only]: ' in line:
            for name in line.split('[names only]: ', 1)[1].split(', '):
                entries[name] = None
    return entries


def _frozen_catalog(request):
    texts = list(_texts(request.get('instructions'))) + list(_texts(request.get('system')))
    for row in request.get('messages', request.get('input', [])):
        if isinstance(row, dict) and row.get('role') in {'system', 'developer'}:
            texts.extend(_texts(row.get('content')))
    return _catalog('\n'.join(texts))


def _loaded_skills(request, files):
    """Compare actual native skill_view results, including single wrappers.

    Reuse the adapter's native call/result parser; prose in user messages is
    never evidence that a skill was loaded. No historical rows are changed.
    """
    from .tool_observations import _request_results
    calls, results = _request_results(request)
    loaded = {}
    for call_id, result in results.items():
        identity = calls.get(call_id)
        if not identity or identity[0] != 'skill_view':
            continue
        try:
            row = json.loads(result if isinstance(result, str) else ''.join(_texts(result)))
        except (ValueError, TypeError):
            continue
        if not isinstance(row, dict) or row.get('success') is not True or not isinstance(row.get('name'), str):
            continue
        if row.get('dedup'):
            continue  # The earlier complete result remains the comparison.
        source = files.get(str(row.get('_source_path') or ''))
        if isinstance(row.get('content'), str):
            loaded[row['name']] = bool(source and source['name'] == row['name']
                and source['sha256'] == hashlib.sha256(row['content'].encode()).hexdigest())
    return loaded


def _request_note(request, text):
    result = dict(request)
    if isinstance(request.get('instructions'), str):
        result['instructions'] = request['instructions']+'\n\n'+text
    elif isinstance(request.get('system'), str):
        result['system'] = request['system']+'\n\n'+text
    elif isinstance(request.get('system'), list):
        result['system'] = [*request['system'], {'type': 'text', 'text': text}]
    elif isinstance(request.get('messages'), list):
        rows = list(request['messages'])
        index = 0
        while index < len(rows) and isinstance(rows[index], dict) and rows[index].get('role') in {'system', 'developer'}:
            index += 1
        rows.insert(index, {'role': 'system', 'content': text})
        result['messages'] = rows
    else:
        return None
    return {'request': result, 'source': 'apsimo', 'reason': 'current_skill_instructions'}


def _invalidate_native_caches(task_id, *, catalog):
    if catalog:
        from agent.prompt_builder import clear_skills_system_prompt_cache
        clear_skills_system_prompt_cache(clear_snapshot=True)
        # 0.21.2 has a separate list cache without a public reset. Keep this
        # optional compatibility touch narrow if a later runtime removes it.
        from tools import skills_tool
        clear = getattr(getattr(skills_tool, '_SKILLS_CACHE', None), 'clear', None)
        if callable(clear):
            clear()
    # Native dedup runs before the disabled-skill check. A config-only change
    # must not return the old successful "unchanged" receipt for this task.
    from tools.skills_tool_dedup import reset_skill_view_dedup
    if task_id:
        reset_skill_view_dedup(task_id)


class SkillContext:
    def __init__(self):
        self._snapshots = OrderedDict()
        self._sessions = OrderedDict()
        self._lock = RLock()

    def __call__(self, request, **context):
        tools = _tool_names(request)
        if not tools.intersection({'skills_list', 'skill_view', 'skill_manage'}):
            return None
        from hermes_constants import get_hermes_home
        from agent.skill_utils import (get_all_skills_dirs, get_project_skills_dirs,
            get_disabled_skill_names, iter_skill_index_files, parse_frontmatter)
        from agent.prompt_builder import build_skills_system_prompt

        home = Path(get_hermes_home())
        roots = [*get_project_skills_dirs(), *get_all_skills_dirs()]
        key = (str(home.resolve()), str(context.get('platform') or ''),
               tuple(str(path.resolve()) for path in roots))
        with self._lock:
            previous = self._snapshots.get(key)
            prior_files = previous['files'] if previous else {}
            files = {}
            for directory in roots:
                for filename in ('SKILL.md', 'DESCRIPTION.md'):
                    for path in iter_skill_index_files(directory, filename):
                        try:
                            stat = path.stat()
                            stamp = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
                            source = str(path.resolve())
                            old = prior_files.get(source)
                            if old and old['stamp'] == stamp:
                                files[source] = old
                                continue
                            raw = path.read_bytes()
                            fields, _ = parse_frontmatter(raw.decode())
                            name = fields.get('name', path.parent.name) if filename == 'SKILL.md' else None
                            files[source] = {'stamp': stamp, 'sha256': hashlib.sha256(raw).hexdigest(),
                                             'name': name if isinstance(name, str) else None}
                        except (OSError, ValueError):
                            continue
            disabled = tuple(sorted(get_disabled_skill_names(context.get('platform') or None)))
            fingerprint = (tuple(sorted((path, row['sha256']) for path, row in files.items())), disabled)
            changed = previous is None or previous['fingerprint'] != fingerprint
            session = (key, str(context.get('session_id') or ''), str(context.get('task_id') or ''))
            seen = self._sessions.get(session)
            if changed or seen is None or seen['fingerprint'] != fingerprint:
                _invalidate_native_caches(context.get('task_id'), catalog=changed)
            rendered = build_skills_system_prompt(available_tools=tools, skills_dir_override=home/'skills')
            current = _catalog(rendered)
            updated = {row['name'] for path, row in files.items() if row['name'] in current
                       and previous and prior_files.get(path, {}).get('sha256') != row['sha256']}
            # Keep the instruction-change notice for all calls in this task.
            # A complete current native tool result clears the reload notice,
            # including when a cold process restored an old saved conversation.
            if seen is not None and seen['fingerprint'] == fingerprint:
                updated.update(seen['updated'])
            loaded = _loaded_skills(request, files)
            updated.update(name for name, fresh in loaded.items() if name in current and not fresh)
            updated.difference_update(name for name, fresh in loaded.items() if fresh)
            self._sessions[session] = {'fingerprint': fingerprint, 'updated': updated}
            self._sessions.move_to_end(session)
            while len(self._sessions) > 128:
                self._sessions.popitem(last=False)
            self._snapshots[key] = {'fingerprint': fingerprint, 'files': files}
            self._snapshots.move_to_end(key)
            while len(self._snapshots) > 32:
                self._snapshots.popitem(last=False)

        old = _frozen_catalog(request)
        different = {name: description for name, description in current.items()
                     if name not in old or (old[name] is not None and old[name] != description)}
        removed = sorted(set(old)-set(current))
        if not different and not removed and not updated:
            return None
        lines = ['[Current skill instructions]',
                 'This current discovery information supersedes earlier skill descriptions and availability.']
        lines += [f'- {name}: {description}' for name, description in sorted(different.items())]
        if updated:
            lines.append('Added, updated or previously loaded instructions needing refresh: '+', '.join(sorted(updated))+'. '
                         'Before using these skills, reload them with skill_view and follow its current result; '
                         'prior loaded instructions may be superseded.')
        if removed:
            lines.append('Removed or unavailable: '+', '.join(removed)+'. Do not follow earlier loaded copies of these skills.')
        lines.append('Other tool capabilities and instructions are unchanged.')
        return _request_note(request, '\n'.join(lines))
