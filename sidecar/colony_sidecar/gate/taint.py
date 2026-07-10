"""Injection-taint registry (L3.1) — TTL'd record of live level-2 injections.

When the leveled tom2 wiring renders an epistemic line ("SUBJECT hasn't
heard X") into a conversation's context, it registers a TAINT here: for the
next COLONY_TOM2_TAINT_TTL_SECS (default 900s) the outbound egress net
(gate/layers/tom2_epistemic.py) knows that a silent prior about SUBJECT is
sitting in the model's context window and can hard-block a reply that
voices it.

REFS-NOT-CONTENT (same pin as Tom2Store / the exposure ledger): a taint row
carries an opaque conversation key, an opaque subject contact id, an opaque
fact ref, an inference kind, and the subject's normalized DISPLAY NAMES —
which are, by the M1 mutual-knowledge gate, names the reader already knows.
Fact TEXT never enters this table; a leak of the registry reveals that a
prior about someone was injected somewhere, never what it said.

Hot-path economics: ``any_active()`` is the guard's first question every
turn, so it is answered from an in-memory latest-expiry watermark (a clock
comparison, no DB hit) whenever no taint could possibly be live. The
watermark is loaded from SQLite at startup, so taints survive a restart.

Fail-closed direction: a taint is PROTECTION, so errors lean toward keeping
it — malformed TTL config reads as the default, and expiry is judged
against wall-clock timestamps written at register time.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from colony_sidecar.gate.context_provenance import normalize_entity
from colony_sidecar.tom.tom2 import INFERENCE_KINDS, _validate_ref

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECS = 900.0

#: Opaque conversation keys / contact ids: compact, no whitespace — prose is
#: refused so message or fact text cannot be smuggled through these fields.
_KEY_RE = re.compile(r"^[A-Za-z0-9_.:+@-]{1,128}$")

_MAX_NAMES = 8
_MAX_NAME_LEN = 80


def taint_ttl_secs() -> float:
    """COLONY_TOM2_TAINT_TTL_SECS (default 900): how long a registered
    injection stays hot for the egress net. Malformed values read as the
    default (a taint is protection; misconfiguration must not shorten it);
    values <= 0 clamp to the default for the same reason."""
    raw = os.environ.get("COLONY_TOM2_TAINT_TTL_SECS")
    if raw is None or not str(raw).strip():
        return DEFAULT_TTL_SECS
    try:
        v = float(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("COLONY_TOM2_TAINT_TTL_SECS=%r is malformed — using "
                       "the default %ss", raw, DEFAULT_TTL_SECS)
        return DEFAULT_TTL_SECS
    return v if v > 0 else DEFAULT_TTL_SECS


def _validate_key(value: Any, what: str) -> str:
    value = str(value or "").strip()
    if not _KEY_RE.match(value):
        raise ValueError(f"taint {what} {value[:40]!r} is not an opaque id "
                         "(refs-not-content invariant)")
    return value


def _clean_names(names: Any) -> List[str]:
    """Normalize subject display names (normalize_entity), dedupe, cap."""
    out: List[str] = []
    seen = set()
    for raw in list(names or [])[: _MAX_NAMES * 2]:
        n = normalize_entity(str(raw or ""))[:_MAX_NAME_LEN]
        if len(n) >= 2 and n not in seen:
            seen.add(n)
            out.append(n)
        if len(out) >= _MAX_NAMES:
            break
    return out


class TaintRegistry:
    """SQLite-backed TTL registry of live level-2 injections."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path or ":memory:",
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS tom2_taints (
                    id                 TEXT PRIMARY KEY,
                    conversation_key   TEXT NOT NULL,
                    subject_contact_id TEXT NOT NULL,
                    subject_names      TEXT NOT NULL DEFAULT '[]',
                    fact_ref           TEXT NOT NULL,
                    kind               TEXT NOT NULL,
                    created_at         REAL NOT NULL,
                    expires_at         REAL NOT NULL
                )""")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tom2_taints_conv "
                "ON tom2_taints(conversation_key, expires_at)")
            self._conn.commit()
            # In-memory watermark: the hot path answers "no taints" from a
            # clock comparison. Persisted taints re-arm it across restarts.
            row = self._conn.execute(
                "SELECT MAX(expires_at) m FROM tom2_taints").fetchone()
            self._latest_expiry: float = float(row["m"] or 0.0)

    # -- writes -------------------------------------------------------------
    def register(self, conversation_key: str, subject_contact_id: str,
                 subject_names: Optional[List[str]] = None,
                 fact_ref: str = "", kind: str = "unaware_of",
                 ttl_seconds: Optional[float] = None) -> Dict[str, Any]:
        """Record one live injection. Raises on any privacy-pin violation
        (prose in an opaque field, unknown kind) — never best-effort."""
        if kind not in INFERENCE_KINDS:
            raise ValueError(f"unknown taint kind {kind!r}")
        ttl = float(ttl_seconds) if ttl_seconds is not None \
            else taint_ttl_secs()
        if ttl <= 0:
            ttl = taint_ttl_secs()
        now = time.time()
        row = {
            "id": str(uuid.uuid4()),
            "conversation_key": _validate_key(conversation_key,
                                              "conversation key"),
            "subject_contact_id": _validate_key(subject_contact_id,
                                                "subject id"),
            "subject_names": _clean_names(subject_names),
            "fact_ref": _validate_ref(fact_ref),
            "kind": kind,
            "created_at": now,
            "expires_at": now + ttl,
        }
        with self._lock:
            self._conn.execute(
                "INSERT INTO tom2_taints (id, conversation_key, "
                "subject_contact_id, subject_names, fact_ref, kind, "
                "created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
                (row["id"], row["conversation_key"],
                 row["subject_contact_id"], json.dumps(row["subject_names"]),
                 row["fact_ref"], row["kind"], row["created_at"],
                 row["expires_at"]))
            self._conn.commit()
            self._latest_expiry = max(self._latest_expiry, row["expires_at"])
        return row

    def purge_expired(self) -> int:
        """Drop expired rows (housekeeping; reads already exclude them)."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM tom2_taints WHERE expires_at <= ?",
                (time.time(),))
            self._conn.commit()
        return cur.rowcount

    # -- reads --------------------------------------------------------------
    @staticmethod
    def _to_dict(row: Any) -> Dict[str, Any]:
        d = dict(row)
        try:
            d["subject_names"] = json.loads(d.get("subject_names") or "[]")
        except Exception:
            d["subject_names"] = []
        return d

    def any_active(self) -> bool:
        """Is ANY taint live right now? Answered from the in-memory
        watermark — zero DB cost on the (overwhelmingly common) no-taint
        turn, which is what keeps the egress check inert for free."""
        with self._lock:
            return time.time() < self._latest_expiry

    def active_for(self, conversation_key: str) -> List[Dict[str, Any]]:
        """Live taints registered against THIS conversation."""
        if not self.any_active():
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tom2_taints WHERE conversation_key = ? "
                "AND expires_at > ? ORDER BY expires_at DESC",
                (str(conversation_key or ""), time.time())).fetchall()
        return [self._to_dict(r) for r in rows]

    def all_active(self) -> List[Dict[str, Any]]:
        """Every live taint, any conversation — the egress net checks the
        reply against ALL of them (a prior injected in one conversation must
        not be voiced anywhere while it is hot)."""
        if not self.any_active():
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tom2_taints WHERE expires_at > ? "
                "ORDER BY expires_at DESC", (time.time(),)).fetchall()
        return [self._to_dict(r) for r in rows]

    def counts(self) -> Dict[str, Any]:
        """Aggregate observability (numbers only)."""
        now = time.time()
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) n FROM tom2_taints").fetchone()["n"]
            active = self._conn.execute(
                "SELECT COUNT(*) n FROM tom2_taints WHERE expires_at > ?",
                (now,)).fetchone()["n"]
        return {"rows": int(total), "active": int(active)}

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
