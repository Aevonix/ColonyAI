"""BeliefStore -- conflict records, supersession audit, property snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional


class BeliefStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path) if db_path else ":memory:",
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS conflicts (
                    id TEXT PRIMARY KEY, scope TEXT, subject TEXT,
                    predicate TEXT, value_a TEXT, value_b TEXT,
                    meta_a TEXT, meta_b TEXT,
                    status TEXT DEFAULT 'open',
                    resolution TEXT, detected_at REAL, resolved_at REAL
                )""")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS supersession_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT, subject TEXT, predicate TEXT,
                    old_value TEXT, new_value TEXT,
                    old_confidence REAL, new_confidence REAL,
                    reason TEXT, actor TEXT, created_at REAL
                )""")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS property_snapshot (
                    entity_id TEXT, key TEXT, value TEXT, confidence REAL,
                    updated_at REAL, PRIMARY KEY (entity_id, key)
                )""")
            self._conn.commit()

    # -- conflicts ---------------------------------------------------------
    @staticmethod
    def conflict_id(scope: str, subject: str, predicate: str,
                    value_a: str, value_b: str) -> str:
        basis = "|".join(sorted([str(value_a), str(value_b)])
                         + [scope, subject.lower(), predicate.lower()])
        return "bc-" + hashlib.sha256(basis.encode()).hexdigest()[:12]

    def record_conflict(self, scope: str, subject: str, predicate: str,
                        value_a: str, value_b: str,
                        meta_a: Optional[dict] = None,
                        meta_b: Optional[dict] = None,
                        status: str = "open") -> str:
        cid = self.conflict_id(scope, subject, predicate, value_a, value_b)
        with self._lock:
            self._conn.execute(
                """INSERT INTO conflicts
                   (id, scope, subject, predicate, value_a, value_b,
                    meta_a, meta_b, status, detected_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO NOTHING""",
                (cid, scope, subject[:200], predicate[:120],
                 str(value_a)[:300], str(value_b)[:300],
                 json.dumps(meta_a or {}), json.dumps(meta_b or {}),
                 status, time.time()))
            self._conn.commit()
        return cid

    def resolve_conflict(self, conflict_id: str, resolution: str,
                         status: str = "resolved") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE conflicts SET status=?, resolution=?, resolved_at=? "
                "WHERE id=?",
                (status, resolution[:400], time.time(), conflict_id))
            self._conn.commit()

    def conflicts(self, status: Optional[str] = None,
                  limit: int = 50) -> List[Dict[str, Any]]:
        q = "SELECT * FROM conflicts"
        params: List[Any] = []
        if status:
            q += " WHERE status=?"; params.append(status)
        q += " ORDER BY detected_at DESC LIMIT ?"; params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    # -- supersession audit ---------------------------------------------------
    def record_supersession(self, scope: str, subject: str, predicate: str,
                            old_value: Any, new_value: Any,
                            old_confidence: float, new_confidence: float,
                            reason: str, actor: str = "belief_engine") -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO supersession_audit
                   (scope, subject, predicate, old_value, new_value,
                    old_confidence, new_confidence, reason, actor, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (scope, subject[:200], predicate[:120], str(old_value)[:300],
                 str(new_value)[:300], old_confidence, new_confidence,
                 reason[:300], actor, time.time()))
            self._conn.commit()

    def supersessions(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM supersession_audit ORDER BY created_at DESC "
                "LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # -- property snapshots (for change/supersession detection) ---------------
    def snapshot_get(self, entity_id: str, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM property_snapshot WHERE entity_id=? AND key=?",
                (entity_id, key)).fetchone()
        return dict(r) if r else None

    def snapshot_put(self, entity_id: str, key: str, value: Any,
                     confidence: float) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO property_snapshot
                   (entity_id, key, value, confidence, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(entity_id, key) DO UPDATE SET
                     value=?, confidence=?, updated_at=?""",
                (entity_id, key, str(value)[:300], confidence, time.time(),
                 str(value)[:300], confidence, time.time()))
            self._conn.commit()
