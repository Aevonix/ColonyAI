"""Reply expectations on the commitment ledger, with no scheduler or sender.

Host adapters supply authenticated contact/source/transport evidence. The normal
Hermes tick calls due(), and the selected outbox remains the only delivery
owner. Worker preparation and sending each recheck preflight independently.
"""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import time
from zoneinfo import ZoneInfo

from pacomind.commitments.store import OPEN_STATUSES


TERMINAL = frozenset({'resolved', 'cancelled', 'expired'})


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def epoch(value):
    if isinstance(value, bool):
        raise ValueError('invalid time')
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if value.tzinfo is None:
            raise ValueError('local time requires an explicit UTC offset')
        value = value.timestamp()
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError('invalid time')
    return float(value)


def reference(value):
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError('durable reference required')
    return value


def minute(value):
    if not isinstance(value, str) or len(value) != 5 or value[2] != ':':
        raise ValueError('local window must use HH:MM')
    hour, minutes = map(int, value.split(':'))
    if not 0 <= hour <= 23 or not 0 <= minutes <= 59:
        raise ValueError('invalid local window')
    return hour * 60 + minutes


def in_window(current, start, end):
    return start <= current < end if start < end else current >= start or current < end


def consent_matches(*, authority, binding, scope, now):
    """Check existing consumed authority; never issue or consume another grant.

    The host constructs both scope and ActionBinding from the immutable task
    action (recipient, channels, purpose, bounds, expiry), never model prose.
    A last consumed use may have exhausted its grant and remains valid until
    expiry/revocation. An unrelated task or materially changed scope cannot fit.
    """
    if not scope or binding is None or authority is None or encoded(binding.scope) != encoded(scope):
        return False
    digest = hashlib.sha256(encoded(scope).encode()).hexdigest()
    if digest != binding.scope_digest:
        return False
    use = authority.get_grant_use(binding.action_digest)
    if not use or use.get('scope_digest') != digest or use.get('grant_status') not in {'active', 'exhausted'}:
        return False
    if use.get('grant_expires_at') is not None and epoch(use['grant_expires_at']) <= now:
        return False
    grants = authority.list_grants(now=datetime.fromtimestamp(now, timezone.utc))
    return any(g['grant_id'] == use['grant_id'] and g['status'] in {'active', 'exhausted'}
               and encoded(g['scope']) == encoded(scope) for g in grants)


class TemporalFollowups:
    """One waiting row per stable condition, in the existing commitments DB.

    authority_scope is an immutable *configuration match*, not authorization.
    Caller identity, privacy visibility and task ownership are host concerns.
    No delivery status is copied: outbox_state is observed at the send boundary.
    """
    def __init__(self, commitment_store, *, clock=time.time):
        self.store, self.clock = commitment_store, clock
        with closing(self.store._connect()) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS temporal_followups (
                    wait_id TEXT PRIMARY KEY, commitment_id TEXT NOT NULL,
                    work_id TEXT NOT NULL, contact_id TEXT NOT NULL,
                    state TEXT NOT NULL, revision INTEGER NOT NULL,
                    material_digest TEXT NOT NULL, payload TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS temporal_followups_contact_state
                    ON temporal_followups(contact_id,state);
            ''')
            db.commit()

    @contextmanager
    def transaction(self):
        with closing(self.store._connect()) as db:
            try:
                db.execute('BEGIN IMMEDIATE')
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    @staticmethod
    def _row(row):
        if row is None:
            raise ValueError('unknown reply expectation')
        return {**json.loads(row['payload']), **{key: row[key] for key in (
            'wait_id', 'commitment_id', 'work_id', 'contact_id', 'state', 'revision', 'created_at', 'updated_at')}}

    def _get(self, db, wait_id):
        return self._row(db.execute('SELECT * FROM temporal_followups WHERE wait_id=?', (wait_id,)).fetchone())

    def _save(self, db, row):
        row['revision'] += 1
        row['updated_at'] = self.clock()
        db.execute('UPDATE temporal_followups SET state=?,revision=?,payload=?,updated_at=? WHERE wait_id=?',
                   (row['state'], row['revision'], encoded(row), row['updated_at'], row['wait_id']))
        return row

    def get(self, wait_id):
        with closing(self.store._connect()) as db:
            return self._get(db, wait_id)

    def expect_reply(self, *, wait_id, commitment_id, work_id, contact_id,
                     outbound_ref, source_refs, expected_after_seconds, expires_at,
                     timezone_name='UTC', quiet_start=None, quiet_end=None,
                     availability_start=None, availability_end=None,
                     forecast_id='', authority_scope=None, promised_at=None,
                     original_local_text='', source_versions=None, source_session_id=''):
        for value in (wait_id, commitment_id, work_id, contact_id, outbound_ref):
            reference(value)
        refs = sorted({reference(r) for r in source_refs})
        if not refs or len(refs) > 40:
            raise ValueError('reply expectation requires bounded source evidence')
        versions = dict(source_versions or {})
        if set(versions) != set(refs) or any(not isinstance(v, str) or not v for v in versions.values()):
            raise ValueError('reply expectation requires exact source versions')
        seconds = epoch(expected_after_seconds)
        expiry = epoch(expires_at)
        ZoneInfo(timezone_name)
        for start, end in ((quiet_start, quiet_end), (availability_start, availability_end)):
            if (start is None) != (end is None):
                raise ValueError('both local window bounds required')
            if start is not None and minute(start) == minute(end):
                raise ValueError('local window must have nonzero duration')
        if len(original_local_text) > 256:
            raise ValueError('local interpretation too long')
        material = dict(wait_id=wait_id, commitment_id=commitment_id, work_id=work_id,
                        contact_id=contact_id, outbound_ref=outbound_ref, source_refs=refs,
                        source_versions=versions, source_session_id=source_session_id, expected_after_seconds=seconds,
                        expires_at=expiry, timezone_name=timezone_name, quiet_start=quiet_start,
                        quiet_end=quiet_end, availability_start=availability_start,
                        availability_end=availability_end, forecast_id=forecast_id,
                        authority_scope=dict(authority_scope or {}), promised_at=epoch(promised_at) if promised_at else None,
                        original_local_text=original_local_text)
        digest = hashlib.sha256(encoded(material).encode()).hexdigest()
        with self.transaction() as db:
            current = db.execute('SELECT * FROM temporal_followups WHERE wait_id=?', (wait_id,)).fetchone()
            if current:
                if current['material_digest'] != digest:
                    raise ValueError('reply condition identity conflict')
                return self._row(current)
            parent = db.execute('SELECT * FROM commitments WHERE id=?', (commitment_id,)).fetchone()
            if not parent or parent['status'] not in OPEN_STATUSES:
                raise ValueError('open parent commitment required')
            now = self.clock()
            if expiry <= now:
                raise ValueError('reply expectation is already expired')
            row = {**material, 'expected_at': None, 'dispatch_receipt_ref': None,
                   'dispatch_occurred_at': None, 'next_review_at': None,
                   'native_task_id': None, 'native_terminal_observed': False, 'native_terminal_status': None, 'reply': None, 'resolution_ref': None,
                   'followup_receipt_ref': None, 'followup_action_digest': None}
            db.execute('INSERT INTO temporal_followups VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (wait_id, commitment_id, work_id, contact_id, 'open', 1, digest, encoded(row), now, now))
            return self._get(db, wait_id)

    def acknowledge_dispatch(self, wait_id, *, receipt_ref, occurred_at):
        reference(receipt_ref)
        occurred = epoch(occurred_at)
        if occurred > self.clock():
            raise ValueError('dispatch receipt cannot be in the future')
        with self.transaction() as db:
            row = self._get(db, wait_id)
            if row['dispatch_receipt_ref'] is not None:
                if (row['dispatch_receipt_ref'], row['dispatch_occurred_at']) != (receipt_ref, occurred):
                    raise ValueError('dispatch receipt conflict')
                return row
            row.update(dispatch_receipt_ref=receipt_ref, dispatch_occurred_at=occurred,
                       expected_at=occurred + row['expected_after_seconds'])
            if row['state'] != 'deferred':
                row['next_review_at'] = row['expected_at']
            # A matching reply may arrive before this ACK. Do not reopen it.
            return self._save(db, row)

    def apply_reply(self, wait_id, match):
        """Consume A's exact CommsLog match, never any-inbound activity.

        Root obtains match from the authenticated ledger, not a tool argument.
        Late replies remain linked even when waiting already expired/cancelled.
        They do not rewrite the corresponding forecast deadline outcome.
        """
        with self.transaction() as db:
            row = self._get(db, wait_id)
            if match.get('status') != 'matched' or match.get('contact_id') != row['contact_id'] or match.get('outbound_ref') != row['outbound_ref']:
                return row
            matches = []
            for item in match.get('matches', [])[:50]:
                if item.get('reply_to_ref') != row['outbound_ref'] or not item.get('external_ref') or not item.get('receipt_ref') or not item.get('ts'):
                    continue
                try:
                    stamp = epoch(item['ts'])
                    reference(item['external_ref'])
                    reference(item['receipt_ref'])
                except (TypeError, ValueError):
                    continue
                # Exact provider linkage can predate both wait registration
                # and our first retained ACK. Use the parent obligation's
                # actual origin, never an ACK timestamp as causal proof.
                parent = db.execute('SELECT made_at FROM commitments WHERE id=?', (row['commitment_id'],)).fetchone()
                if parent is None or stamp < epoch(parent['made_at']) or stamp > self.clock():
                    continue
                matches.append({key: item.get(key) for key in ('external_ref', 'reply_to_ref', 'provider_reply_to_ref', 'receipt_ref', 'ts', 'channel', 'reaction')})
            if not matches or row['reply'] is not None:
                return row
            row['reply'] = {'matches': matches, 'recorded_at': self.clock()}
            row['resolution_ref'] = matches[0]['receipt_ref']
            if row['state'] not in {'cancelled', 'expired'}:
                row['state'] = 'resolved'
            return self._save(db, row)

    def cancel(self, wait_id, *, evidence_ref):
        reference(evidence_ref)
        with self.transaction() as db:
            row = self._get(db, wait_id)
            if row['state'] not in TERMINAL:
                row.update(state='cancelled', resolution_ref=evidence_ref, cancelled_at=self.clock())
                return self._save(db, row)
            return row

    def defer(self, wait_id, *, until, evidence_ref):
        reference(evidence_ref)
        stamp = epoch(until)
        with self.transaction() as db:
            row = self._get(db, wait_id)
            if not self.clock() < stamp < row['expires_at'] or row['state'] in TERMINAL:
                raise ValueError('cannot defer beyond expiry or after resolution')
            row.update(state='deferred', next_review_at=stamp, resolution_ref=evidence_ref)
            return self._save(db, row)

    def _refresh(self, db, row, now):
        parent = db.execute('SELECT status FROM commitments WHERE id=?', (row['commitment_id'],)).fetchone()
        if row['state'] not in TERMINAL:
            if not parent or parent['status'] not in OPEN_STATUSES:
                row.update(state='cancelled', resolution_ref='commitment:'+row['commitment_id'])
                self._save(db, row)
            elif now >= row['expires_at']:
                row.update(state='expired', resolution_ref=None)
                self._save(db, row)
        return row

    @staticmethod
    def _reason(row, now):
        if row['state'] in TERMINAL:
            return row['state']
        if row['reply']:
            return 'reply_observed'
        if row['dispatch_receipt_ref'] is None:
            return 'awaiting_dispatch_evidence'
        if row['followup_receipt_ref']:
            return 'followup_already_recorded'
        if now < max(row['expected_at'], row['next_review_at'] or row['expected_at']):
            return 'not_due'
        local = datetime.fromtimestamp(now, ZoneInfo(row['timezone_name']))
        current = local.hour * 60 + local.minute
        if row['quiet_start'] is not None and in_window(current, minute(row['quiet_start']), minute(row['quiet_end'])):
            return 'quiet_window'
        if row['availability_start'] is not None and not in_window(current, minute(row['availability_start']), minute(row['availability_end'])):
            return 'outside_availability'
        return 'due'

    def list_for_context(self, *, contact_id, limit=30, now=None, outbound_refs=None):
        point = self.clock() if now is None else epoch(now)
        where, params = 'contact_id=?', [contact_id]
        if outbound_refs is not None:
            refs = tuple(dict.fromkeys(outbound_refs))
            if not refs:
                return []
            # Select the receipt's exact waits before applying the context cap.
            where += " AND json_extract(payload,'$.outbound_ref') IN (" + ','.join('?' for _ in refs) + ')'
            params.extend(refs)
        params.append(min(100, max(1, int(limit))))
        with self.transaction() as db:
            values = db.execute('SELECT * FROM temporal_followups WHERE '+where+' ORDER BY updated_at DESC LIMIT ?', params).fetchall()
            result = []
            for value in values:
                row = self._refresh(db, self._row(value), point)
                result.append({**row, 'eligibility': self._reason(row, point),
                               'overdue': row['state'] not in TERMINAL and row['expected_at'] is not None and point >= row['expected_at']})
            return result

    def due(self, *, now=None, limit=100):
        point = self.clock() if now is None else epoch(now)
        with self.transaction() as db:
            # The installed tick is the only driver. Include terminal rows with
            # native tasks so the adapter can reconcile cancellation after restart.
            values = db.execute('SELECT * FROM temporal_followups WHERE state IN (\'open\',\'deferred\') OR (json_extract(payload,\'$.native_task_id\') IS NOT NULL AND NOT json_extract(payload,\'$.native_terminal_observed\')) ORDER BY updated_at').fetchall()
            rows = [self._refresh(db, self._row(v), point) for v in values]
        return [{**r, 'eligibility': self._reason(r, point)} for r in rows
                if not r.get('native_terminal_observed') and (self._reason(r, point) == 'due' or r['native_task_id'])][:min(200, max(1, int(limit)))]

    def preflight(self, wait_id, *, authority=None, binding=None, outbox_state='unknown', now=None):
        point = self.clock() if now is None else epoch(now)
        with self.transaction() as db:
            row = self._refresh(db, self._get(db, wait_id), point)
        reason = self._reason(row, point)
        review = reason == 'due'
        authorized = review and consent_matches(authority=authority, binding=binding, scope=row['authority_scope'], now=point)
        dispatch = authorized and outbox_state == 'absent'
        return {'wait_id': wait_id, 'revision': row['revision'], 'review_allowed': review,
                'dispatch_allowed': bool(dispatch), 'reason': reason if not review else
                'outbox_'+outbox_state if authorized and not dispatch else 'consent_missing' if not authorized else 'allowed',
                'native_task_id': row['native_task_id']}

    def bind_native_task(self, wait_id, *, native_task_id):
        reference(native_task_id)
        with self.transaction() as db:
            row = self._refresh(db, self._get(db, wait_id), self.clock())
            if row['native_task_id']:
                if row['native_task_id'] != native_task_id:
                    raise ValueError('reply wait already bound to another native task')
                return row
            # Binding is association only. Even a reply racing attachment
            # must bind the blocked task so prepare can cancel it safely.
            row['native_task_id'] = native_task_id
            return self._save(db, row)

    def observe_native_terminal(self, wait_id, *, native_task_id, native_status):
        """Host calls only after independent native done/archive/failure readback."""
        if native_status not in {'done', 'archived', 'cancelled', 'failed'}:
            raise ValueError('native terminal observation required')
        with self.transaction() as db:
            row = self._get(db, wait_id)
            if row['native_task_id'] != native_task_id:
                raise ValueError('native followup association mismatch')
            if not row.get('native_terminal_observed'):
                row['native_terminal_observed'] = True
                row['native_terminal_status'] = native_status
                return self._save(db, row)
            return row

    def bind_authority_scope(self, wait_id, *, scope, evidence_ref):
        """Trusted host binds its immutable task consent configuration once.

        This grants nothing: preflight still requires the authority store's
        consumed exact-scope grant at the actual send boundary. It lets a
        passive wait receive task-scoped consent later without replacing work.
        """
        reference(evidence_ref)
        if not isinstance(scope, dict) or not scope or len(encoded(scope)) > 16000:
            raise ValueError('bounded task authority configuration required')
        with self.transaction() as db:
            row = self._refresh(db, self._get(db, wait_id), self.clock())
            if row['authority_scope']:
                if encoded(row['authority_scope']) != encoded(scope):
                    raise ValueError('task authority configuration is immutable')
                return row
            if row['state'] in TERMINAL:
                raise ValueError('reply wait is already terminal')
            row.update(authority_scope=scope, authority_configuration_ref=evidence_ref)
            return self._save(db, row)

    def mark_followup_dispatched(self, wait_id, *, action_digest, receipt_ref):
        """Record the selected outbox's receipt, never manufacture a send ACK."""
        reference(action_digest)
        reference(receipt_ref)
        with self.transaction() as db:
            row = self._get(db, wait_id)
            if row['followup_receipt_ref']:
                if (row['followup_action_digest'], row['followup_receipt_ref']) != (action_digest, receipt_ref):
                    raise ValueError('followup receipt conflict')
                return row
            row.update(followup_action_digest=action_digest, followup_receipt_ref=receipt_ref,
                       followup_observed_at=self.clock())
            return self._save(db, row)

    def invalidate_sources(self, source_refs, *, evidence_ref):
        """Cancel affected pending work after root's correction/erasure event."""
        refs = set(source_refs)
        reference(evidence_ref)
        changed = []
        with self.transaction() as db:
            rows = db.execute('SELECT * FROM temporal_followups').fetchall()
            for raw in rows:
                row = self._row(raw)
                if refs.intersection(row['source_refs']) and row['state'] not in TERMINAL:
                    row.update(state='cancelled', resolution_ref=evidence_ref)
                    self._save(db, row)
                    changed.append(row['wait_id'])
        return changed
