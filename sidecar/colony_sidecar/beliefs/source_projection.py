"""Scoped assertion projections owned by the canonical source transaction."""
from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
import json
import logging
import time
import uuid

from .source_claims import (EXTRACTION_VERSION, admission_metadata, extract_claims,
                            extraction_diagnostics, projection_timeout_seconds, norm_value)
from .source_time import MemoryTimeQuery, filter_unstructured

logger = logging.getLogger(__name__)


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS source_claim_jobs (
        turn_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
        timezone TEXT NOT NULL DEFAULT 'UTC', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
        error TEXT, model TEXT, extraction_version TEXT, lease_token TEXT NOT NULL DEFAULT '')''')
    if 'diagnostics_json' not in {row[1] for row in conn.execute('PRAGMA table_info(source_claim_jobs)')}:
        conn.execute('ALTER TABLE source_claim_jobs ADD COLUMN diagnostics_json TEXT')
    conn.execute('''CREATE TABLE IF NOT EXISTS source_claims (
        id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, message_hash TEXT NOT NULL,
        subject_key TEXT NOT NULL, predicate TEXT NOT NULL, value_key TEXT NOT NULL, data_json TEXT NOT NULL,
        valid_from TEXT, valid_to TEXT, superseded_by TEXT, retracted_by TEXT)''')
    conn.execute('CREATE INDEX IF NOT EXISTS source_claim_turn ON source_claims(turn_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS source_claim_key ON source_claims(subject_key,predicate)')
    conn.execute('CREATE INDEX IF NOT EXISTS source_claim_message ON source_claims(message_hash)')
    conn.execute('CREATE INDEX IF NOT EXISTS source_contact_scope ON turn_sources(contact_id,scope,session_id)')


def enqueue(conn, turn_id, messages, *, scope, timezone_name=None):
    # Checkpoint history has no per-message verified speaker attribution/time.
    if scope == "person" and any(m.get("role") == "user" for m in messages):
        conn.execute('INSERT OR IGNORE INTO source_claim_jobs(turn_id,timezone) VALUES (?,?)',
                     (turn_id, timezone_name or "UTC"))


def erase_removed(conn, turn_id, session_id, retained):
    from colony_sidecar.turns.idempotency import source_message_hash
    hashes = {source_message_hash(session_id, message) for message in retained}
    rows = conn.execute('SELECT id,message_hash FROM source_claims WHERE turn_id=?', (turn_id,)).fetchall()
    for row in rows:
        if row["message_hash"] not in hashes:
            # Inherited identities point directly to their original grounded
            # claim, so erasure needs one hop and never removes raw corrections.
            conn.execute("DELETE FROM source_claims WHERE json_extract(data_json,'$.subject_basis_claim_id')=?", (row['id'],))
            conn.execute('DELETE FROM source_claims WHERE id=?', (row["id"],))
    if not retained:
        conn.execute('DELETE FROM source_claim_jobs WHERE turn_id=?', (turn_id,))
    # Supersession/retraction links on surviving claims retain IDs, not erased
    # values. Deleting a correction must never silently revive its old value.


def _subject_basis_source_sql(identifier_sql, contact_sql):
    """Shared root lifecycle eligibility; full grounding stays in subject_basis.

    Both arguments are internal SQL fragments, never user-supplied values.
    """
    return f'''FROM source_claims b
        JOIN turn_sources bs ON bs.turn_id=b.turn_id JOIN source_claim_jobs bj ON bj.turn_id=b.turn_id
        WHERE b.id={identifier_sql} AND bs.contact_id={contact_sql} AND bs.scope='person' AND bj.status='complete'
        AND NOT EXISTS (SELECT 1 FROM source_attribution_invalidations i WHERE i.source_id=bs.turn_id)
        AND NOT EXISTS (SELECT 1 FROM source_projection_erasures e WHERE e.turn_id=bs.turn_id)
        AND NOT EXISTS (SELECT 1 FROM source_annotations a,json_each(a.target_message_hashes_json) h
                        WHERE a.target_source_id=bs.turn_id AND h.value=b.message_hash)'''


def subject_basis(conn, claim, *, contact_id):
    """Return grounded identity and explicitly qualified earlier episode context.

    A root's value may be retracted or superseded while its literal subject
    remains grounded. Annotation, erasure or changed attribution revokes it.
    Only one fully grounded ancestor is allowed, not a recursive claim chain.
    An immediate episode correction can qualify the earlier report without
    withdrawing every unchanged detail. Further corrections require the existing
    history reader: the root alone does not represent intervening revisions.
    """
    identifier = claim.get('subject_basis_claim_id')
    if not identifier:
        return None
    from colony_sidecar.turns.idempotency import source_message_hash, canonical_turn_digest
    from colony_sidecar.turns.audio import claim_message
    row = conn.execute('SELECT b.*,bs.session_id,bs.messages_json,bs.occurred_at,bs.ingested_at '
        + _subject_basis_source_sql('?', '?'),
        (identifier, contact_id)).fetchone()
    if row is None:
        return None
    data = json.loads(row['data_json'])
    messages = json.loads(row['messages_json'])
    message = next((claim_message(m) for m in messages
        if source_message_hash(row['session_id'], m) == row['message_hash']), None)
    if (data.get('subject_basis_claim_id') or message is None or message.get('role') != 'user'
            or data.get('subject') != claim.get('subject') or row['subject_key'] != claim['subject_key']
            or row['predicate'] != claim['predicate'] or data['evidence'] not in message.get('content', '')
            or admission_metadata(data) is None
            or ('source_admission' in data and ('_audio_segments' in message
                or data['evidence'] != message.get('content')))):
        return None
    # Reuse the original literal-grounding rule, without inferring an alias.
    from .source_claims import literal_subject
    episode = data.get('representation') == claim.get('representation') == 'episode'
    if episode:
        if row['subject_key'] != 'episode:' + hashlib.sha256(data['evidence'].encode()).hexdigest():
            return None
    elif not literal_subject(data['subject'], data['evidence']):
        return None
    result = {'claim_id': row['id'], 'turn_id': row['turn_id'], 'message_hash': row['message_hash'],
            'source_version': canonical_turn_digest(messages),
            'disposition': 'subject_identity_only',
            'value_use': 'not_evidence_for_current_value',
            **{k: data[k] for k in ('evidence_basis', 'epistemic_state', 'source_modality') if k in data},
            'subject': data['subject'], 'predicate': row['predicate'], 'evidence': data['evidence']}
    if episode:
        immediate = claim.get('prior_claim_id') == identifier
        result.update(
            disposition='prior_episode_report' if immediate else 'episode_history_incomplete',
            value_use=(
                'Read with the current correction. Corrected or withdrawn details are not current. '
                'Unchanged details remain attributed earlier context, not newly verified facts. '
                'A withdrawal of the whole report withdraws all its details.' if immediate else
                'Intermediate corrections are absent here. Do not infer current details from this '
                'original report. Open assertion history; missing or withdrawn revisions cannot '
                'be reconstructed from the original.'),
            role='user', reported_at=row['occurred_at'], recorded_at=row['ingested_at'],
            event_at=data.get('event_at'), event_time=data.get('event_time', {
                'status': 'legacy_precision_unknown' if data.get('event_at') else 'unknown'}))
    return result


class SourceClaimProjection:
    def __init__(self, ledger):
        self.ledger = ledger

    def _rows(self, conn, contact_id, session_id, *, turn_ids=None, message_hashes=None, ids=None,
              key=None, time_query=None, distinct_values=False, limit=256):
        from colony_sidecar.turns.idempotency import source_message_hash
        where = ["s.contact_id=?", "(s.scope='person' OR s.session_id=?)",
                 "NOT EXISTS (SELECT 1 FROM source_attribution_invalidations i WHERE i.source_id=s.turn_id)"]
        args = [contact_id, session_id]
        if ids is not None:
            if not ids:
                return []
            where.append("c.id IN (" + ",".join("?" for _ in ids) + ")")
            args.extend(ids)
        if turn_ids is not None or message_hashes is not None:
            alternatives = []
            for column, values in (("c.turn_id", turn_ids), ("c.message_hash", message_hashes)):
                if values:
                    alternatives.append(column + " IN (" + ",".join("?" for _ in values) + ")")
                    args.extend(values)
            if not alternatives:
                return []
            where.append("(" + " OR ".join(alternatives) + ")")
        if key:
            where += ["c.subject_key=?", "c.predicate=?"]
            args.extend(key)
        if time_query:
            where.append("c.retracted_by IS NULL")
            if time_query.mode == "unresolved_time":
                pass  # Return labelled evidence, never certify a requested time.
            elif time_query.mode == "observed_range":
                # A source calendar day is an interval, not a midnight event.
                # Source and query timezones can give that day different UTC
                # boundaries. Retain overlap without claiming an exact instant.
                where.append("""((json_extract(c.data_json,'$.event_time.status')='resolved'
                    AND json_extract(c.data_json,'$.event_time.precision')='calendar_day'
                    AND json_extract(c.data_json,'$.event_time.start')<?
                    AND json_extract(c.data_json,'$.event_time.end_exclusive')>?)
                    OR (coalesce(json_extract(c.data_json,'$.event_time.precision'),'')!='calendar_day'
                    AND json_extract(c.data_json,'$.event_at')>=?
                    AND json_extract(c.data_json,'$.event_at')<?))""")
                args.extend((time_query.end, time_query.start, time_query.start, time_query.end))
            elif time_query.mode == "valid_range":
                where += ["c.valid_from IS NOT NULL", "c.valid_from<?", "(c.valid_to IS NULL OR c.valid_to>?)"]
                args.extend((time_query.end, time_query.start))
            else:
                where += ["(c.valid_from IS NULL OR c.valid_from<=?)", "(c.valid_to IS NULL OR c.valid_to>?)"]
                args.extend((time_query.start, time_query.start))
        columns = "c.*,s.contact_id,s.session_id,s.scope,s.messages_json,s.occurred_at,s.ingested_at"
        if distinct_values:
            # A revoked inherited claim cannot win value deduplication over
            # an independent witness. Filter lifecycle eligibility inside the
            # scoped query, preserving its value limit and the grounding
            # checks on returned rows below.
            where.append("(json_extract(c.data_json,'$.subject_basis_claim_id') IS NULL OR EXISTS (SELECT 1 "
                + _subject_basis_source_sql("json_extract(c.data_json,'$.subject_basis_claim_id')", 's.contact_id') + '))')
            columns += ",row_number() OVER (PARTITION BY c.value_key ORDER BY s.ingested_at DESC,c.id) AS value_rank"
        query = ("SELECT " + columns + " FROM source_claims c JOIN turn_sources s ON s.turn_id=c.turn_id WHERE "
                 + " AND ".join(where))
        if distinct_values:
            query = "SELECT * FROM (" + query + ") WHERE value_rank=1"
        query += " ORDER BY ingested_at DESC LIMIT ?"
        rows = conn.execute(query, [*args, limit]).fetchall()
        result, membership = [], {}
        for row in rows:
            turn = row["turn_id"]
            if turn not in membership:
                membership[turn] = {source_message_hash(row["session_id"], message)
                                    for message in json.loads(row["messages_json"])}
            if row["message_hash"] not in membership[turn]:
                continue
            data = json.loads(row["data_json"])
            if data.get('subject_basis_claim_id'):
                basis = subject_basis(conn, data, contact_id=contact_id)
                if basis is None:
                    continue
                data['subject_basis'] = basis
            data.update({key: row[key] for key in ("id", "turn_id", "message_hash", "subject_key", "predicate",
                        "valid_from", "valid_to", "superseded_by", "retracted_by")})
            data.update(observed_at=row["occurred_at"], recorded_at=row["ingested_at"])
            result.append(data)
        return result

    def prior(self, source, message, limit=16):
        words = set(norm_value(message.get("content", "")).split())

        def relevance(row):
            # A partial correction can omit every topic word in the original
            # report. Its retained, scoped basis still identifies that episode.
            basis = row.get('subject_basis', {}) if row.get('representation') == 'episode' else {}
            evidence = row['evidence'] + ' ' + basis.get('evidence', '')
            return len(words & set(norm_value(evidence).split())), row['recorded_at']

        hits = self.ledger.search_sources(message.get("content", ""), contact_id=source["contact_id"],
                                          session_id=source["session_id"], limit=10)
        turn_ids = [hit["turn_id"] for hit in hits if hit["turn_id"] != source["turn_id"]]
        with closing(self.ledger._connect()) as conn:
            rows = self._rows(conn, source["contact_id"], source["session_id"], turn_ids=turn_ids or None)
            # Relevance can find a retracted report without finding its latest
            # count-only correction. Offer the existing current episode, never
            # the stale handle. The normal row reader rechecks source scope,
            # attribution and retained basis; no erased history is reconstructed.
            keys = list(dict.fromkeys((row['subject_key'], row['predicate'])
                for row in sorted(rows, key=relevance, reverse=True)
                if row.get('representation') == 'episode'))[:limit]
            for key in keys:
                rows.extend(self._rows(conn, source['contact_id'], source['session_id'],
                    key=key, limit=limit))
        rows = list({row['id']: row for row in rows
                     if not row['superseded_by'] and not row['retracted_by']}.values())
        rows.sort(key=relevance, reverse=True)
        return rows[:limit]

    def commit(self, source, message, claims, *, model, lease_token=None):
        from colony_sidecar.turns.idempotency import source_message_hash, canonical_turn_digest
        from colony_sidecar.turns.audio import claim_message, claim_basis
        message_hash = source_message_hash(source["session_id"], message)
        with closing(self.ledger._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            if lease_token is not None and not conn.execute(
                "SELECT 1 FROM source_claim_jobs WHERE turn_id=? AND status='running' AND lease_token=?",
                (source["turn_id"], lease_token)).fetchone():
                return 0
            current = conn.execute('''SELECT * FROM turn_sources s WHERE turn_id=? AND NOT EXISTS
                (SELECT 1 FROM source_attribution_invalidations i WHERE i.source_id=s.turn_id)''',
                (source["turn_id"],)).fetchone()
            if current is None:
                return 0
            current_messages = json.loads(current['messages_json'])
            original = next((m for m in current_messages
                if source_message_hash(current['session_id'], m) == message_hash), None)
            current_view = claim_message(original) if original is not None else None
            if (current_view is None or current_view.get('content') != message.get('content')
                    or current_view.get('_audio_segments') != message.get('_audio_segments')):
                return 0
            ids = list({claim["prior_claim_id"] for claim in claims if claim.get("prior_claim_id")})
            prior = {row["id"]: row for row in self._rows(conn, current["contact_id"], current["session_id"], ids=ids)}
            written = 0
            for raw in claims:
                claim = dict(raw)
                # Validation occurs against exact current message bytes again,
                # after model execution and after any concurrent source erase.
                if current_view["content"][claim["span_start"]:claim["span_end"]] != claim["evidence"]:
                    continue
                if 'source_admission' in claim and (admission_metadata(claim) is None
                        or '_audio_segments' in current_view or claim['evidence'] != current_view['content']):
                    continue
                if '_audio_segments' in current_view:
                    basis = claim_basis(current_view, claim['span_start'], claim['span_end'])
                    review = claim.get('admission_review', {})
                    if (basis is None or basis != claim.get('evidence_basis')
                            or review.get('version') != 'source-claim-review-v1'
                            or review.get('basis') != 'model_judgment_unverified'):
                        continue
                    if not conn.execute('''SELECT 1 FROM source_media_links l JOIN source_media m USING(asset_hash)
                        WHERE l.turn_id=? AND l.message_hash=? AND l.asset_hash=?
                        AND m.mime_type='audio/wav' AND m.status='complete' ''',
                        (source['turn_id'], message_hash, basis['segment']['asset_id'][7:])).fetchone():
                        continue
                    claim.update(epistemic_state='derived_unverified', source_modality='audio_transcript',
                        evidence_basis={**basis, 'source_message_hash': message_hash,
                            'source_version_at_formation': canonical_turn_digest(current_messages)})
                basis = [source["turn_id"], message_hash, claim["subject_key"], claim["predicate"], claim["value"], claim["evidence"]]
                if claim.get('subject_basis_claim_id'):
                    basis.append(claim['subject_basis_claim_id'])
                cid = "claim:" + hashlib.sha256(json.dumps(basis, ensure_ascii=False).encode()).hexdigest()
                if conn.execute('SELECT 1 FROM source_claims WHERE id=?', (cid,)).fetchone():
                    continue
                old = prior.get(claim.get("prior_claim_id"))
                if claim.get('subject_basis_claim_id'):
                    # Re-read current status even when another candidate in
                    # this same batch already corrected the supplied prior.
                    live = self._rows(conn, current['contact_id'], current['session_id'],
                                      ids=[claim.get('prior_claim_id')])
                    old = live[0] if live else None
                    if (old is None or old['subject'] != claim['subject']
                            or old['subject_key'] != claim['subject_key'] or old['predicate'] != claim['predicate']
                            or old['superseded_by'] or old['retracted_by']
                            or claim['operation'] not in {'correct', 'change'}
                            or admission_metadata(claim) is None
                            or claim['subject_basis_claim_id'] != (old.get('subject_basis_claim_id') or old['id'])
                            or subject_basis(conn, claim, contact_id=current['contact_id']) is None):
                        continue  # A missing dependency cannot become assert.
                if old and (old["subject_key"] != claim["subject_key"] or old["predicate"] != claim["predicate"]
                            or old["superseded_by"] or old["retracted_by"]):
                    old = None
                if not old:
                    claim["operation"] = "assert"
                claim.update(model=model, extraction_version=EXTRACTION_VERSION, role="user")
                conn.execute('''INSERT INTO source_claims
                    (id,turn_id,message_hash,subject_key,predicate,value_key,data_json,valid_from,valid_to)
                    VALUES (?,?,?,?,?,?,?,?,?)''', (cid, source["turn_id"], message_hash,
                    claim["subject_key"], claim["predicate"], norm_value(claim["value"]), json.dumps(claim, ensure_ascii=False),
                    claim["valid_from"], claim["valid_to"]))
                if old and norm_value(old["value"]) != norm_value(claim["value"]):
                    if claim["operation"] == "correct":
                        conn.execute('UPDATE source_claims SET retracted_by=? WHERE id=?', (cid, old["id"]))
                    elif claim["operation"] == "change" and claim["valid_from"]:
                        if not old["valid_from"] or old["valid_from"] < claim["valid_from"]:
                            conn.execute('UPDATE source_claims SET valid_to=?,superseded_by=? WHERE id=?',
                                         (claim["valid_from"], cid, old["id"]))
                written += 1
            return written

    def claim_job(self):
        now = time.time()
        with closing(self.ledger._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            job = conn.execute('''SELECT j.*,s.contact_id,s.session_id,s.scope,s.messages_json,s.occurred_at,s.ingested_at
                FROM source_claim_jobs j JOIN turn_sources s ON s.turn_id=j.turn_id
                WHERE (j.status='pending' AND j.next_attempt<=?) OR (j.status='running' AND j.lease_until<=?)
                ORDER BY s.ingested_at LIMIT 1''', (now, now)).fetchone()
            if job is None:
                return None
            token = uuid.uuid4().hex
            conn.execute("UPDATE source_claim_jobs SET status='running',attempts=attempts+1,lease_until=?,lease_token=? WHERE turn_id=?",
                         (now + 60, token, job["turn_id"]))
            return dict(job, lease_token=token)

    def finish_job(self, job, *, model=None, error=None, diagnostics=None):
        # claim_job returns the pre-increment count. Older workers can leave
        # this additive column untouched; never attribute their later attempt
        # to measurements produced by this worker.
        encoded = json.dumps(dict(diagnostics, attempt=job['attempts'] + 1), sort_keys=True) if diagnostics is not None else None
        with closing(self.ledger._connect()) as conn, conn:
            if error:
                delay = min(900, 15 * 2 ** min(job["attempts"], 6))
                conn.execute("UPDATE source_claim_jobs SET status='pending',error=?,next_attempt=?,lease_until=0,diagnostics_json=? WHERE turn_id=? AND lease_token=?",
                             (error, time.time() + delay, encoded, job["turn_id"], job["lease_token"]))
            else:
                conn.execute("UPDATE source_claim_jobs SET status='complete',error=NULL,model=?,extraction_version=?,lease_until=0,diagnostics_json=? WHERE turn_id=? AND lease_token=?",
                             (model, EXTRACTION_VERSION, encoded, job["turn_id"], job["lease_token"]))

    def renew_job(self, job, request_timeout):
        """Extend this lease for bounded extraction, review and their commit."""
        with closing(self.ledger._connect()) as conn, conn:
            updated = conn.execute('''UPDATE source_claim_jobs SET lease_until=?
                WHERE turn_id=? AND status='running' AND lease_token=?''',
                (time.time() + request_timeout + 30, job['turn_id'], job['lease_token']))
            return updated.rowcount == 1

    async def process_one(self, router):
        job = self.claim_job()
        if job is None:
            return False
        model = None
        diagnostics = extraction_diagnostics()
        try:
            for message in json.loads(job["messages_json"]):
                # Retained ASR has a deterministic derived view. Other media
                # remains in its existing caption/source recall path.
                from colony_sidecar.turns.audio import claim_message
                message = claim_message(message)
                if message is None:
                    continue
                content = message.get('content')
                if message.get("role") != "user" or not isinstance(content, str) or not content.strip():
                    continue
                request_timeout = projection_timeout_seconds(router)
                if not self.renew_job(job, request_timeout):
                    return True  # Forgotten or reclaimed; do not start a stale model call.
                claims, model = await extract_claims(router, job, message, self.prior(job, message),
                                                    timezone_name=job["timezone"], request_timeout=request_timeout,
                                                    diagnostics=diagnostics)
                if model == "local_extraction_role_unavailable":
                    self.finish_job(job, error=model, diagnostics=diagnostics)
                    return True
                self.commit(job, message, claims, model=model, lease_token=job["lease_token"])
            self.finish_job(job, model=model, diagnostics=diagnostics)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.finish_job(job, error=type(exc).__name__, diagnostics=diagnostics)
            logger.warning("source claim projection deferred (%s)", type(exc).__name__)
        return True

    def status(self, contact_id):
        with closing(self.ledger._connect()) as conn:
            rows = [dict(row) for row in conn.execute('''SELECT j.turn_id,j.status,j.attempts,j.error,j.model,j.extraction_version,j.diagnostics_json,
                (SELECT count(*) FROM source_claims c WHERE c.turn_id=j.turn_id) AS claim_count
                FROM source_claim_jobs j JOIN turn_sources s ON s.turn_id=j.turn_id WHERE s.contact_id=?
                ORDER BY s.ingested_at DESC LIMIT 20''', (contact_id,))]
        for row in rows:
            encoded = row.pop('diagnostics_json')
            data = json.loads(encoded) if encoded is not None else None
            row['diagnostics'] = data if data is not None and data['attempt'] == row['attempts'] else None
        return rows

    def preferences(self, contact_id, session_id='', *, now=None, limit=20):
        """Read admitted speaker preferences without authoring another profile.

        Keep the full quotation, including a recurring activity or condition.
        A correction makes an interpretation unsuitable as automatic guidance;
        ordinary source recall still presents the original and its annotation.
        """
        from datetime import datetime, timezone
        from colony_sidecar.turns.idempotency import canonical_turn_digest
        stamp = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).isoformat()
        with closing(self.ledger._connect()) as conn:
            ids = [r[0] for r in conn.execute('''SELECT c.id FROM source_claims c
                JOIN turn_sources s ON s.turn_id=c.turn_id
                WHERE s.contact_id=? AND (s.scope='person' OR s.session_id=?)
                AND c.subject_key='speaker' AND c.superseded_by IS NULL AND c.retracted_by IS NULL
                AND json_extract(c.data_json,'$.memory_quality.memory_kind')='preference'
                AND json_extract(c.data_json,'$.admission_review.version')='source-claim-review-v1'
                AND json_extract(c.data_json,'$.admission_review.basis')='model_judgment_unverified'
                AND (c.valid_from IS NULL OR c.valid_from<=?) AND (c.valid_to IS NULL OR c.valid_to>?)
                AND NOT EXISTS (SELECT 1 FROM source_projection_erasures e WHERE e.turn_id=s.turn_id)
                AND NOT EXISTS (SELECT 1 FROM source_attribution_invalidations i WHERE i.source_id=s.turn_id)
                ORDER BY s.ingested_at DESC,c.id LIMIT ?''',
                (contact_id, session_id, stamp, stamp, max(1, min(limit, 100))))]
            result = []
            for claim in self._rows(conn, contact_id, session_id, ids=ids, limit=len(ids) or 1):
                annotations = conn.execute('SELECT target_message_hashes_json FROM source_annotations WHERE target_source_id=?',
                                           (claim['turn_id'],)).fetchall()
                if any(claim['message_hash'] in json.loads(r[0]) for r in annotations):
                    continue
                source = conn.execute('SELECT session_id,messages_json FROM turn_sources WHERE turn_id=?', (claim['turn_id'],)).fetchone()
                refs = [{
                    'source_id': claim['turn_id'], 'source_contact_id': contact_id,
                    'source_version': canonical_turn_digest(json.loads(source['messages_json'])),
                    'message_hash': claim['message_hash']}]
                if claim.get('subject_basis'):
                    basis = claim['subject_basis']
                    refs.append({'source_id': basis['turn_id'], 'source_contact_id': contact_id,
                                 'source_version': basis['source_version'], 'message_hash': basis['message_hash']})
                result.append({**claim, 'session_id': source['session_id'], 'sources': refs})
        return result

    def _current_work_reply(self, conn, source, message, *, contact_id, session_id):
        """Resolve one exact status request through existing canonical lineage.

        A native answer is often stored separately from its supplied user input.
        Unknown, corrected, changed, multiple-input and cross-scope provenance
        remains recallable. This transient hint never changes stored history.
        """
        from colony_sidecar.intelligence.graph.selection import current_work_query
        from colony_sidecar.turns.idempotency import canonical_turn_digest, source_message_hash
        if message.get('role') != 'assistant' or source['scope'] != 'person':
            return False
        refs = message.get('_supplied_inputs')
        if refs is not None:
            if not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], dict):
                return False
            ref = refs[0]
            if set(ref) != {'source_id', 'input_message_hash'}:
                return False
            parent = conn.execute('''SELECT * FROM turn_sources WHERE turn_id=? AND contact_id=?
                AND (scope='person' OR session_id=?) AND NOT EXISTS (
                    SELECT 1 FROM source_attribution_invalidations i WHERE i.source_id=turn_sources.turn_id)
                ''', (ref['source_id'], contact_id, session_id)).fetchone()
            if parent is None or parent['scope'] != 'person':
                return False
            messages = json.loads(parent['messages_json'])
            if {'source_id': parent['turn_id'], 'source_version': canonical_turn_digest(messages)} \
                    not in message.get('_supplied_sources', []):
                return False
            matches = [m for m in messages if m.get('role') == 'user'
                       and source_message_hash(parent['session_id'], m) == ref['input_message_hash']]
            if len(matches) != 1:
                return False
            request = matches[0]
        else:
            # Ordinary paired turns can carry the request in the same envelope.
            messages = json.loads(source['messages_json'])
            indices = [i for i, m in enumerate(messages) if m == message]
            if len(indices) != 1 or indices[0] == 0:
                return False
            parent, request = source, messages[indices[0] - 1]
            if request.get('role') != 'user':
                return False
        request_hash = source_message_hash(parent['session_id'], request)
        annotations = conn.execute('SELECT target_message_hashes_json FROM source_annotations WHERE target_source_id=?',
                                   (parent['turn_id'],)).fetchall()
        if any(request_hash in json.loads(row[0]) for row in annotations):
            return False
        return current_work_query(request.get('content'))

    def prepare_context(self, beliefs, source_hits, *, contact_id, session_id, time_query: MemoryTimeQuery,
                        classify_work_replies=False):
        """Expand retrieved keys into complete scoped assertion bundles.

        Ranking chooses relevant keys. It cannot choose a winner within an
        unresolved conflict or reintroduce text from a corrected assertion.
        """
        from colony_sidecar.turns.idempotency import source_message_hash
        from colony_sidecar.turns.audio import source_text
        from colony_sidecar.intelligence.graph.recall import source_candidates
        turn_ids = list(dict.fromkeys(
            [row["turn_id"] for row in source_hits] + [str(row["source_uri"])[5:] for row in beliefs
             if str(row.get("source_uri") or "").startswith("turn:")]))
        if not turn_ids:
            return filter_unstructured(beliefs, time_query), []
        with closing(self.ledger._connect()) as conn:
            sources = {row["turn_id"]: dict(row) for row in conn.execute(
                "SELECT * FROM turn_sources WHERE contact_id=? AND (scope='person' OR session_id=?) AND turn_id IN ("
                + ",".join("?" for _ in turn_ids) + ")", (contact_id, session_id, *turn_ids))}
            hashes = {source_message_hash(source["session_id"], message)
                      for source in sources.values() for message in json.loads(source["messages_json"])
                      if any(
                          hit["turn_id"] == source["turn_id"] and hit["role"] == message.get("role")
                          and hit["content"] in source_text(message.get('content')) for hit in source_hits)}
            claims = self._rows(conn, contact_id, session_id, turn_ids=turn_ids, message_hashes=hashes, limit=512)
            status_replies = set()
            if classify_work_replies:
                # Resolve only exact retrieved messages, never every assistant
                # message in an otherwise long canonical checkpoint.
                pending = {(hit['turn_id'], hit.get('source_message_hash')) for hit in source_hits}
                for source in sources.values():
                    for message in json.loads(source['messages_json']):
                        identity = (source['turn_id'], source_message_hash(source['session_id'], message))
                        if identity not in pending:
                            continue
                        pending.discard(identity)
                        if self._current_work_reply(conn, source, message,
                                                    contact_id=contact_id, session_id=session_id):
                            status_replies.add(identity)
        by_turn, by_hash = {}, {}
        for claim in claims:
            by_turn.setdefault(claim["turn_id"], []).append(claim)
            by_hash.setdefault(claim["message_hash"], []).append(claim)
        keys, retained_beliefs = [], []
        for original in beliefs:
            row = dict(original)
            uri = str(row.get("source_uri") or "")
            turn = uri[5:] if uri.startswith("turn:") else None
            known = by_turn.get(turn, [])
            if known:
                keys.extend((c["subject_key"], c["predicate"]) for c in known)
                continue
            if turn in sources:
                row["occurred_at"] = sources[turn]["occurred_at"]
            retained_beliefs.append(row)
        retained_hits = []
        for original in source_hits:
            hit = dict(original)
            hit.pop('_current_work_status_reply', None)
            source = sources.get(hit["turn_id"])
            removed = []
            if source:
                # Carry canonical attribution through selection. Checkpoint
                # scope cannot attest the speaker of every historical message.
                hit.update({name: source[name] for name in ("contact_id", "session_id", "scope")})
                for message in json.loads(source["messages_json"]):
                    if hit.get('source_message_hash') and hit['source_message_hash'] != source_message_hash(source['session_id'], message):
                        continue
                    text = message.get("content")
                    if isinstance(text, list):
                        text = source_text(text)
                    if message.get("role") != hit["role"] or not isinstance(text, str):
                        continue
                    if ((source['turn_id'], source_message_hash(source['session_id'], message)) in status_replies
                            and hit.get('source_message_hash') == source_message_hash(source['session_id'], message)):
                        hit['_current_work_status_reply'] = True
                    message_claims = by_hash.get(source_message_hash(source["session_id"], message), [])
                    # Source FTS chunks overlap. Clip exact message spans into
                    # every matching chunk occurrence; never depend on an entire
                    # corrected quotation fitting inside one retrieved chunk.
                    offset = text.find(hit["content"])
                    if offset >= 0 and hit["content"] != text:
                        hit["excerpt_truncated"] = True
                    while offset >= 0:
                        procedures = [c for c in message_claims
                            if c.get('memory_quality', {}).get('memory_kind') == 'procedure']
                        if procedures:
                            # Its unclaimed remainder may also contain a
                            # required condition. Select the owning message,
                            # never an independently ranked leftover fragment.
                            keys.extend((c['subject_key'], c['predicate']) for c in procedures)
                            removed.append((0, len(hit['content'])))
                        for claim in message_claims:
                            start = max(0, claim["span_start"] - offset)
                            end = min(len(hit["content"]), claim["span_end"] - offset)
                            if start < end:
                                keys.append((claim["subject_key"], claim["predicate"]))
                                removed.append((start, end))
                        offset = text.find(hit["content"], offset + 1)
            cursor = 0
            # Each remainder is still one exact contiguous source quotation.
            # Concatenating separated fragments would fabricate a quotation.
            for start, end in sorted(removed) + [(len(hit["content"]), len(hit["content"]))]:
                if start > cursor:
                    fragment = hit["content"][cursor:start]
                    if fragment.strip(" .,:;\n\t"):
                        retained_hits.append(dict(hit, content=fragment,
                            excerpt_truncated=bool(removed) or bool(hit.get("excerpt_truncated"))))
                cursor = max(cursor, end)
        bundles, message_contexts, emitted_messages, groups = [], {}, set(), {}
        unresolved_time_keys = set()

        def current_group(key):
            if key not in groups:
                with closing(self.ledger._connect()) as conn:
                    groups[key] = self._rows(conn, contact_id, session_id, key=key,
                        time_query=time_query, distinct_values=True, limit=9)
                    if time_query.mode == 'observed_range' and any(
                            c.get('event_time', {}).get('precision') == 'calendar_day'
                            and (c['event_time']['start'] < time_query.start
                                 or c['event_time']['end_exclusive'] > time_query.end)
                            for c in groups[key]):
                        # The event could fall in the overlapping part or the
                        # remainder of its source day. Relevance is not proof
                        # that it happened within the query's narrower window.
                        unresolved_time_keys.add(key)
                    if not groups[key] and time_query.mode == 'observed_range':
                        # Relevance already selected this source. An unknown
                        # episode time may help answer a date question, but it
                        # cannot certify a matching date. Never admit a known
                        # event outside the requested interval through here.
                        groups[key] = [c for c in self._rows(conn, contact_id, session_id, key=key,
                            time_query=MemoryTimeQuery(), distinct_values=True, limit=9)
                            if c.get('representation') == 'episode' and not c.get('event_at')]
                        if groups[key]:
                            unresolved_time_keys.add(key)
            return groups[key]

        def complete_message_context(claim):
            """Keep a current short message or a procedure's conditions together.

            Keep the owning message together when it is still current. A
            changed message needs the existing source/history readers instead
            of reviving obsolete instructions from its original quotation.
            """
            procedure = claim.get('memory_quality', {}).get('memory_kind') == 'procedure'
            identity = (claim['turn_id'], claim['message_hash'])
            if identity not in message_contexts:
                with closing(self.ledger._connect()) as conn:
                    source = conn.execute('''SELECT * FROM turn_sources WHERE turn_id=? AND contact_id=?
                        AND (scope='person' OR session_id=?)''',
                        (claim['turn_id'], contact_id, session_id)).fetchone()
                    message = next((m for m in json.loads(source['messages_json'])
                        if source_message_hash(source['session_id'], m) == claim['message_hash']), None) if source else None
                    ids = [row['id'] for row in conn.execute(
                        'SELECT id FROM source_claims WHERE turn_id=? AND message_hash=? LIMIT 513', identity)]
                    all_claims = self._rows(conn, contact_id, session_id, ids=ids, limit=513)
                    current = self._rows(conn, contact_id, session_id, ids=ids,
                        time_query=time_query, limit=513)
                text = source_text(message.get('content')) if message else ''
                complete = bool(text and all_claims and len(ids) < 513 and len(all_claims) == len(ids)
                    and {c['id'] for c in all_claims} == {c['id'] for c in current}
                    and not any(c['superseded_by'] or c['retracted_by'] for c in all_claims)
                    and all(len(current_group((c['subject_key'], c['predicate']))) == 1 for c in all_claims))
                message_contexts[identity] = {'source': dict(source) if source else None,
                    'message': message, 'text': text, 'complete': complete, 'claims': all_claims}
            value = message_contexts[identity]
            if not procedure:
                # An unresolved time expression cannot certify a dated fact,
                # but must not strip conditions from its attributed source.
                # The bundle retains query_time_unresolved in that case.
                # Resolved historical windows still use qualified assertions.
                if (not value['complete'] or len(value['text']) > 2000
                        or time_query.mode not in {'current', 'unresolved_time'}
                        # Derived media needs its exact segment/recognizer basis;
                        # a transcript's display prefix is not missing context.
                        or any(c.get('evidence_basis') for c in value['claims'])
                        or all(c['evidence'].strip() == value['text'].strip() for c in value['claims'])):
                    return None
                return value
            # A complete one-message assertion keeps its existing property
            # semantics. This boundary concerns partial-message procedures.
            if value['complete'] and claim['evidence'].strip() == value['text'].strip():
                return None
            return value

        for key in dict.fromkeys(keys):
            group = current_group(key)
            if not group:
                continue
            overflow = len(group) > 8
            group.sort(key=lambda c: (c["valid_from"] or "", c["recorded_at"], c["id"]))
            # Exact value equality only; substring containment is not agreement.
            values = {norm_value(c["value"]) for c in group}
            def overlaps(a, b):
                return (not a["valid_to"] or not b["valid_from"] or b["valid_from"] < a["valid_to"]) and (
                    not b["valid_to"] or not a["valid_from"] or a["valid_from"] < b["valid_to"])
            conflict = any(norm_value(a["value"]) != norm_value(b["value"]) and overlaps(a, b)
                           for i, a in enumerate(group) for b in group[i + 1:])
            members = [{"claim_id": c["id"], "source": "turn:" + c["turn_id"],
                        "source_message_hash": c["message_hash"], "role": c["role"],
                        "value": c["value"], "quote": c["evidence"], "observed_at": c["observed_at"],
                        "recorded_at": c["recorded_at"], "valid_from": c["valid_from"], "valid_to": c["valid_to"],
                        "event_at": c.get("event_at"), "event_time": c.get("event_time", {
                            "status": "legacy_precision_unknown" if c.get("event_at") else "unknown"}),
                        "reported_at": c["observed_at"], "validity_basis": c["validity_basis"],
                        "operation": c["operation"], "prior_claim_id": c.get("prior_claim_id"),
                        **{k: c[k] for k in ('representation', 'evidence_basis', 'epistemic_state', 'source_modality', 'subject_basis') if k in c}}
                       for c in group]
            status = "unresolved_conflict" if conflict else ("temporal_history" if len(values) > 1 else "source_assertion")
            source_refs = [(c['turn_id'], c['message_hash']) for c in group]
            source_refs.extend((c['subject_basis']['turn_id'], c['subject_basis']['message_hash'])
                               for c in group if c.get('subject_basis'))
            identifier = hashlib.sha256(json.dumps([contact_id, key, [c["id"] for c in group]]).encode()).hexdigest()
            bundle = {"id": "assertions:" + identifier, "kind": "source_quote",
                            "history_anchor": {"source_id": group[0]['turn_id'], "claim_id": group[0]['id']},
                            "source_turn_ids": list(dict.fromkeys(turn for turn, _ in source_refs)),
                            "_source_message_hashes": {turn: list(dict.fromkeys(
                                message for source, message in source_refs if source == turn))
                                for turn, _ in source_refs},
                            "source_uri": "turn:" + group[0]["turn_id"], "claim_status": status,
                            "epistemic_state": ('derived_unverified' if any(c.get('evidence_basis') for c in group) else status),
                            **({'source_modality': 'audio_transcript'} if any(c.get('evidence_basis') for c in group) else {}),
                            "atomic_evidence": True,
                            "content_format": "source_assertions_v1",
                            # Rank the grounded language the user supplied.
                            # Administrative IDs/timestamps in the output JSON
                            # are provenance, not the passage's semantic topic.
                            # Conflicting peers remain one indivisible candidate.
                            "ranking_text": "\n".join(dict.fromkeys(c["evidence"] for c in group)),
                            **({"validity_status": "query_time_unresolved"}
                               if time_query.mode == "unresolved_time" or key in unresolved_time_keys else {}),
                            "contradiction_count": len(values) - 1 if conflict else 0, "relevance": 1 / (61 + len(bundles)),
                            "content": json.dumps({"subject": group[0]["subject"], "predicate": key[1],
                                                   "status": status, "assertions": members}, ensure_ascii=False),
                            **({"excerpt_truncated": True, "content": json.dumps({
                                "subject": group[0]['subject'], "predicate": key[1],
                                "status": "incomplete_assertion_history", "distinct_values_at_least": len(values),
                                "instruction": "Open assertion history before resolving this property; no value selected."},
                                ensure_ascii=False)} if overflow else {})}
            contexts = [(claim, complete_message_context(claim)) for claim in group]
            contexts = [(claim, context) for claim, context in contexts if context is not None]
            if contexts:
                bundle['source_anchors'] = [{'source_id': turn} for turn in
                    dict.fromkeys(claim['turn_id'] for claim in group)]
                if len(group) == 1 and contexts[0][1]['complete']:
                    claim, context = contexts[0]
                    identity = (claim['turn_id'], claim['message_hash'])
                    if identity in emitted_messages:
                        continue
                    emitted_messages.add(identity)
                    source, message = context['source'], context['message']
                    procedure = claim.get('memory_quality', {}).get('memory_kind') == 'procedure'
                    bundle.update(id=('procedure-source:' if procedure else 'message-source:') + hashlib.sha256(json.dumps(identity).encode()).hexdigest(),
                        content=context['text'], ranking_text=context['text'],
                        **({'procedure_context': 'complete_source_message_text'} if procedure else {
                            'source_context': 'complete_source_message_text',
                            'source_history_anchors': [{'source_id': c['turn_id'], 'claim_id': c['id']}
                                for c in context['claims']]}),
                        claim_status='source_procedure_context' if procedure else 'source_message_context',
                        epistemic_state='derived_unverified' if claim.get('evidence_basis') else 'quotation',
                        source_turn_id=claim['turn_id'], source_message_hash=claim['message_hash'],
                        role=message['role'], contact_id=source['contact_id'], session_id=source['session_id'],
                        scope=source['scope'], occurred_at=source['occurred_at'], ingested_at=source['ingested_at'])
                    # Content now holds raw source text, which can itself look
                    # like JSON. It is no longer an internal assertion card.
                    bundle.pop('content_format', None)
                    derived = [{'claim_id': c['id'], 'evidence_basis': c['evidence_basis']}
                               for c in context['claims'] if c.get('evidence_basis')]
                    if derived:
                        bundle.update(source_evidence_bases=derived, epistemic_state='derived_unverified',
                                      source_modality='audio_transcript')
                    # Other represented properties can inherit an exact subject
                    # from another source. Retain every opening dependency.
                    for represented in context['claims']:
                        basis = represented.get('subject_basis')
                        if basis:
                            if basis['turn_id'] not in bundle['source_turn_ids']:
                                bundle['source_turn_ids'].append(basis['turn_id'])
                            hashes = bundle['_source_message_hashes'].setdefault(basis['turn_id'], [])
                            if basis['message_hash'] not in hashes:
                                hashes.append(basis['message_hash'])
                    bundle['source_anchors'] = [{'source_id': turn} for turn in bundle['source_turn_ids']]
                else:
                    # A conflicting or changed procedure remains discoverable,
                    # but its partial assertion is not sufficient instructions.
                    # Opening only the selected property's history could miss
                    # the sibling whose correction made this message unsafe.
                    anchors = {(c['turn_id'], c['subject_key'], c['predicate']): {
                        'source_id': c['turn_id'], 'claim_id': c['id']}
                        for _, context in contexts for c in context['claims']}
                    bundle.update(procedure_context='full_source_required', excerpt_truncated=True,
                        procedure_history_anchors=list(anchors.values()),
                        content='Incomplete procedure context. Open the full sources in source_anchors '
                            'using their recalled source versions and inspect each procedure_history_anchors entry before '
                            'following the procedure. Property history alone may omit its conditions.')
            bundles.append(bundle)
        # A complete selected message already includes its unclaimed remainder.
        # Do not rank those fragments independently against their own context.
        retained_hits = [hit for hit in retained_hits
            if (hit['turn_id'], hit.get('source_message_hash')) not in emitted_messages]
        return (filter_unstructured(retained_beliefs, time_query),
                bundles + filter_unstructured(source_candidates(retained_hits), time_query))


async def run_source_claim_worker(ledger, router_provider, *, claims_enabled=True):
    """One consumer, durable jobs and leases; process loss resumes from SQLite."""
    projection = SourceClaimProjection(ledger)
    from colony_sidecar.identity import get_owner_contact_id
    from colony_sidecar.self_model.judgments import SelfJudgments
    judgments = SelfJudgments(ledger, owner_id=get_owner_contact_id())
    from colony_sidecar.self_model.appraisals import AppraisalStore
    appraisals = AppraisalStore(ledger, owner_id=get_owner_contact_id())
    from colony_sidecar.turns.media import SourceMedia
    media = SourceMedia(ledger)
    from colony_sidecar.turns.source_vectors import SourceVectors
    from colony_sidecar.vector import get_store, get_pipeline
    vectors = SourceVectors(ledger, get_store(), get_pipeline())
    vectors.backfill()
    try:
        media.recover_unowned_files()
    except OSError:
        logger.warning("source media orphan recovery deferred")
    reflections = {'judgment': judgments, 'appraisal': appraisals, 'claim': projection}
    reflection_tasks = {name: None for name in reflections}
    next_identity_check = 0.0
    try:
        while True:
            worked = False
            if time.monotonic() >= next_identity_check:
                next_identity_check = time.monotonic() + 30
                try:
                    from colony_sidecar.api.routers.social_state import reconcile_pending_identities
                    worked = await reconcile_pending_identities(ledger)
                except Exception as exc:
                    logger.warning('identity source reconciliation deferred (%s)', type(exc).__name__)
            # Durable model projections share this worker's lifecycle. Their
            # requests must not stall source indexing or media processing.
            if claims_enabled:
                for name, projection_worker in reflections.items():
                    task = reflection_tasks[name]
                    if task is None or task.done():
                        if task is not None:
                            try:
                                worked = task.result() or worked
                            except Exception as exc:
                                logger.warning("source %s deferred (%s)", name, type(exc).__name__)
                        reflection_tasks[name] = asyncio.create_task(
                            projection_worker.process_one(router_provider()))
            try:
                worked = await vectors.process_one() or worked
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("source semantic projection deferred (%s)", type(exc).__name__)
            try:
                if claims_enabled:
                    media_worked = await media.process_one(router_provider())
                    worked = worked or media_worked
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("source claim worker deferred (%s)", type(exc).__name__)
            await asyncio.sleep(.05 if worked else 2)
    finally:
        tasks = [task for task in reflection_tasks.values() if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
