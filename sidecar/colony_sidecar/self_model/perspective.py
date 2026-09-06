"""Source-backed preferences and bounded operational judgments.

This is a portable working perspective, not feelings, a worldview or authority.
Original owner words stay in the canonical source ledger. Operational judgments
reuse effective competence evidence and only adjust optional research priority.
"""
from __future__ import annotations

from contextlib import closing
from copy import copy
from datetime import datetime, timezone
import hashlib
import json
import re
import time


DOMAINS = frozenset({'research', 'knowledge_acquisition'})
VERSION = 'operational-perspective-v1'


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS self_preference_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL,
        pref_key TEXT NOT NULL, value_json TEXT NOT NULL,
        source_turn_id TEXT NOT NULL, message_hash TEXT NOT NULL,
        source_at TEXT NOT NULL, supersedes INTEGER,
        UNIQUE(owner_id,pref_key,source_turn_id,message_hash))''')
    # A value-free marker prevents erased sourced corrections from revealing
    # an older unprovenanced cache value for the same preference.
    conn.execute('''CREATE TABLE IF NOT EXISTS self_preference_keys (
        owner_id TEXT NOT NULL,pref_key TEXT NOT NULL,latest_event_id INTEGER,latest_source_at TEXT NOT NULL,
        PRIMARY KEY(owner_id,pref_key))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS self_opinion_revisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT NOT NULL,
        weight REAL NOT NULL, basis_json TEXT NOT NULL, basis_digest TEXT NOT NULL,
        reason TEXT NOT NULL, updated_at REAL NOT NULL, version TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS self_attention (
        slot INTEGER PRIMARY KEY CHECK(slot=1), snapshot_json TEXT NOT NULL,
        observed_at REAL NOT NULL)''')


def erase_removed(conn, turn_id, session_id, retained):
    from colony_sidecar.turns.idempotency import source_message_hash
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='self_preference_events'").fetchone():
        return
    hashes = {source_message_hash(session_id, message) for message in retained}
    rows = conn.execute('SELECT id,message_hash FROM self_preference_events WHERE source_turn_id=?', (turn_id,)).fetchall()
    for row in rows:
        if row['message_hash'] not in hashes:
            conn.execute('DELETE FROM self_preference_events WHERE id=?', (row['id'],))
    # Attention contains correction references/weights; do not retain a stale
    # derived snapshot after deleting its source. The next decision rebuilds it.
    conn.execute('DELETE FROM self_attention')


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


class SelfPerspective:
    def __init__(self, ledger, *, owner_id, clock=time.time):
        self.ledger = ledger
        self.owner_id = str(owner_id or '')
        self.clock = clock
        with closing(ledger._connect()) as conn, conn:
            initialize(conn)

    def observe_source(self, turn_id, learner):
        """Learn only direct, attributed owner words; replay does not add votes."""
        from colony_sidecar.turns.idempotency import source_message_hash
        if not self.owner_id:
            return []
        learned = []
        with closing(self.ledger._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            source = conn.execute("SELECT * FROM turn_sources WHERE turn_id=? AND contact_id=? AND scope='person'", (turn_id, self.owner_id)).fetchone()
            if source is None:
                return []
            for message in json.loads(source['messages_json']):
                if message.get('role') != 'user':
                    continue
                text = message.get('content', '')
                if isinstance(text, list):
                    text = '\n'.join(p.get('text', '') for p in text if p.get('type') in {'text', 'input_text'})
                if not isinstance(text, str):
                    continue
                updates = self._directives(text, learner)
                for key, value in updates:
                    source_at = datetime.fromisoformat(source['occurred_at'] or source['ingested_at']).astimezone(timezone.utc).isoformat()
                    previous = conn.execute('SELECT id FROM self_preference_events WHERE owner_id=? AND pref_key=? AND source_at<=? ORDER BY source_at DESC,id DESC LIMIT 1', (self.owner_id, key, source_at)).fetchone()
                    cur = conn.execute('''INSERT OR IGNORE INTO self_preference_events
                        (owner_id,pref_key,value_json,source_turn_id,message_hash,source_at,supersedes)
                        VALUES (?,?,?,?,?,?,?)''', (self.owner_id, key, _json(value), turn_id,
                        source_message_hash(source['session_id'], message),
                        source_at, previous['id'] if previous else None))
                    if cur.rowcount:
                        conn.execute('''INSERT INTO self_preference_keys VALUES (?,?,?,?)
                            ON CONFLICT(owner_id,pref_key) DO UPDATE SET latest_event_id=excluded.latest_event_id,
                            latest_source_at=excluded.latest_source_at WHERE excluded.latest_source_at>=self_preference_keys.latest_source_at''',
                            (self.owner_id, key, cur.lastrowid, source_at))
                        learned.append((key, value))
            return learned

    @staticmethod
    def _directives(text, learner):
        raw = text.strip()
        # Conservative direct-address subset. Quotation, questions, reports
        # about another person and ambiguous negation remain raw evidence.
        if len(raw.split()) > 40 or any(c in raw for c in ('"', '“', '”', '?', '`', '\n')):
            return []
        operational = re.fullmatch(r'(?:please\s+)?(prefer|deprioritize|use normal priority for)\s+(research|knowledge acquisition)\s+initiatives[.!]?', raw, re.I)
        if operational:
            weight = {'prefer': 1.2, 'deprioritize': 0.8, 'use normal priority for': 1.0}[operational[1].lower()]
            return [('initiative.' + operational[2].lower().replace(' ', '_'), weight)]
        if not re.match(r'^(?:(?:please|actually|from now on|going forward)[,:]?\s+)*(?:be\b|keep\b|use\b|stop\b|skip\b|avoid\b|don.t\b|do not\b|no emoji\b|no emojis\b|i prefer\b|i want\b)', raw, re.I):
            return []
        # Reuse existing style vocabulary, but do not guess the direction of
        # a negated or comparative request from bag-of-words keyword order.
        polarity = re.sub(r'\b(?:stop\s+(?:using\s+|with\s+)?|skip\s+(?:the\s+)?|avoid\s+(?:the\s+)?|no\s+|don.t\s+use\s+|do\s+not\s+use\s+)emojis?\b', '', raw, flags=re.I)
        if re.search(r'\b(not|never|rather|instead|than|except|without|don.t|stop|skip|avoid|no)\b', polarity, re.I):
            return []
        from colony_sidecar.intelligence.components import preference_learner as vocabulary
        words = set(re.findall(r'\b\w+\b', raw.lower()))
        style_words = vocabulary._STYLE_KEYWORDS | vocabulary._DIRECTIVE_CUES | vocabulary._NEGATION | {'and', 'the', 't', 'a', 'an', 'of', 'with', 'so', 'being', 'all', 'future', 'my', 'me', 'actually'}
        if words - style_words:
            return []  # Content/task-specific instructions are not standing style corrections.
        for alternatives in ((vocabulary._LENGTH_SHORT, vocabulary._LENGTH_LONG, vocabulary._LENGTH_MEDIUM),
                             (vocabulary._FORMAT_BULLETS, vocabulary._FORMAT_PROSE, vocabulary._FORMAT_TABLE),
                             (vocabulary._STYLE_FORMAL, vocabulary._STYLE_CASUAL)):
            if sum(bool(words & option) for option in alternatives) > 1:
                return []
        if not learner.detect_directive(raw):
            return []
        return [('communication_style.' + key, value) for key, value in learner._parse_all(raw)]

    def preferences(self, *, history=False):
        from colony_sidecar.turns.idempotency import source_message_hash
        with closing(self.ledger._connect()) as conn:
            heads = {r[0]: r[1] for r in conn.execute('SELECT pref_key,latest_event_id FROM self_preference_keys WHERE owner_id=?', (self.owner_id,))}
            query = '''SELECT p.*,s.session_id,s.messages_json FROM self_preference_events p
                JOIN turn_sources s ON s.turn_id=p.source_turn_id
                WHERE p.owner_id=? AND s.contact_id=? AND s.scope='person' '''
            if not history:
                query += 'AND p.id IN (SELECT latest_event_id FROM self_preference_keys WHERE owner_id=p.owner_id) '
            query += 'ORDER BY p.source_at DESC,p.id DESC'
            if history:
                query += ' LIMIT 100'
            rows = conn.execute(query, (self.owner_id, self.owner_id)).fetchall()
            result, seen = [], set()
            for row in rows:
                if row['message_hash'] not in {source_message_hash(row['session_id'], m) for m in json.loads(row['messages_json'])}:
                    continue
                if not history and (row['pref_key'] in seen or row['id'] != heads.get(row['pref_key'])):
                    continue
                seen.add(row['pref_key'])
                result.append({key: row[key] for key in ('id', 'pref_key', 'source_turn_id', 'message_hash', 'source_at', 'supersedes')} | {'value': json.loads(row['value_json']), 'basis': 'owner_correction'})
            return result

    def tracked_keys(self):
        with closing(self.ledger._connect()) as conn:
            return {r[0] for r in conn.execute('SELECT pref_key FROM self_preference_keys WHERE owner_id=?', (self.owner_id,))}

    def refresh(self, competence):
        """At least three distinct completed work sources per ordinary update.

        Weights stay in [0.8,1.2] and move at most 0.05 per new evidence batch.
        Reconciled/withdrawn evidence resets an unsupported judgment immediately.
        These are priority weights, never statistical confidence or permission.
        """
        if competence is None:
            return
        for domain in sorted(DOMAINS):
            events = competence.events(domain, include_shadow=False, limit=120)
            unique = {}
            for event in events:
                if event.get('evidence_status') not in {'observed', 'verified'} or not event.get('source_ref') or not event.get('event_key') or event.get('outcome_contract') in {None, '', 'legacy.unversioned'}:
                    continue
                key = str(event['source']) + ':' + str(event['source_ref'])
                evidence = event.get('evidence') or {}
                if not isinstance(evidence, dict):
                    evidence = {}
                unique.setdefault(key, {'key': key, 'event_id': event['id'], 'fingerprint': event['fingerprint'], 'outcome': event['outcome'],
                    'model_role': evidence.get('model_role', 'unknown'), 'model_id': evidence.get('model_id', 'unknown'),
                    'model_revision': evidence.get('model_revision', 'unknown'), 'evidence_status': event['evidence_status']})
            basis = list(unique.values())[:20]
            digest = hashlib.sha256(_json(basis).encode()).hexdigest()
            with closing(self.ledger._connect()) as conn, conn:
                conn.execute('BEGIN IMMEDIATE')
                previous = conn.execute('SELECT * FROM self_opinion_revisions WHERE domain=? ORDER BY id DESC LIMIT 1', (domain,)).fetchone()
                if previous and previous['basis_digest'] == digest:
                    continue
                old = json.loads(previous['basis_json']) if previous else []
                old_keys = {item['key'] for item in old}
                removed = old_keys - set(unique)
                revised = any(item['key'] in unique and (item['outcome'] != unique[item['key']]['outcome'] or item['fingerprint'] != unique[item['key']]['fingerprint']) for item in old)
                new_count = len({item['key'] for item in basis} - old_keys)
                if new_count < 3 and not removed and not revised:
                    continue
                if len(basis) < 3:
                    weight, reason = 1.0, 'Insufficient surviving independent work evidence; use neutral priority.'
                else:
                    failures = sum(item['outcome'] != 'success' for item in basis)
                    target = 1.0 + 0.2 * (1.0 - 2.0 * failures / len(basis))
                    previous_weight = float(previous['weight']) if previous else 1.0
                    weight = target if removed or revised else max(previous_weight - 0.05, min(previous_weight + 0.05, target))
                    reason = f'{failures}/{len(basis)} distinct recent work runtime outcomes failed or timed out; optional {domain} priority reflects this working judgment, not verified semantic quality.'
                conn.execute('INSERT INTO self_opinion_revisions(domain,weight,basis_json,basis_digest,reason,updated_at,version) VALUES (?,?,?,?,?,?,?)',
                    (domain, round(max(0.8, min(1.2, weight)), 4), _json(basis), digest, reason, self.clock(), VERSION))

    def opinions(self):
        with closing(self.ledger._connect()) as conn:
            rows = conn.execute('SELECT * FROM self_opinion_revisions WHERE id IN (SELECT max(id) FROM self_opinion_revisions GROUP BY domain) ORDER BY domain').fetchall()
        return [dict(row) | {'basis': json.loads(row['basis_json'])} for row in rows]

    def rank(self, initiatives, *, competence=None, load=None):
        self.refresh(competence)
        initiatives = [copy(item) for item in initiatives]  # no compounding on cached engine candidates
        with closing(self.ledger._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            opinions = {row['domain']: row for row in self.opinions()}
            overrides = {row['pref_key'].removeprefix('initiative.'): row for row in self.preferences() if row['pref_key'].startswith('initiative.')}
            decisions = []
            for item in initiatives:
                domain = getattr(item.type, 'value', str(item.type))
                original = float(item.priority)
                override, opinion = overrides.get(domain), opinions.get(domain)
                weight = float(override['value']) if override else float(opinion['weight']) if opinion else 1.0
                # Due/urgent and non-research work keeps its existing urgency.
                applied = domain in DOMAINS and original < 0.9
                item.priority = round(max(0.0, min(0.89, original * weight)), 4) if applied else original
                decisions.append({'initiative_id': str(item.id), 'domain': domain, 'original_priority': original,
                    'priority': item.priority, 'weight': weight if applied else 1.0,
                    'basis': 'owner_correction' if override and applied else 'operational_judgment' if opinion and applied else 'unchanged',
                    'correction_id': override['id'] if override and applied else None,
                    'opinion_revision': opinion['id'] if opinion and applied else None})
            ranked = sorted(initiatives, key=lambda item: -item.priority)
            snapshot = {'version': VERSION, 'load': dict(load or {}), 'decisions': decisions,
                        'ordered_ids': [str(item.id) for item in ranked], 'authority_changed': False,
                        'load_coverage': 'existing initiative/project/queue probes; missing sources are not proven idle'}
            conn.execute('INSERT OR REPLACE INTO self_attention VALUES (1,?,?)', (_json(snapshot), self.clock()))
            return ranked

    def status(self):
        with closing(self.ledger._connect()) as conn:
            row = conn.execute('SELECT * FROM self_attention WHERE slot=1').fetchone()
        attention = None if row is None else json.loads(row['snapshot_json']) | {'observed_at': row['observed_at'], 'age_seconds': max(0, round(self.clock() - row['observed_at'], 1))}
        return {'kind': 'operational_working_perspective', 'preferences': self.preferences(),
                'corrections': self.preferences(history=True), 'opinions': self.opinions(), 'attention': attention}

    def brief(self):
        rows = self.opinions()
        lines = [f"Working judgment: {row['reason']} Priority weight {row['weight']:.2f}; opinion revision {row['id']}." for row in rows]
        for pref in self.preferences():
            if pref['pref_key'].startswith('initiative.'):
                lines.append(f"Owner correction: {pref['pref_key']} priority weight {pref['value']:.2f}; source turn:{pref['source_turn_id']}; overrides observational weighting.")
        state = self.status()['attention']
        if state is not None:
            lines.append(f"Last initiative ranking, {state['age_seconds']:g}s ago: " + ', '.join(state['ordered_ids'][:8]) + '. This is a decision snapshot, not current liveness.')
        if lines:
            lines.append('These are evidence-based operational judgments, not emotions, general preferences or authorization grants.')
        return '\n'.join(lines)
