"""ConversationPresenceStore — who has been seen in which conversation (L1.1).

The conversation participant registry the leveled cross-contact tom2 system
(docs-level: TOM2 levels) is built on. Every attributed turn records
``(conversation_key, contact_id, resolution method, group_id)``; downstream
consumers (the environment-risk classifier, subject-presence exclusion) read a
windowed census of a conversation.

Properties:

* **Passive.** Recording is fed from the turns/sync attribution chokepoint
  (after the ParticipantResolver has decided WHO the turn came from) and
  changes nothing about turn processing. ``COLONY_CONV_PRESENCE`` (default
  on) can disable recording entirely.
* **Identity only.** Rows carry contact ids, resolution methods and opaque
  conversation keys — never message content.
* **System excluded.** The reserved machine sentinel (``system``) is never
  recorded: machine turns must not shape a conversation's human census.
* **Reads fail closed.** Read methods PROPAGATE storage errors instead of
  returning an empty (i.e. "nobody here") census — an empty answer from a
  broken store would read as a SAFE signal to the risk classifier. Callers
  must catch and treat any error as "unknown, assume hostile".
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from colony_sidecar.identity.participants import SYSTEM_CONTACT_ID

logger = logging.getLogger(__name__)

#: Resolution methods considered STRONG identity evidence: the contact was
#: matched by a verified handle or arrived pre-resolved as a canonical contact
#: id. Scoped-name matches (unverified link proposals), shadow contacts and
#: client-claimed ids are NOT strong.
STRONG_METHODS = frozenset({"handle", "contact_id"})


def conv_presence_enabled() -> bool:
    """COLONY_CONV_PRESENCE (default on): passive presence recording."""
    return os.environ.get("COLONY_CONV_PRESENCE", "on").strip().lower() not in (
        "off", "0", "false", "no")


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class ConversationPresenceStore:
    """SQLite-backed registry of who was seen in which conversation."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path or ":memory:",
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS conversation_presence (
                    conversation_key TEXT NOT NULL,
                    contact_id       TEXT NOT NULL,
                    method           TEXT NOT NULL DEFAULT '',
                    group_id         TEXT NOT NULL DEFAULT '',
                    first_seen_at    TEXT NOT NULL,
                    last_seen_at     TEXT NOT NULL,
                    turns            INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (conversation_key, contact_id)
                )""")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conv_presence_contact "
                "ON conversation_presence(contact_id)")
            self._conn.commit()

    # -- writes -----------------------------------------------------------
    def record(self, conversation_key: str, contact_id: str, *,
               method: str = "", group_id: str = "") -> bool:
        """Record one sighting of ``contact_id`` in ``conversation_key``.

        Returns True when a row was written/refreshed. The system sentinel
        and empty ids are silently skipped (never an error — the chokepoint
        must not care); the COLONY_CONV_PRESENCE gate turns the whole write
        into a no-op. ``method`` stores the LATEST resolution method for the
        pair: a weak latest sighting correctly downgrades the row, and a row
        can only read as strong when the most recent turn actually resolved
        strongly (by verified handle / canonical contact id).
        """
        if not conv_presence_enabled():
            return False
        conversation_key = str(conversation_key or "").strip()
        contact_id = str(contact_id or "").strip()
        if not conversation_key or not contact_id:
            return False
        if contact_id == SYSTEM_CONTACT_ID:
            return False
        now = _now().isoformat()
        method = str(method or "").strip().lower()
        group_id = str(group_id or "").strip()
        with self._lock:
            self._conn.execute(
                """INSERT INTO conversation_presence
                   (conversation_key, contact_id, method, group_id,
                    first_seen_at, last_seen_at, turns)
                   VALUES (?,?,?,?,?,?,1)
                   ON CONFLICT(conversation_key, contact_id) DO UPDATE SET
                    method=excluded.method,
                    group_id=CASE WHEN excluded.group_id != ''
                                  THEN excluded.group_id ELSE group_id END,
                    last_seen_at=excluded.last_seen_at,
                    turns=turns+1""",
                (conversation_key, contact_id, method, group_id, now, now))
            self._conn.commit()
        return True

    # -- reads (fail closed: errors PROPAGATE) ------------------------------
    def census(self, conversation_key: str,
               window_hours: float = 48.0) -> List[Dict[str, Any]]:
        """Participants seen in this conversation within the window.

        Raises on any storage error — callers MUST treat an exception as
        "census unknown" (hostile), never as "empty room".
        """
        cutoff = (_now() - timedelta(hours=float(window_hours))).isoformat()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM conversation_presence "
                "WHERE conversation_key=? AND last_seen_at >= ? "
                "ORDER BY last_seen_at DESC",
                (str(conversation_key or ""), cutoff)).fetchall()
        return [dict(r) for r in rows]

    def is_present(self, conversation_key: str, contact_id: str,
                   window_hours: float = 48.0) -> bool:
        """True when the contact was seen in the conversation within the
        window. Raises on storage errors (see census)."""
        cutoff = (_now() - timedelta(hours=float(window_hours))).isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM conversation_presence "
                "WHERE conversation_key=? AND contact_id=? AND last_seen_at >= ?",
                (str(conversation_key or ""), str(contact_id or ""),
                 cutoff)).fetchone()
        return row is not None

    def cooccurred(self, contact_a: str, contact_b: str,
                   within_days: float = 30.0) -> bool:
        """True when both contacts were seen in at least ONE common
        conversation within the window (mutual-knowledge evidence: they
        demonstrably know of each other). Raises on storage errors."""
        a = str(contact_a or "").strip()
        b = str(contact_b or "").strip()
        if not a or not b or a == b:
            return False
        cutoff = (_now() - timedelta(days=float(within_days))).isoformat()
        with self._lock:
            row = self._conn.execute(
                """SELECT 1 FROM conversation_presence pa
                   JOIN conversation_presence pb
                     ON pa.conversation_key = pb.conversation_key
                   WHERE pa.contact_id=? AND pb.contact_id=?
                     AND pa.last_seen_at >= ? AND pb.last_seen_at >= ?
                   LIMIT 1""", (a, b, cutoff, cutoff)).fetchone()
        return row is not None

    def counts(self) -> Dict[str, Any]:
        """Aggregate observability (numbers only)."""
        with self._lock:
            convs = self._conn.execute(
                "SELECT COUNT(DISTINCT conversation_key) n "
                "FROM conversation_presence").fetchone()
            contacts = self._conn.execute(
                "SELECT COUNT(DISTINCT contact_id) n "
                "FROM conversation_presence").fetchone()
            rows = self._conn.execute(
                "SELECT COUNT(*) n FROM conversation_presence").fetchone()
        return {"rows": int(rows["n"]), "conversations": int(convs["n"]),
                "contacts": int(contacts["n"])}

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
