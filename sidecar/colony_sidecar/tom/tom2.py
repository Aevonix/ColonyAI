"""Second-order theory of mind (tom2) — refs, never content.

Stores inferences about what a CONTACT knows or is unaware of, built on the
SharedFactsStore's per-contact epistemic rows.

PRIVACY INVARIANT (the reason this store exists as its own table):
  * A tom2 row references facts by ID only (``fact_ref`` + ``evidence_refs``)
    — cross-contact fact TEXT is never copied into an inference row, so a
    leak of this table reveals topology ("A doesn't know something B told
    me"), never content.
  * ``visibility='owner'`` is the ONLY writable value; readers that are not
    the owner scope get nothing.

Both pins are enforced at write time (ValueError, not best-effort) and are
regression-locked by a raw-DB regex test.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# What an inference can claim about a contact's epistemic state.
INFERENCE_KINDS = ("knows", "unaware_of")

OWNER_VISIBILITY = "owner"

# A fact ref is an opaque id: compact, no whitespace — anything that looks
# like prose is refused so text cannot be smuggled through the refs field.
_REF_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_ref(ref: Any) -> str:
    ref = str(ref or "").strip()
    if not _REF_RE.match(ref):
        raise ValueError(
            f"tom2 evidence ref {ref[:40]!r} is not an opaque id "
            "(refs-not-content invariant)")
    return ref


class Tom2Store:
    """SQLite-backed second-order inference store (refs-not-content)."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path or ":memory:",
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS tom2_inferences (
                    id TEXT PRIMARY KEY,
                    contact_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    fact_ref TEXT NOT NULL,
                    evidence_refs TEXT NOT NULL DEFAULT '[]',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    visibility TEXT NOT NULL DEFAULT 'owner',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(contact_id, kind, fact_ref)
                )""")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tom2_contact "
                "ON tom2_inferences(contact_id)")
            self._conn.commit()

    # -- writes -----------------------------------------------------------
    def record_inference(
        self,
        *,
        contact_id: str,
        kind: str,
        fact_ref: str,
        evidence_refs: Optional[List[str]] = None,
        confidence: float = 0.5,
        visibility: str = OWNER_VISIBILITY,
    ) -> Dict[str, Any]:
        """Upsert one inference row. Raises on any privacy-pin violation."""
        if kind not in INFERENCE_KINDS:
            raise ValueError(f"unknown tom2 inference kind {kind!r}")
        if visibility != OWNER_VISIBILITY:
            # The only writable visibility is 'owner' — wider scoping is a
            # separate, explicitly-gated rendering decision, never storage.
            raise ValueError(
                "tom2 rows are owner-only; refusing visibility "
                f"{visibility!r}")
        contact_id = str(contact_id or "").strip()
        if not contact_id:
            raise ValueError("tom2 inference requires a contact_id")
        fact_ref = _validate_ref(fact_ref)
        refs = [_validate_ref(r) for r in (evidence_refs or [])]
        confidence = max(0.0, min(1.0, float(confidence)))
        now = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO tom2_inferences
                   (id, contact_id, kind, fact_ref, evidence_refs,
                    confidence, visibility, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(contact_id, kind, fact_ref) DO UPDATE SET
                    evidence_refs=excluded.evidence_refs,
                    confidence=excluded.confidence,
                    updated_at=excluded.updated_at""",
                (str(uuid.uuid4()), contact_id, kind, fact_ref,
                 json.dumps(refs), confidence, OWNER_VISIBILITY, now, now))
            self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM tom2_inferences WHERE contact_id=? AND kind=? "
            "AND fact_ref=?", (contact_id, kind, fact_ref)).fetchone()
        return self._to_dict(row)

    def delete_for_fact(self, fact_ref: str) -> int:
        """Drop inferences referencing a fact (e.g. after fact deletion)."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM tom2_inferences WHERE fact_ref=?", (fact_ref,))
            self._conn.commit()
        return cur.rowcount

    # -- reads ------------------------------------------------------------
    @staticmethod
    def _to_dict(row: Any) -> Dict[str, Any]:
        d = dict(row)
        try:
            d["evidence_refs"] = json.loads(d.get("evidence_refs") or "[]")
        except Exception:
            d["evidence_refs"] = []
        return d

    def list_inferences(
        self,
        *,
        contact_id: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 100,
        reader_scope: str = OWNER_VISIBILITY,
    ) -> List[Dict[str, Any]]:
        """Owner-scoped read. Any non-owner reader scope gets nothing —
        second-order inferences are for the owner's understanding only."""
        if reader_scope != OWNER_VISIBILITY:
            return []
        clauses, params = ["visibility = ?"], [OWNER_VISIBILITY]
        if contact_id:
            clauses.append("contact_id = ?")
            params.append(contact_id)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM tom2_inferences WHERE {' AND '.join(clauses)}"
                " ORDER BY updated_at DESC LIMIT ?", params).fetchall()
        return [self._to_dict(r) for r in rows]

    def counts(self) -> Dict[str, Any]:
        """Aggregate observability (safe in any mode: numbers only)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) n FROM tom2_inferences GROUP BY kind"
            ).fetchall()
            contacts = self._conn.execute(
                "SELECT COUNT(DISTINCT contact_id) n FROM tom2_inferences"
            ).fetchone()
        by_kind = {r["kind"]: r["n"] for r in rows}
        return {"total": sum(by_kind.values()), "by_kind": by_kind,
                "contacts": int(contacts["n"]) if contacts else 0}

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
