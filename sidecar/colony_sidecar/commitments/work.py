"""Atomic, renewable undertakings in the existing commitment database.

These leases coordinate explicit work. They never authorize an external effect
or claim that an obligation has been fulfilled.
"""
from contextlib import closing
import time
import uuid

from colony_sidecar.commitments.store import OPEN_STATUSES


class CommitmentWork:
    def __init__(self, store, *, clock=time.time, lease_seconds=120):
        self.store, self.clock = store, clock
        self.lease_seconds = lease_seconds
        with closing(store._connect()) as conn, conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS commitment_work (
                commitment_id TEXT PRIMARY KEY, claim_id TEXT NOT NULL,
                principal_id TEXT NOT NULL, contact_id TEXT NOT NULL,
                session_id TEXT NOT NULL, task_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                state TEXT NOT NULL, last_observed_at REAL NOT NULL, lease_until REAL NOT NULL)''')

    def _view(self, commitment, row, now):
        result = {'commitment_id': commitment['id'], 'description': commitment['description'],
                  'commitment_status': commitment['status'], 'work_state': 'unclaimed'}
        if row:
            result.update({key: row[key] for key in ('session_id', 'turn_id', 'last_observed_at', 'lease_until')})
            result['work_state'] = row['state'] if row['state'] != 'held' or row['lease_until'] > now else 'expired'
            result['observation_age_seconds'] = round(max(0, now - row['last_observed_at']), 1)
        if commitment['status'] not in OPEN_STATUSES:
            result['work_state'] = 'obligation_closed'
        return result

    def operate(self, commitment_id, *, operation, principal_id, contact_id,
                session_id, task_id, turn_id, claim_id=''):
        now = self.clock()
        holder = (principal_id, contact_id, session_id, task_id, turn_id)
        with closing(self.store._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            commitment = conn.execute('SELECT * FROM commitments WHERE id=? AND person_id=?',
                                      (commitment_id, contact_id)).fetchone()
            if commitment is None:
                raise KeyError('unknown commitment')
            row = conn.execute('SELECT * FROM commitment_work WHERE commitment_id=?', (commitment_id,)).fetchone()
            def result(accepted, reason, token=''):
                current = conn.execute('SELECT * FROM commitment_work WHERE commitment_id=?', (commitment_id,)).fetchone()
                return {**self._view(commitment, current, now), 'accepted': accepted, 'reason': reason,
                        **({'claim_id': token} if token else {}), 'effect_authorized': False}
            if operation == 'status':
                return result(True, 'observed')
            if commitment['status'] not in OPEN_STATUSES:
                return result(False, 'obligation_closed')
            same_holder = row is not None and tuple(row[key] for key in
                ('principal_id', 'contact_id', 'session_id', 'task_id', 'turn_id')) == holder
            if operation == 'claim':
                if row and row['state'] == 'held' and row['lease_until'] > now:
                    return result(same_holder, 'already_held' if same_holder else 'undertaken_elsewhere',
                                  row['claim_id'] if same_holder else '')
                token = uuid.uuid4().hex
                conn.execute('''INSERT INTO commitment_work VALUES (?,?,?,?,?,?,?,'held',?,?)
                    ON CONFLICT(commitment_id) DO UPDATE SET claim_id=excluded.claim_id,
                    principal_id=excluded.principal_id,contact_id=excluded.contact_id,
                    session_id=excluded.session_id,task_id=excluded.task_id,turn_id=excluded.turn_id,
                    state='held',last_observed_at=excluded.last_observed_at,lease_until=excluded.lease_until''',
                    (commitment_id, token, *holder, now, now + self.lease_seconds))
                return result(True, 'claimed', token)
            if not (same_holder and row['state'] == 'held' and claim_id and row['claim_id'] == claim_id):
                return result(False, 'claim_superseded')
            if operation == 'renew':
                # Extending an unchallenged holder is safe even after inactivity;
                # a racing reclaim changes the token in this same SQLite lock.
                conn.execute('UPDATE commitment_work SET last_observed_at=?,lease_until=? WHERE commitment_id=?',
                             (now, now + self.lease_seconds, commitment_id))
                return result(True, 'renewed', claim_id)
            if operation == 'release':
                conn.execute("UPDATE commitment_work SET state='released',last_observed_at=?,lease_until=? WHERE commitment_id=?",
                             (now, now, commitment_id))
                return result(True, 'released')
            raise ValueError('unknown work operation')

    def for_commitments(self, commitment_ids, *, contact_id):
        if not commitment_ids:
            return {}
        ids = list(dict.fromkeys(commitment_ids))[:20]
        with closing(self.store._connect()) as conn:
            rows = conn.execute('SELECT w.*,c.description,c.status FROM commitment_work w '
                'JOIN commitments c ON c.id=w.commitment_id WHERE c.person_id=? AND c.id IN (' +
                ','.join('?' for _ in ids) + ')', [contact_id, *ids]).fetchall()
        return {row['commitment_id']: self._view({'id': row['commitment_id'],
            'description': row['description'], 'status': row['status']}, row, self.clock()) for row in rows}
