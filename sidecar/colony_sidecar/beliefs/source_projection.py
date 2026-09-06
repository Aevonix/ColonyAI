"""Scoped assertion projections owned by the canonical source transaction."""
from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
import json
import logging
import time
import uuid

from .source_claims import EXTRACTION_VERSION, extract_claims, norm_value
from .source_time import MemoryTimeQuery, filter_unstructured

logger = logging.getLogger(__name__)


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS source_claim_jobs (
        turn_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
        timezone TEXT NOT NULL DEFAULT 'UTC', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
        error TEXT, model TEXT, extraction_version TEXT, lease_token TEXT NOT NULL DEFAULT '')''')
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
            conn.execute('DELETE FROM source_claims WHERE id=?', (row["id"],))
    if not retained:
        conn.execute('DELETE FROM source_claim_jobs WHERE turn_id=?', (turn_id,))
    # Supersession/retraction links on surviving claims retain IDs, not erased
    # values. Deleting a correction must never silently revive its old value.


class SourceClaimProjection:
    def __init__(self, ledger):
        self.ledger = ledger

    def _rows(self, conn, contact_id, session_id, *, turn_ids=None, message_hashes=None, ids=None,
              key=None, time_query=None, distinct_values=False, limit=256):
        from colony_sidecar.turns.idempotency import source_message_hash
        where = ["s.contact_id=?", "(s.scope='person' OR s.session_id=?)"]
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
                where += ["json_extract(c.data_json,'$.event_at')>=?", "json_extract(c.data_json,'$.event_at')<?"]
                args.extend((time_query.start, time_query.end))
            elif time_query.mode == "valid_range":
                where += ["c.valid_from IS NOT NULL", "c.valid_from<?", "(c.valid_to IS NULL OR c.valid_to>?)"]
                args.extend((time_query.end, time_query.start))
            else:
                where += ["(c.valid_from IS NULL OR c.valid_from<=?)", "(c.valid_to IS NULL OR c.valid_to>?)"]
                args.extend((time_query.start, time_query.start))
        columns = "c.*,s.contact_id,s.session_id,s.scope,s.messages_json,s.occurred_at,s.ingested_at"
        if distinct_values:
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
            data.update({key: row[key] for key in ("id", "turn_id", "message_hash", "subject_key", "predicate",
                        "valid_from", "valid_to", "superseded_by", "retracted_by")})
            data.update(observed_at=row["occurred_at"], recorded_at=row["ingested_at"])
            result.append(data)
        return result

    def prior(self, source, message, limit=16):
        words = set(norm_value(message.get("content", "")).split())
        hits = self.ledger.search_sources(message.get("content", ""), contact_id=source["contact_id"],
                                          session_id=source["session_id"], limit=10)
        turn_ids = [hit["turn_id"] for hit in hits if hit["turn_id"] != source["turn_id"]]
        with closing(self.ledger._connect()) as conn:
            rows = self._rows(conn, source["contact_id"], source["session_id"], turn_ids=turn_ids or None)
        rows = [row for row in rows if not row["superseded_by"] and not row["retracted_by"]]
        rows.sort(key=lambda row: (len(words & set(norm_value(row["evidence"]).split())), row["recorded_at"]), reverse=True)
        return rows[:limit]

    def commit(self, source, message, claims, *, model, lease_token=None):
        from colony_sidecar.turns.idempotency import source_message_hash
        message_hash = source_message_hash(source["session_id"], message)
        with closing(self.ledger._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            if lease_token is not None and not conn.execute(
                "SELECT 1 FROM source_claim_jobs WHERE turn_id=? AND status='running' AND lease_token=?",
                (source["turn_id"], lease_token)).fetchone():
                return 0
            current = conn.execute('SELECT * FROM turn_sources WHERE turn_id=?', (source["turn_id"],)).fetchone()
            if current is None or message_hash not in {
                source_message_hash(current["session_id"], m) for m in json.loads(current["messages_json"])
            }:
                return 0
            ids = list({claim["prior_claim_id"] for claim in claims if claim.get("prior_claim_id")})
            prior = {row["id"]: row for row in self._rows(conn, current["contact_id"], current["session_id"], ids=ids)}
            written = 0
            for raw in claims:
                claim = dict(raw)
                # Validation occurs against exact current message bytes again,
                # after model execution and after any concurrent source erase.
                if message["content"][claim["span_start"]:claim["span_end"]] != claim["evidence"]:
                    continue
                basis = [source["turn_id"], message_hash, claim["subject_key"], claim["predicate"], claim["value"], claim["evidence"]]
                cid = "claim:" + hashlib.sha256(json.dumps(basis, ensure_ascii=False).encode()).hexdigest()
                if conn.execute('SELECT 1 FROM source_claims WHERE id=?', (cid,)).fetchone():
                    continue
                old = prior.get(claim.get("prior_claim_id"))
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

    def finish_job(self, job, *, model=None, error=None):
        with closing(self.ledger._connect()) as conn, conn:
            if error:
                delay = min(900, 15 * 2 ** min(job["attempts"], 6))
                conn.execute("UPDATE source_claim_jobs SET status='pending',error=?,next_attempt=?,lease_until=0 WHERE turn_id=? AND lease_token=?",
                             (error, time.time() + delay, job["turn_id"], job["lease_token"]))
            else:
                conn.execute("UPDATE source_claim_jobs SET status='complete',error=NULL,model=?,extraction_version=?,lease_until=0 WHERE turn_id=? AND lease_token=?",
                             (model, EXTRACTION_VERSION, job["turn_id"], job["lease_token"]))

    async def process_one(self, router):
        job = self.claim_job()
        if job is None:
            return False
        model = None
        try:
            for message in json.loads(job["messages_json"]):
                if message.get("role") != "user":
                    continue
                claims, model = await extract_claims(router, job, message, self.prior(job, message),
                                                    timezone_name=job["timezone"])
                if model == "local_extraction_role_unavailable":
                    self.finish_job(job, error=model)
                    return True
                self.commit(job, message, claims, model=model, lease_token=job["lease_token"])
            self.finish_job(job, model=model)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.finish_job(job, error=type(exc).__name__)
            logger.warning("source claim projection deferred (%s)", type(exc).__name__)
        return True

    def status(self, contact_id):
        with closing(self.ledger._connect()) as conn:
            return [dict(row) for row in conn.execute('''SELECT j.turn_id,j.status,j.attempts,j.error,j.model,j.extraction_version,
                (SELECT count(*) FROM source_claims c WHERE c.turn_id=j.turn_id) AS claim_count
                FROM source_claim_jobs j JOIN turn_sources s ON s.turn_id=j.turn_id WHERE s.contact_id=?
                ORDER BY s.ingested_at DESC LIMIT 20''', (contact_id,))]

    def prepare_context(self, beliefs, source_hits, *, contact_id, session_id, time_query: MemoryTimeQuery):
        """Expand retrieved keys into complete scoped assertion bundles.

        Ranking chooses relevant keys. It cannot choose a winner within an
        unresolved conflict or reintroduce text from a corrected assertion.
        """
        from colony_sidecar.turns.idempotency import source_message_hash
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
                      if isinstance(message.get("content"), str) and any(
                          hit["turn_id"] == source["turn_id"] and hit["role"] == message.get("role")
                          and hit["content"] in message["content"] for hit in source_hits)}
            claims = self._rows(conn, contact_id, session_id, turn_ids=turn_ids, message_hashes=hashes, limit=512)
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
            source = sources.get(hit["turn_id"])
            removed = []
            if source:
                for message in json.loads(source["messages_json"]):
                    text = message.get("content")
                    if message.get("role") != hit["role"] or not isinstance(text, str):
                        continue
                    message_claims = by_hash.get(source_message_hash(source["session_id"], message), [])
                    # Source FTS chunks overlap. Clip exact message spans into
                    # every matching chunk occurrence; never depend on an entire
                    # corrected quotation fitting inside one retrieved chunk.
                    offset = text.find(hit["content"])
                    while offset >= 0:
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
                        retained_hits.append(dict(hit, content=fragment, excerpt_truncated=bool(removed)))
                cursor = max(cursor, end)
        bundles = []
        for key in dict.fromkeys(keys):
            with closing(self.ledger._connect()) as conn:
                group = self._rows(conn, contact_id, session_id, key=key, time_query=time_query, distinct_values=True, limit=9)
            if not group or len(group) > 8:
                continue
            group.sort(key=lambda c: (c["valid_from"] or "", c["recorded_at"], c["id"]))
            # Exact value equality only; substring containment is not agreement.
            values = {norm_value(c["value"]) for c in group}
            def overlaps(a, b):
                return (not a["valid_to"] or not b["valid_from"] or b["valid_from"] < a["valid_to"]) and (
                    not b["valid_to"] or not a["valid_from"] or a["valid_from"] < b["valid_to"])
            conflict = any(norm_value(a["value"]) != norm_value(b["value"]) and overlaps(a, b)
                           for i, a in enumerate(group) for b in group[i + 1:])
            members = [{"claim_id": c["id"], "source": "turn:" + c["turn_id"], "role": c["role"],
                        "value": c["value"], "quote": c["evidence"], "observed_at": c["observed_at"],
                        "recorded_at": c["recorded_at"], "valid_from": c["valid_from"], "valid_to": c["valid_to"],
                        "event_at": c.get("event_at"), "validity_basis": c["validity_basis"],
                        "operation": c["operation"], "prior_claim_id": c.get("prior_claim_id")}
                       for c in group]
            status = "unresolved_conflict" if conflict else ("temporal_history" if len(values) > 1 else "source_assertion")
            identifier = hashlib.sha256(json.dumps([contact_id, key, [c["id"] for c in group]]).encode()).hexdigest()
            bundles.append({"id": "assertions:" + identifier, "kind": "source_quote",
                            "source_uri": "turn:" + group[0]["turn_id"], "claim_status": status,
                            "epistemic_state": status, "atomic_evidence": True,
                            **({"validity_status": "query_time_unresolved"} if time_query.mode == "unresolved_time" else {}),
                            "contradiction_count": len(values) - 1 if conflict else 0, "relevance": 1 / (61 + len(bundles)),
                            "content": json.dumps({"subject": group[0]["subject"], "predicate": key[1],
                                                   "status": status, "assertions": members}, ensure_ascii=False)})
        return (filter_unstructured(retained_beliefs, time_query),
                bundles + filter_unstructured(source_candidates(retained_hits), time_query))


async def run_source_claim_worker(ledger, router_provider):
    """One consumer, durable jobs and leases; process loss resumes from SQLite."""
    projection = SourceClaimProjection(ledger)
    while True:
        try:
            worked = await projection.process_one(router_provider())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("source claim worker deferred (%s)", type(exc).__name__)
            worked = False
        await asyncio.sleep(.05 if worked else 2)
