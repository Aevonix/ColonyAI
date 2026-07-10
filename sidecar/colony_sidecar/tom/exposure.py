"""Tom2 exposure ledger + budgets (L2.3) — refs only, never content.

Every level-2 rendering, when the system eventually wires one, must be
written here FIRST and budgeted against what was already shown. The ledger
slows aggregation (T6): a reader cannot harvest the epistemic map by asking
often, because budgets bind per (reader, subject) pair, per reader, and
globally per rolling 24h window.

PRIVACY INVARIANT (same pin as Tom2Store, enforced at write time and
regression-locked by a raw-DB regex test): a ledger row carries contact
ids, an opaque fact ref, and an opaque conversation key — cross-contact
fact TEXT never enters this table. A leak of the ledger reveals that
topology was rendered, never what it said.

Budget semantics are fail-closed: malformed env values read as budget 0
(nothing renders), and any storage error answers "no budget".
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from colony_sidecar.tom.tom2 import _validate_ref

logger = logging.getLogger(__name__)

#: Opaque conversation key: compact, no whitespace — prose is refused so
#: message text cannot be smuggled through the key field.
_CONV_KEY_RE = re.compile(r"^[A-Za-z0-9_.:+@-]{1,128}$")

#: Shipped budget defaults — deliberately tight; a deployment widens them
#: explicitly. Malformed values fail closed to 0.
DEFAULT_PAIR_DAY = 1
DEFAULT_READER_DAY = 3
DEFAULT_GLOBAL_DAY = 10


def _budget(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        logger.warning("%s=%r is malformed — failing closed to budget 0",
                       name, raw)
        return 0


def budget_pair_day() -> int:
    """COLONY_TOM2_BUDGET_PAIR_DAY (default 1): renderings per (reader,
    subject) pair per rolling 24h."""
    return _budget("COLONY_TOM2_BUDGET_PAIR_DAY", DEFAULT_PAIR_DAY)


def budget_reader_day() -> int:
    """COLONY_TOM2_BUDGET_READER_DAY (default 3): renderings per reader
    per rolling 24h."""
    return _budget("COLONY_TOM2_BUDGET_READER_DAY", DEFAULT_READER_DAY)


def budget_global_day() -> int:
    """COLONY_TOM2_BUDGET_GLOBAL_DAY (default 10): renderings across all
    readers per rolling 24h."""
    return _budget("COLONY_TOM2_BUDGET_GLOBAL_DAY", DEFAULT_GLOBAL_DAY)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _validate_conv_key(key: Any) -> str:
    key = str(key or "").strip()
    if not _CONV_KEY_RE.match(key):
        raise ValueError(
            f"exposure conversation key {key[:40]!r} is not an opaque id "
            "(refs-not-content invariant)")
    return key


def _validate_cid(cid: Any, what: str) -> str:
    cid = str(cid or "").strip()
    if not _CONV_KEY_RE.match(cid):
        raise ValueError(
            f"exposure {what} {cid[:40]!r} is not an opaque contact id "
            "(refs-not-content invariant)")
    return cid


class Tom2ExposureStore:
    """SQLite-backed ledger of level-2 renderings (refs-not-content)."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path or ":memory:",
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS tom2_exposures (
                    id                 TEXT PRIMARY KEY,
                    reader_contact_id  TEXT NOT NULL,
                    subject_contact_id TEXT NOT NULL,
                    fact_ref           TEXT NOT NULL,
                    conversation_key   TEXT NOT NULL,
                    created_at         TEXT NOT NULL
                )""")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tom2_exp_reader "
                "ON tom2_exposures(reader_contact_id, created_at)")
            self._conn.commit()

    # -- writes -------------------------------------------------------------
    def record_exposure(self, *, reader_contact_id: str,
                        subject_contact_id: str, fact_ref: str,
                        conversation_key: str) -> Dict[str, Any]:
        """Append one rendering to the ledger. Raises on any privacy-pin
        violation (prose in any field) — never best-effort."""
        row = {
            "id": str(uuid.uuid4()),
            "reader_contact_id": _validate_cid(reader_contact_id, "reader"),
            "subject_contact_id": _validate_cid(subject_contact_id,
                                                "subject"),
            "fact_ref": _validate_ref(fact_ref),
            "conversation_key": _validate_conv_key(conversation_key),
            "created_at": _now().isoformat(),
        }
        with self._lock:
            self._conn.execute(
                "INSERT INTO tom2_exposures (id, reader_contact_id, "
                "subject_contact_id, fact_ref, conversation_key, created_at)"
                " VALUES (?,?,?,?,?,?)", list(row.values()))
            self._conn.commit()
        return row

    # -- budgets --------------------------------------------------------------
    def _count(self, where: str, params: List[Any], hours: float) -> int:
        cutoff = (_now() - timedelta(hours=hours)).isoformat()
        with self._lock:
            r = self._conn.execute(
                f"SELECT COUNT(*) n FROM tom2_exposures WHERE {where} "
                "AND created_at >= ?", params + [cutoff]).fetchone()
        return int(r["n"])

    def budget_ok(self, reader_contact_id: str, subject_contact_id: str,
                  fact_ref: str = "") -> bool:
        """True when rendering one more line for (reader, subject) stays
        inside ALL budgets (pair, reader, global; rolling 24h). Any error
        answers False — a broken ledger cannot vouch for capacity."""
        try:
            reader = str(reader_contact_id or "").strip()
            subject = str(subject_contact_id or "").strip()
            if not reader or not subject:
                return False
            if self._count(
                    "reader_contact_id=? AND subject_contact_id=?",
                    [reader, subject], 24.0) >= budget_pair_day():
                return False
            if self._count("reader_contact_id=?", [reader],
                           24.0) >= budget_reader_day():
                return False
            if self._count("1=1", [], 24.0) >= budget_global_day():
                return False
            return True
        except Exception:
            logger.debug("exposure budget check failed (=> no budget)",
                         exc_info=True)
            return False

    # -- owner reads ----------------------------------------------------------
    def recent(self, *, reader_contact_id: Optional[str] = None,
               subject_contact_id: Optional[str] = None,
               limit: int = 50) -> List[Dict[str, Any]]:
        clauses, params = ["1=1"], []
        if reader_contact_id:
            clauses.append("reader_contact_id=?")
            params.append(reader_contact_id)
        if subject_contact_id:
            clauses.append("subject_contact_id=?")
            params.append(subject_contact_id)
        params.append(max(1, min(500, int(limit))))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM tom2_exposures WHERE {' AND '.join(clauses)}"
                " ORDER BY created_at DESC LIMIT ?", params).fetchall()
        return [dict(r) for r in rows]

    def counts(self) -> Dict[str, Any]:
        """Aggregate observability (numbers only)."""
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) n FROM tom2_exposures").fetchone()["n"]
            readers = self._conn.execute(
                "SELECT COUNT(DISTINCT reader_contact_id) n "
                "FROM tom2_exposures").fetchone()["n"]
            pairs = self._conn.execute(
                "SELECT COUNT(*) n FROM (SELECT DISTINCT reader_contact_id,"
                " subject_contact_id FROM tom2_exposures)").fetchone()["n"]
        last24 = self._count("1=1", [], 24.0)
        return {"total": int(total), "readers": int(readers),
                "pairs": int(pairs), "last_24h": last24}

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
