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
import os
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


# ---------------------------------------------------------------------------
# Cross-contact rendering gate (H3.5) — BUILT, SHIPPED DARK, UNWIRED.
#
# This is the ONLY sanctioned way a tom2 inference could ever be rendered for
# a non-owner contact, and it is deliberately NOT called from any live
# injection path (context assembly, briefings, delivery — nothing). Flipping
# COLONY_TOM2_CROSS_CONTEXT alone therefore changes no live behavior; wiring
# it anywhere is a separate owner decision that additionally requires the
# chat guard to be ENFORCING (the doctor warns on the incoherent combination)
# because "X hasn't heard this" carries implication-leak risk that only an
# enforcing outbound guard can backstop.
# ---------------------------------------------------------------------------

def tom2_cross_context_enabled() -> bool:
    """COLONY_TOM2_CROSS_CONTEXT (default 0): first half of the double gate.
    Ships OFF by design; see the block comment above."""
    return os.environ.get("COLONY_TOM2_CROSS_CONTEXT", "0").strip().lower() \
        in ("1", "true", "yes", "on")


def _ref_visible_to(facts_store: Any, ref: str, contact_id: str) -> bool:
    """A fact ref is visible to a contact only when the fact ROW belongs to
    that contact (it was shared with / told by them). Missing or unreadable
    facts are NOT visible — fail closed."""
    try:
        f = facts_store.get_fact(str(ref or ""))
    except Exception:
        return False
    return bool(f) and str(f.get("contact_id") or "") == str(contact_id)


def render_inference_for_contact(inference: Dict[str, Any], facts_store: Any,
                                 contact_id: str) -> Optional[str]:
    """Render ONE inference for a non-owner contact, or None.

    Second half of the double gate: EVERY ref the inference rests on
    (fact_ref AND each evidence_ref) must be independently visible to the
    reading contact. ANY partial visibility renders None — never a redacted
    line, never a hint that something was withheld (a redaction IS a leak
    of topology). Inferences about the reading contact themselves render
    None (first-order information wearing a second-order hat).
    """
    if not tom2_cross_context_enabled():
        return None
    if facts_store is None or not contact_id or not isinstance(inference, dict):
        return None
    subject_cid = str(inference.get("contact_id") or "")
    if not subject_cid or subject_cid == str(contact_id):
        return None
    refs = [inference.get("fact_ref")] + list(inference.get("evidence_refs")
                                              or [])
    if not refs or any(not _ref_visible_to(facts_store, r, contact_id)
                       for r in refs):
        return None
    fact = facts_store.get_fact(str(inference.get("fact_ref")))
    text = str((fact or {}).get("fact") or "").strip()
    if not text:
        return None
    kind = inference.get("kind")
    if kind == "unaware_of":
        return f"{subject_cid} has not heard: {text[:160]}"
    if kind == "knows":
        return f"{subject_cid} already knows: {text[:160]}"
    return None


def render_for_contact(store: Any, facts_store: Any, contact_id: str,
                       *, limit: int = 5) -> Optional[str]:
    """Aggregate cross-contact rendering: only inferences whose EVERY ref is
    visible to the reader make it in; None when the gate is off, nothing is
    fully visible, or anything errors (fail closed, no partial output)."""
    if not tom2_cross_context_enabled():
        return None
    if store is None or facts_store is None or not contact_id:
        return None
    try:
        rows = store.list_inferences(limit=100)
    except Exception:
        return None
    lines: List[str] = []
    for r in rows:
        line = render_inference_for_contact(r, facts_store, contact_id)
        if line:
            lines.append(f"- {line}")
        if len(lines) >= limit:
            break
    return "\n".join(lines) if lines else None
