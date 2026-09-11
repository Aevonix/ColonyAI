"""Source-bound native task associations, independent of any channel or runtime.

The existing ``native_voice_*`` table names are a compatibility contract, not
an execution platform. Hermes still owns execution; this store creates none.
"""
import hashlib
import json
import time


class TaskHandoffError(ValueError):
    """An association, source, or control operation could not be retained."""


class TaskHandoffs:
    """Durable native task associations in the caller's existing SQLite store.

    ``database()`` yields a sqlite3 connection with Row results and commits or
    rolls back on exit. ``resolve_source(source, dependencies=None)`` checks
    current canonical source readability and returns normalized provenance.
    ``resolve_owner(source, require_task_grant=...)`` independently verifies the
    current participant binding; it must not need the old content to be readable.
    Task updates additionally require the current task grant for both sources.

    These callbacks are trusted application boundaries. This class is not an
    unauthenticated HTTP API. Adapters authenticate the requester, and only native
    hooks associated with this handoff may bind turns or retain observations.
    """

    def __init__(self, database, resolve_source, resolve_owner, *,
                 error_type=TaskHandoffError, reply_effect='retained_for_transport'):
        self._database = database
        self._resolve_source = resolve_source
        self._resolve_owner = resolve_owner
        self._error = error_type
        self._reply_effect = reply_effect
        with self._database() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS native_voice_handoffs (
                id TEXT PRIMARY KEY, request_id TEXT NOT NULL, request TEXT NOT NULL,
                source_json TEXT NOT NULL, created REAL NOT NULL,
                origin_session_id TEXT, native_session_id TEXT, native_task_id TEXT, native_turn_id TEXT,
                dependencies_json TEXT, response_json TEXT, notice_json TEXT,
                UNIQUE(request_id))''')
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(native_voice_handoffs)')}
            for name in ('stop_json', 'terminal_json'):
                if name not in columns:
                    db.execute(f'ALTER TABLE native_voice_handoffs ADD COLUMN {name} TEXT')
            db.execute('''CREATE TABLE IF NOT EXISTS native_voice_updates (
                id TEXT PRIMARY KEY, handoff_id TEXT NOT NULL, instruction TEXT NOT NULL,
                source_json TEXT NOT NULL, created REAL NOT NULL,
                dispatch_json TEXT, observations_json TEXT NOT NULL DEFAULT '{}')''')

    def admit(self, *, request_id, request, source_input):
        if (not isinstance(request_id, str) or not request_id or len(request_id) > 256
                or any(ord(char) < 32 for char in request_id)):
            raise self._error('A bounded stable identifier is required')
        if not isinstance(request, str) or not request.strip() or len(request) > 32768:
            raise self._error('A bounded task request is required')
        # Only the injected resolver can attest source content and its actual
        # authenticated channel. Caller-supplied provenance is not authority.
        resolved = self._resolve_source(source_input)
        source = self._source_record(resolved)
        payload = json.dumps(source, sort_keys=True, separators=(',', ':'))
        immutable = {key: value for key, value in source.items() if key != 'watermark'}
        identity = hashlib.sha256(json.dumps([request_id, request, immutable],
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with self._database() as db:
            row = db.execute('SELECT id FROM native_voice_handoffs WHERE request_id=?', (request_id,)).fetchone()
            if row and row['id'] != identity:
                raise self._error('A request ID cannot be rebound')
            db.execute('INSERT OR IGNORE INTO native_voice_handoffs '
                '(id,request_id,request,source_json,created) VALUES(?,?,?,?,?)',
                (identity, request_id, request, payload, time.time()))
        return self.get(identity)

    def pending(self, limit=16, *, after=None):
        """Bounded retry candidates for the existing native transport drain."""
        if type(limit) is not int or not 1 <= limit <= 64:
            raise self._error('A bounded handoff batch is required')
        with self._database() as db:
            cursor = db.execute('SELECT created,id FROM native_voice_handoffs WHERE id=?', (after,)).fetchone() if after else None
            if after and cursor is None:
                return []
            return [row[0] for row in db.execute("SELECT id FROM native_voice_handoffs "
                "WHERE ((request!='' AND native_session_id IS NULL AND response_json IS NULL AND stop_json IS NULL) "
                "OR (stop_json IS NOT NULL AND terminal_json IS NULL "
                "AND COALESCE(json_extract(stop_json,'$.native_control_acknowledged'),0)=0 "
                "AND COALESCE(json_extract(stop_json,'$.native_admission_blocked'),0)=0 "
                "AND json_extract(stop_json,'$.native_resume_blocked') IS NULL)) "
                "AND (? IS NULL OR (created,id)>(?,?)) ORDER BY created,id LIMIT ?",
                (after, cursor['created'] if cursor else 0, after or '', limit))]

    def recent(self, limit=8, *, principal=None, contact_id=None):
        if type(limit) is not int or not 1 <= limit <= 64:
            raise self._error('A bounded handoff view is required')
        with self._database() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM native_voice_handoffs WHERE request!='' "
                "AND (? IS NULL OR json_extract(source_json,'$.principal')=?) "
                "AND (? IS NULL OR json_extract(source_json,'$.contact_id')=?) ORDER BY created DESC,id LIMIT ?",
                (principal, principal, contact_id, contact_id, limit))]
        return [self.get(identity) for identity in ids]

    def count(self, *, principal=None, contact_id=None):
        """Retained associations, not a count of running native workers."""
        with self._database() as db:
            return db.execute("SELECT count(*) FROM native_voice_handoffs WHERE request!='' "
                "AND (? IS NULL OR json_extract(source_json,'$.principal')=?) "
                "AND (? IS NULL OR json_extract(source_json,'$.contact_id')=?)",
                (principal, principal, contact_id, contact_id)).fetchone()[0]

    def get(self, identity):
        if not isinstance(identity, str) or len(identity) != 64:
            raise self._error('Unknown native task handoff')
        with self._database() as db:
            row = db.execute('SELECT * FROM native_voice_handoffs WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise self._error('Unknown native task handoff')
        result = dict(row)
        for key in ('source', 'dependencies', 'response', 'stop', 'terminal'):
            result[key] = json.loads(result.pop(key + '_json') or 'null')
        return result

    def control(self, identity, *, principal=None, require_task_grant=False):
        """Resolve retained ownership without requiring still-readable source content."""
        row = self.get(identity)
        source = row['source']
        if principal is not None and source['principal'] != principal:
            raise self._error('Unknown native task handoff')
        self._checked_owner(source, require_task_grant=require_task_grant)
        return row

    def _checked_owner(self, source, *, require_task_grant):
        owner = self._resolve_owner(source, require_task_grant=require_task_grant)
        if not owner or owner != source['contact_id']:
            raise self._error('Native task ownership changed')
        return owner

    def _source_owner(self, source):
        return self._checked_owner(source, require_task_grant=True)

    @staticmethod
    def _source_record(resolved):
        # Keep the original seven-field encoding, including JSON's existing
        # Unicode escaping, so adopted rows and request IDs remain stable.
        source = {key: resolved[key] for key in ('version', 'principal',
            'source_session_id', 'input_refs', 'source_refs', 'watermark', 'contact_id')}
        if 'origin' in resolved:
            source['origin'] = resolved['origin']
        return source

    def update_authorized(self, identity, update_id):
        """Current local owner grants; canonical freshness stays at the source boundary."""
        row = self.control(identity)
        update = self.get_update(identity, update_id)
        return bool(update['instruction']) and self._source_owner(row['source']) == self._source_owner(update['source'])

    def admit_update(self, identity, *, instruction, source_input, principal):
        if (not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 8192
                or source_input.get('principal') != principal):
            raise self._error('A bounded update from its authenticated source is required')
        row, original = self.resolve(identity)
        resolved = self._resolve_source(source_input)
        if (original['contact_id'] != resolved['contact_id']
                or self._source_owner(row['source']) != self._source_owner(resolved)):
            raise self._error('The update does not belong to the current task owner')
        source = self._source_record(resolved)
        immutable = {key: value for key, value in source.items() if key != 'watermark'}
        update_id = hashlib.sha256(json.dumps([identity, immutable], sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        with self._database() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT instruction FROM native_voice_updates WHERE id=?', (update_id,)).fetchone()
            if old:
                if old['instruction'] != instruction:
                    raise self._error('A captured task update cannot be rebound')
            else:
                target = db.execute('SELECT stop_json,response_json,terminal_json FROM native_voice_handoffs '
                    'WHERE id=?', (identity,)).fetchone()
                if target['stop_json'] or target['response_json'] or target['terminal_json']:
                    return None  # A terminal native turn or retained stop wins this admission.
                size = db.execute('SELECT count(*),COALESCE(sum(length(CAST(instruction AS BLOB))),0) '
                    'FROM native_voice_updates WHERE handoff_id=?', (identity,)).fetchone()
                if size[0] >= 16 or size[1] + len(instruction.encode()) > 65536:
                    raise self._error('The retained task update limit has been reached')
                prior = [json.loads(value[0]) for value in db.execute(
                    'SELECT source_json FROM native_voice_updates WHERE handoff_id=?', (identity,))]
                for name in ('input_refs', 'source_refs'):
                    refs = [*row['source'][name], *(row['dependencies'] or {}).get(name, []),
                            *source[name], *(ref for item in prior for ref in item[name])]
                    if len({json.dumps(ref, sort_keys=True) for ref in refs}) > 64:
                        raise self._error('The retained task source limit has been reached')
                db.execute('INSERT INTO native_voice_updates(id,handoff_id,instruction,source_json,created) '
                    'VALUES(?,?,?,?,?)', (update_id, identity, instruction,
                    json.dumps(source, sort_keys=True), time.time()))
        if not self.update_authorized(identity, update_id):
            raise self._error('Current task update authority is unavailable')
        return self.get_update(identity, update_id)

    def get_update(self, identity, update_id):
        with self._database() as db:
            row = db.execute('SELECT * FROM native_voice_updates WHERE id=? AND handoff_id=?',
                             (update_id, identity)).fetchone()
        if row is None:
            raise self._error('Unknown native task update')
        value = dict(row)
        for key in ('source', 'dispatch', 'observations'):
            value[key] = json.loads(value.pop(key + '_json') or 'null')
        return value

    def resolve_update(self, identity, update_id):
        row, original = self.resolve(identity)
        update = self.get_update(identity, update_id)
        if not update['instruction'] or not self.update_authorized(identity, update_id):
            raise self._error('Current task update authority is unavailable')
        resolved = self._resolve_source(update['source'])
        if original['contact_id'] != resolved['contact_id']:
            raise self._error('The update no longer belongs to the task owner')
        return row, update, resolved

    def updates(self, identity):
        with self._database() as db:
            ids = [row[0] for row in db.execute('SELECT id FROM native_voice_updates '
                'WHERE handoff_id=? ORDER BY created,id LIMIT 64', (identity,))]
        return [self.get_update(identity, update_id) for update_id in ids]

    def pending_updates(self, limit=4, *, after=None):
        if type(limit) is not int or not 1 <= limit <= 64:
            raise self._error('A bounded update batch is required')
        with self._database() as db:
            cursor = db.execute('SELECT created FROM native_voice_updates WHERE id=?', (after,)).fetchone() if after else None
            if after and cursor is None:
                return []
            return [dict(row) for row in db.execute('SELECT u.id,u.handoff_id FROM native_voice_updates u '
                'JOIN native_voice_handoffs h ON h.id=u.handoff_id WHERE u.instruction!=\'\' '
                'AND u.dispatch_json IS NULL AND h.stop_json IS NULL AND h.response_json IS NULL '
                'AND h.terminal_json IS NULL AND (? IS NULL OR (u.created,u.id)>(?,?)) '
                'ORDER BY u.created,u.id LIMIT ?', (after, cursor[0] if cursor else 0, after or '', limit))]

    def claim_update_dispatch(self, identity, update_id):
        """Claim once before native dispatch. A crash here remains uncertain, never replayable."""
        value = {'started_at': time.time(), 'state': 'dispatch_unknown'}
        with self._database() as db:
            changed = db.execute('UPDATE native_voice_updates SET dispatch_json=? WHERE id=? AND handoff_id=? '
                'AND dispatch_json IS NULL AND instruction!=\'\' AND EXISTS(SELECT 1 FROM native_voice_handoffs '
                'WHERE id=? AND stop_json IS NULL AND response_json IS NULL AND terminal_json IS NULL)',
                (json.dumps(value, sort_keys=True), update_id, identity, identity))
        return changed.rowcount == 1

    def observe_update(self, identity, update_id, observation):
        if observation.get('update_id', update_id) != update_id:
            raise self._error('Native update observation belongs to another update')
        stage = observation.get('stage')
        if stage not in {'middleware_visible', 'native_request_visible', 'native_control_acknowledged'}:
            raise self._error('Unknown native update observation')
        value = {key: observation[key] for key in ('session_id', 'task_id', 'turn_id', 'request_sha256')
                 if isinstance(observation.get(key), str) and 0 < len(observation[key]) <= 256}
        value['observed_at'] = time.time()
        with self._database() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT observations_json,source_json FROM native_voice_updates WHERE id=? AND handoff_id=?',
                             (update_id, identity)).fetchone()
            if row is None:
                raise self._error('Unknown native task update')
            observations = json.loads(row[0])
            observations.setdefault(stage, value)
            db.execute('UPDATE native_voice_updates SET observations_json=? WHERE id=? AND handoff_id=?',
                       (json.dumps(observations, sort_keys=True), update_id, identity))
            if stage in {'middleware_visible', 'native_request_visible'}:
                # Persist consumed parents before the provider/tool boundary can
                # continue, including a crash before final native turn capture.
                target = db.execute('SELECT dependencies_json,source_json,native_session_id,native_turn_id '
                    'FROM native_voice_handoffs WHERE id=?', (identity,)).fetchone()
                dependencies = json.loads(target['dependencies_json'] or '{}')
                original, source = json.loads(target['source_json']), json.loads(row['source_json'])
                for name in ('input_refs', 'source_refs'):
                    values = [*original[name], *dependencies.get(name, []), *source[name]]
                    dependencies[name] = list({json.dumps(ref, sort_keys=True): ref for ref in values}.values())
                dependencies.update(session_id=target['native_session_id'], turn_id=target['native_turn_id'])
                db.execute('UPDATE native_voice_handoffs SET dependencies_json=? WHERE id=?',
                           (json.dumps(dependencies, sort_keys=True), identity))
        return True

    @staticmethod
    def update_view(update):
        visible = any(update['observations'].get(stage) for stage in
                      ('native_control_acknowledged', 'middleware_visible', 'native_request_visible'))
        return {'update_id': update['id'], 'accepted': True,
            'dispatch_unknown': update['dispatch'] is not None and not visible,
            'dispatch_started': update['dispatch'] is not None,
            'native_control_acknowledged': bool(update['observations'].get('native_control_acknowledged')),
            'middleware_visible': bool(update['observations'].get('middleware_visible')),
            'native_request_visible': bool(update['observations'].get('native_request_visible')),
            'provider_delivery': 'unobserved', 'behavior_applied': 'unobserved',
            'observations': update['observations']}

    def request_stop(self, identity, *, principal=None):
        self.control(identity, principal=principal)
        # One SQL decision orders stop against reply retention. Repeated calls
        # retain the first intent; a reply that won first remains completed.
        value = json.dumps({'requested_at': time.time(), 'native_control_acknowledged': False})
        with self._database() as db:
            db.execute('UPDATE native_voice_handoffs SET stop_json=COALESCE(stop_json,?) '
                'WHERE id=? AND response_json IS NULL', (value, identity))
        return self.get(identity)

    def observe_stop(self, identity, *, control_acknowledged=False, admission_blocked=False, resume_session_id=None):
        with self._database() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT stop_json,native_session_id FROM native_voice_handoffs WHERE id=?',
                             (identity,)).fetchone()
            if row is None or not row['stop_json']:
                return
            value = json.loads(row['stop_json'])
            if control_acknowledged:
                value['native_control_acknowledged'] = True
                value.setdefault('control_acknowledged_at', time.time())
            if admission_blocked and not row['native_session_id']:
                value['native_admission_blocked'] = True
            if resume_session_id and resume_session_id == row['native_session_id']:
                value.setdefault('native_resume_blocked', {'session_id': resume_session_id,
                    'observed_at': time.time(), 'basis': 'native_startup_resume_suppressed'})
            db.execute('UPDATE native_voice_handoffs SET stop_json=? WHERE id=?',
                       (json.dumps(value, sort_keys=True), identity))

    def observe_terminal(self, identity, native):
        fields = ('session_id', 'task_id', 'turn_id')
        if any(not isinstance(native.get(key), str) or not native[key] for key in fields):
            return
        value = {key: native[key] for key in fields}
        value.update({key: native.get(key) is True for key in ('completed', 'failed', 'interrupted')})
        value.update(observed_at=time.time(), basis='native_on_session_end',
                     turn_exit_reason=str(native.get('turn_exit_reason') or '')[:256])
        with self._database() as db:
            db.execute('UPDATE native_voice_handoffs SET terminal_json=? WHERE id=? '
                'AND native_session_id=? AND native_task_id=? AND native_turn_id=?',
                (json.dumps(value, sort_keys=True), identity, *(native[key] for key in fields)))

    @staticmethod
    def stop_view(row):
        stop = row.get('stop')
        if not stop:
            return None
        terminal = row.get('terminal')
        if terminal and any(terminal.get(key) != row['native_' + key]
                            for key in ('session_id', 'task_id', 'turn_id')):
            terminal = None
        resumed = stop.get('native_resume_blocked')
        resumed = resumed if resumed and resumed.get('session_id') == row['native_session_id'] else None
        basis = ('native_turn_finalized' if terminal else 'native_startup_resume_suppressed' if resumed
                 else 'native_admission_blocked' if stop.get('native_admission_blocked') else None)
        settled = basis is not None
        return {'status': 'cancelled' if settled else 'stopping',
                'stop': {**stop, 'cancellation_basis': basis, 'native_turn_termination': terminal,
                         'process_cleanup': 'unobserved'}}

    def resolve(self, identity):
        row = self.get(identity)
        return row, self._resolve_source(row['source'], row['dependencies'])

    def bind(self, identity, native):
        fields = ('session_id', 'task_id', 'turn_id')
        if any(not isinstance(native.get(key), str) or not native[key] for key in fields):
            raise self._error('Native turn identity is unavailable')
        with self._database() as db:
            row = db.execute('SELECT native_session_id FROM native_voice_handoffs WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise self._error('Native task handoff is unavailable')
            # Only the native hook in the correlated handler calls this. A
            # compression successor is an observed session, not a new owner.
            db.execute('UPDATE native_voice_handoffs SET origin_session_id=COALESCE(origin_session_id,?), '
                'terminal_json=CASE WHEN native_session_id=? AND native_task_id=? AND native_turn_id=? '
                'THEN terminal_json ELSE NULL END, '
                "stop_json=CASE WHEN stop_json IS NOT NULL THEN json_remove(stop_json,"
                "'$.native_admission_blocked','$.native_resume_blocked') ELSE NULL END, "
                'native_session_id=?, native_task_id=?, native_turn_id=? WHERE id=?',
                (native['session_id'], *(native[key] for key in fields),
                 *(native[key] for key in fields), identity))

    def complete_source(self, identity, dependencies):
        if not isinstance(dependencies, dict) or not dependencies.get('input_refs'):
            raise self._error('Native result has no source receipt')
        row, _ = self.resolve(identity)
        if not dependencies.get('session_id') or not row['native_session_id']:
            raise self._error('Native result has no observed native session')
        self._resolve_source(row['source'], dependencies)
        with self._database() as db:
            changed = db.execute('UPDATE native_voice_handoffs SET dependencies_json=?,native_session_id=? '
                'WHERE id=? AND stop_json IS NULL',
                (json.dumps(dependencies, sort_keys=True), dependencies['session_id'], identity))
            if changed.rowcount != 1:
                raise self._error('Native task reply was stopped')

    def retain_notice(self, identity, content):
        self.resolve(identity)
        with self._database() as db:
            db.execute('UPDATE native_voice_handoffs SET notice_json=? WHERE id=?',
                (json.dumps({'text': content[:2000], 'effect': 'retained_notice'}), identity))

    def retain_reply(self, identity, content):
        row, _ = self.resolve(identity)
        if not row['dependencies']:
            raise self._error('Native reply has no current source receipt')
        if not isinstance(content, str) or not content.strip():
            raise self._error('Native reply is empty')
        response = {'text': content, 'source_dependencies': row['dependencies'],
                    'effect': self._reply_effect, 'playback': 'unobserved'}
        with self._database() as db:
            encoded = json.dumps(response, sort_keys=True)
            changed = db.execute('UPDATE native_voice_handoffs SET response_json=? WHERE id=? '
                'AND stop_json IS NULL AND (response_json IS NULL OR response_json=?)',
                (encoded, identity, encoded))
            if changed.rowcount != 1:
                raise self._error('Native task reply was stopped or a different reply is already retained')
        return response



def erase_task_handoffs(database, contact_id, rules):
    """Erase owned text copies during the existing source erasure pass."""
    if not rules:
        return
    from .client import redact_source_payload, source_input_erased
    with database() as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_voice_handoffs'").fetchone() is None:
            return
        rows = db.execute("SELECT id,source_json,dependencies_json FROM native_voice_handoffs "
            "WHERE json_extract(source_json,'$.contact_id')=? AND (request!='' OR response_json IS NOT NULL OR notice_json IS NOT NULL)",
            (contact_id,)).fetchall()
        for row in rows:
            source = json.loads(row['source_json'])
            deps = json.loads(row['dependencies_json'] or '{}')
            inputs = [*source['input_refs'], *deps.get('input_refs', [])]
            sources = [*source['source_refs'], *deps.get('source_refs', [])]
            payload = {'contact_id': contact_id, 'session_id': source['source_session_id'],
                'turn_id': row['id'], 'assistant_message': 'retained native transport content',
                'assistant_source_refs': sources}
            if (any(source_input_erased(ref, rules) for ref in inputs)
                    or redact_source_payload(payload, rules) is None):
                db.execute("UPDATE native_voice_handoffs SET request='',response_json=NULL,notice_json=NULL WHERE id=?", (row['id'],))
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_voice_updates'").fetchone():
            for row in db.execute("SELECT id,source_json FROM native_voice_updates WHERE instruction!='' "
                    "AND json_extract(source_json,'$.contact_id')=?", (contact_id,)).fetchall():
                source = json.loads(row['source_json'])
                payload = {'contact_id': contact_id, 'session_id': source['source_session_id'],
                    'turn_id': row['id'], 'assistant_message': 'retained update copy',
                    'assistant_source_refs': source['source_refs']}
                if (any(source_input_erased(ref, rules) for ref in source['input_refs'])
                        or redact_source_payload(payload, rules) is None):
                    db.execute("UPDATE native_voice_updates SET instruction='' WHERE id=?", (row['id'],))
