"""Cross-channel communication ledger + outreach governance.

Gives the agent the WHOLE picture of its relationship traffic with a contact —
every inbound/outbound exchange across every channel (WhatsApp now, email/SMS
later) — and a principled decision on whether / how / when to (re)initiate, so it
never spams and always references the last discussion + open follow-ups before
reaching out. Proactive outreach to anyone but the owner is gated on owner
approval by policy.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pacomind.tom.source_lineage import SourceLinkedStore

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


class CommsLog(SourceLinkedStore):
    """SQLite ledger of communications with each contact, across all channels."""

    def __init__(self, db_path: str, *, source_ledger=None) -> None:
        self._db_path = str(db_path)
        self._source_ledger = source_ledger
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS communications (
                id TEXT PRIMARY KEY,
                contact_id TEXT NOT NULL,
                channel TEXT NOT NULL DEFAULT 'unknown',
                direction TEXT NOT NULL,        -- 'in' (they->us) | 'out' (us->them)
                summary TEXT,
                session_id TEXT,
                ts TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_comms_contact ON communications(contact_id, ts);
            """
        )
        existing = {str(row[1]) for row in self._conn.execute(
            "PRAGMA table_info(communications)").fetchall()}
        for name, sql_type in {
            "external_ref": "TEXT",
            "reply_to_ref": "TEXT",
            "reaction": "TEXT",
            "receipt_ref": "TEXT",
            "outbound_ref": "TEXT",
            "source_lineage_json": "TEXT",
        }.items():
            if name not in existing:
                self._conn.execute(
                    f"ALTER TABLE communications ADD COLUMN {name} {sql_type}")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_comms_reply_ref "
            "ON communications(contact_id,reply_to_ref,ts)")
        self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_comms_source_lineage
            ON communications(contact_id,json_extract(source_lineage_json,'$.turn_id'))
            WHERE source_lineage_json IS NOT NULL""")
        self._conn.commit()
        from .transport_ingress import ensure_schema
        ensure_schema(self._conn)

    def _ledger(self):
        # An offline copy can belong to another profile. Missing local source
        # rows are not evidence that its linked communications were erased.
        if self._source_ledger is None:
            raise ValueError('communications_source_ledger_required')
        return self._source_ledger

    def _reconcile_erasure_ids(self, contact_id=None) -> None:
        """Read existing tombstone IDs, not every original conversation body."""
        where, params = (' AND contact_id=?', (contact_id,)) if contact_id else ('', ())
        if self._conn.execute('SELECT 1 FROM communications WHERE source_lineage_json IS NOT NULL'
                              + where + ' LIMIT 1', params).fetchone() is None:
            return
        condition = ' WHERE e.contact_id=?' if contact_id else ''
        with closing(self._ledger()._connect()) as conn:
            erased = conn.execute('''SELECT e.contact_id,coalesce(r.source_turn_id,e.turn_id)
                FROM source_erasures e LEFT JOIN source_erasure_revisions r USING(sequence)'''
                + condition + ''' UNION SELECT e.contact_id,p.turn_id FROM source_erasures e
                LEFT JOIN source_erasure_revisions r USING(sequence)
                JOIN source_projection_erasures p ON p.source_turn_id=coalesce(r.source_turn_id,e.turn_id)'''
                + condition, params + params).fetchall()
        with self._conn:
            self._conn.executemany('''DELETE FROM communications WHERE contact_id=?
                AND source_lineage_json IS NOT NULL
                AND json_extract(source_lineage_json,'$.turn_id')=?''', [tuple(row) for row in erased])

    def _summary_rows(self, query, params, fields, *, contact_id=None):
        self._reconcile_erasure_ids(contact_id)
        rows = self._conn.execute(query, params).fetchall()
        invalid = self._invalid_sources([row for row in rows if row['source_lineage_json'] is not None])
        rejected = {row['id'] for row in invalid}
        with self._conn:
            self._conn.executemany('DELETE FROM communications WHERE id=?', [(key,) for key in rejected])
        return [{key: row[key] for key in fields} for row in rows if row['id'] not in rejected]

    def purge_erased_sources(self, turn_ids=None, *, contact_id=None) -> int:
        sql = ('SELECT id,contact_id,source_lineage_json FROM communications '
               'WHERE source_lineage_json IS NOT NULL')
        rows = self._conn.execute(sql + (' AND contact_id=?' if contact_id else ''),
                                  (contact_id,) if contact_id else ()).fetchall()
        invalid = self._invalid_sources(rows, turn_ids)
        with self._conn:
            self._conn.executemany('DELETE FROM communications WHERE id=?', [(row['id'],) for row in invalid])
        return len(invalid)

    def unlinked_summary_count(self, contact_id: str) -> int:
        """Historical/unattributed prose cannot be erased by guessing its source."""
        return self._conn.execute('''SELECT count(*) FROM communications WHERE contact_id=?
            AND source_lineage_json IS NULL AND coalesce(summary,'')!=''
            AND coalesce(receipt_ref,'')='' ''', (contact_id,)).fetchone()[0]

    def read_connection(self):
        """An independent read connection for projections running on a worker thread.

        The caller closes it. The transport writer's thread-bound connection
        and transaction ownership remain unchanged.
        """
        db = sqlite3.connect(Path(self._db_path).resolve().as_uri()+'?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        return db

    def log_receipt(self, *, event_id, contact_id, channel, direction, external_ref,
                    receipt_ref, occurred_at, status, reply_to_ref='', outbound_ref=''):
        """Idempotent metadata from a trusted transport adapter, never prose."""
        import hashlib
        stamp = _parse(occurred_at)
        if stamp is None or stamp > _now() or direction not in {'in', 'out'}:
            raise ValueError('invalid_transport_observation')
        identifier = 'transport:' + hashlib.sha256(event_id.encode()).hexdigest()
        values = (identifier, contact_id, channel, direction, 'Transport '+status, '',
                  stamp.isoformat(), external_ref, reply_to_ref or None, None,
                  receipt_ref, outbound_ref or None)
        columns = 'id,contact_id,channel,direction,summary,session_id,ts,external_ref,reply_to_ref,reaction,receipt_ref,outbound_ref'
        with self._conn:
            previous = self._conn.execute('SELECT '+columns+' FROM communications WHERE id=?', (identifier,)).fetchone()
            if previous:
                if tuple(previous) != values:
                    raise ValueError('transport_event_conflict')
                return False
            self._conn.execute('INSERT INTO communications ('+columns+') VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', values)
        return True

    def outbound_receipts(self, *, contact_id, outbound_ref):
        rows = self._conn.execute('''SELECT * FROM communications WHERE contact_id=?
            AND direction='out' AND (outbound_ref=? OR external_ref=?)
            AND receipt_ref IS NOT NULL AND external_ref IS NOT NULL ORDER BY ts,id LIMIT 100''',
            (contact_id, outbound_ref, outbound_ref)).fetchall()
        return [dict(row) for row in rows]

    def log(self, contact_id: str, *, channel: str = "unknown", direction: str = "in",
            summary: str = "", session_id: str = "",
            external_ref: str = "", reply_to_ref: str = "",
            reaction: str = "", receipt_ref: str = "",
            ts: Optional[str] = None, source_lineage=None) -> None:
        if not contact_id or direction not in ("in", "out"):
            return
        if source_lineage is not None and not self._source_visible(contact_id, source_lineage):
            from pacomind.turns.idempotency import SourceErased
            raise SourceErased('source_erased')
        normalized_reaction = (reaction or "").strip().lower()
        if normalized_reaction not in {
                "", "accepted", "acknowledged", "actioned", "negative",
                "dismissed", "corrected", "rejected", "neutral"}:
            normalized_reaction = "neutral"
        self._conn.execute(
            "INSERT INTO communications "
            "(id,contact_id,channel,direction,summary,session_id,ts,"
            "external_ref,reply_to_ref,reaction,receipt_ref,source_lineage_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, contact_id, channel or "unknown", direction,
             (summary or "")[:500], session_id or "", ts or _now().isoformat(),
             (external_ref or "")[:512] or None,
             (reply_to_ref or "")[:512] or None,
             normalized_reaction or None,
             (receipt_ref or "")[:512] or None,
             json.dumps(source_lineage) if source_lineage is not None else None),
        )
        self._conn.commit()
        # A source erasure can finish while this separate projection is written.
        if source_lineage is not None and not self._source_visible(contact_id, source_lineage):
            self.purge_erased_sources(contact_id=contact_id)
            from pacomind.turns.idempotency import SourceErased
            raise SourceErased('source_erased')

    def history(self, contact_id: str, limit: int = 15) -> List[Dict[str, Any]]:
        return self._summary_rows(
            "SELECT id,contact_id,source_lineage_json,channel,direction,summary,ts "
            "FROM communications WHERE contact_id=? ORDER BY ts DESC LIMIT ?",
            (contact_id, limit), ('channel', 'direction', 'summary', 'ts'), contact_id=contact_id)

    def last_per_channel(self, contact_id: str) -> Dict[str, Dict[str, Any]]:
        rows = self._summary_rows(
            "SELECT id,contact_id,source_lineage_json,channel,direction,summary,MAX(ts) AS ts "
            "FROM communications WHERE contact_id=? GROUP BY channel", (contact_id,),
            ('channel', 'direction', 'summary', 'ts'), contact_id=contact_id)
        return {r["channel"]: {"direction": r["direction"], "summary": r["summary"], "ts": r["ts"]}
                for r in rows}

    def last_outbound(self, contact_id: str) -> Optional[Dict[str, Any]]:
        rows = self._summary_rows(
            "SELECT id,contact_id,source_lineage_json,channel,summary,ts "
            "FROM communications WHERE contact_id=? AND direction='out' ORDER BY ts DESC LIMIT 1",
            (contact_id,), ('channel', 'summary', 'ts'), contact_id=contact_id)
        return rows[0] if rows else None

    def inbound_since(self, contact_id: str, since_iso: str) -> List[str]:
        """Timestamps of inbound rows from a contact since an ISO instant
        (selfhood benchmark: did the owner respond after a delivery)."""
        self._reconcile_erasure_ids(contact_id)
        rows = self._conn.execute(
            "SELECT ts FROM communications WHERE contact_id=? AND"
            " direction='in' AND ts >= ? ORDER BY ts ASC LIMIT 5000",
            (contact_id, since_iso)).fetchall()
        return [r["ts"] for r in rows]

    def reactions_for_refs(
        self,
        contact_id: str,
        refs: List[str],
        *,
        since_iso: str,
        until_iso: str,
    ) -> List[Dict[str, Any]]:
        """Explicit inbound reactions tied to exact outbound references."""
        bounded = sorted({str(ref) for ref in refs if str(ref).strip()})[:5000]
        if not bounded:
            return []
        placeholders = ",".join("?" for _ in bounded)
        return self._summary_rows(
            "SELECT id,contact_id,source_lineage_json,channel,summary,ts,external_ref,reply_to_ref,reaction,"
            "receipt_ref FROM communications WHERE contact_id=? "
            "AND direction='in' AND ts>=? AND ts<? AND reply_to_ref IN ("
            f"{placeholders}) AND reaction IS NOT NULL ORDER BY ts",
            [contact_id, since_iso, until_iso, *bounded],
            ('id', 'channel', 'summary', 'ts', 'external_ref', 'reply_to_ref', 'reaction', 'receipt_ref'),
            contact_id=contact_id)

    def match_reply(self, *, contact_id: str, outbound_ref: str, since_iso: str,
                    until_iso: Optional[str] = None, connection=None) -> Dict[str, Any]:
        """Exact receipt-backed reply references for a previously resolved contact.

        A reply link proves which message was addressed, not whether its requested
        answer or artifact was supplied. The waiting-condition owner checks that.
        Neither similar text nor an unrelated inbound message counts as a reply.
        """
        start, end = _parse(since_iso), _parse(until_iso) if until_iso else _now()
        if not contact_id or not outbound_ref or start is None or end is None or end < start:
            raise ValueError('invalid_reply_window')
        rows = (connection or self._conn).execute('''SELECT channel,ts,external_ref,reply_to_ref,reaction,receipt_ref
            FROM communications WHERE contact_id=? AND direction='in' AND reply_to_ref=?
            AND external_ref IS NOT NULL AND external_ref!=''
            AND receipt_ref IS NOT NULL AND receipt_ref!='' ORDER BY ts LIMIT 500''',
            (contact_id, outbound_ref)).fetchall()
        matches, seen = [], set()
        for row in rows:
            at = _parse(row['ts'])
            key = (row['external_ref'], row['receipt_ref'])
            if at is not None and start <= at <= end and key not in seen:
                matches.append(dict(row))
                seen.add(key)
        return {'status': 'matched' if matches else 'unrelated', 'contact_id': contact_id,
                'outbound_ref': outbound_ref, 'matches': matches,
                'condition_satisfied': False, 'coverage_limited': len(rows) == 500}

    def outbound_between(
        self,
        contact_id: str,
        since_iso: str,
        until_iso: str,
        *,
        require_receipt: bool = False,
    ) -> List[Dict[str, Any]]:
        """One auditable outbound cohort for benchmark denominators."""
        query = (
            "SELECT id,contact_id,source_lineage_json,channel,summary,ts,external_ref,receipt_ref "
            "FROM communications WHERE contact_id=? AND direction='out' "
            "AND ts>=? AND ts<?")
        if require_receipt:
            query += " AND receipt_ref IS NOT NULL AND receipt_ref!=''"
        query += " ORDER BY ts"
        return self._summary_rows(query, (contact_id, since_iso, until_iso),
            ('id', 'channel', 'summary', 'ts', 'external_ref', 'receipt_ref'), contact_id=contact_id)

    def counts(self, contact_id: str) -> Dict[str, Any]:
        self._reconcile_erasure_ids(contact_id)
        r = self._conn.execute(
            "SELECT SUM(direction='in') AS inbound, SUM(direction='out') AS outbound,"
            " COUNT(DISTINCT channel) AS channels FROM communications WHERE contact_id=?",
            (contact_id,)).fetchone()
        return {"inbound": r["inbound"] or 0, "outbound": r["outbound"] or 0,
                "channels": r["channels"] or 0}

    def stats(self, contact_id: str, *, since_days: int = 90) -> Dict[str, Any]:
        """Channel-usage counts and interaction-hour histogram (UTC hours;
        the consumer shifts into the contact's timezone). Feeds the
        relationship profiler's approach guidance (preferred channel,
        best time to reach)."""
        self._reconcile_erasure_ids(contact_id)
        rows = self._conn.execute(
            "SELECT channel, ts FROM communications"
            " WHERE contact_id=? AND ts >= datetime('now', ?)",
            (contact_id, f"-{int(since_days)} day")).fetchall()
        channels: Dict[str, int] = {}
        hours = [0] * 24
        for r in rows:
            channels[r["channel"]] = channels.get(r["channel"], 0) + 1
            try:
                hours[int(str(r["ts"])[11:13])] += 1
            except (ValueError, IndexError):
                pass
        return {"total": len(rows), "channels": channels, "hours_utc": hours}

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        """The newest exchanges across ALL contacts and channels, most recent
        first. The per-contact reads above answer 'how do I stand with X';
        this answers 'what has been flowing lately' for an ops ledger view."""
        limit = max(1, min(500, int(limit)))
        return self._summary_rows(
            "SELECT id,contact_id,source_lineage_json,channel,direction,summary,ts "
            "FROM communications ORDER BY ts DESC LIMIT ?", (limit,),
            ('contact_id', 'channel', 'direction', 'summary', 'ts'))

    def rollup(self, *, since_days: int = 30) -> Dict[str, Dict[str, int]]:
        """Inbound/outbound counts per channel over a window: the ledger's
        flow summary. ``{channel: {"in": n, "out": n}}``."""
        self._reconcile_erasure_ids()
        rows = self._conn.execute(
            "SELECT channel, direction, COUNT(*) AS n FROM communications"
            " WHERE ts >= datetime('now', ?) GROUP BY channel, direction",
            (f"-{int(since_days)} day",)).fetchall()
        out: Dict[str, Dict[str, int]] = {}
        for r in rows:
            ch = out.setdefault(r["channel"], {"in": 0, "out": 0})
            if r["direction"] in ("in", "out"):
                ch[r["direction"]] = r["n"]
        return out


# ---------------------------------------------------------------------------
# Outreach governance
# ---------------------------------------------------------------------------
def evaluate_outreach(
    contact: Any,
    *,
    is_owner: bool = False,
    last_outbound_ts: Optional[str] = None,
    cadence_days: Optional[float] = None,
    overdue: bool = False,
    open_followups: Optional[List[str]] = None,
    suggested_channel: str = "",
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Decide whether the agent should (re)initiate contact, and how.

    Returns: should_contact, reason, requires_owner_approval, suggested_channel,
    cooldown_active, talking_points. Policy: never contact a blocked contact;
    respect a cooldown so we don't double-message; only reach out when there's a
    real reason (overdue cadence or an open follow-up); and ANY proactive outreach
    to someone other than the owner requires the owner's approval first.
    """
    now = now or _now()
    followups = [f for f in (open_followups or []) if f]
    allowed = getattr(contact, "interaction_allowed", True)

    result = {
        "should_contact": False,
        "reason": "",
        "requires_owner_approval": (not is_owner),
        "suggested_channel": suggested_channel or "",
        "cooldown_active": False,
        "talking_points": followups[:5],
    }

    if not allowed:
        result["reason"] = "contact is not authorized for outreach (interaction_allowed=false)"
        result["requires_owner_approval"] = True
        return result

    # Cooldown: don't reach out again too soon after our last outbound.
    last_out = _parse(last_outbound_ts)
    if last_out is not None:
        hrs = (now - last_out).total_seconds() / 3600.0
        cooldown_hrs = max(24.0, (float(cadence_days) * 24.0 / 2.0) if cadence_days else 24.0)
        if hrs < cooldown_hrs:
            result["cooldown_active"] = True
            result["reason"] = (f"reached out {round(hrs)}h ago; in cooldown "
                                f"(~{round(cooldown_hrs)}h) — hold off to avoid spamming")
            return result

    reasons = []
    if overdue:
        reasons.append("overdue vs their usual cadence"
                       + (f" (~{round(cadence_days)}d)" if cadence_days else ""))
    if followups:
        reasons.append(f"{len(followups)} open follow-up(s)")

    if not reasons:
        result["reason"] = "no current reason to reach out (not overdue, no open follow-ups)"
        return result

    result["should_contact"] = True
    result["reason"] = "; ".join(reasons)
    return result
