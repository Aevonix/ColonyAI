"""AdaptiveParamStore -- bounded, journaled runtime parameters.

The meta-learning loop (CognitionPipeline -> StrategyAdjuster) needs a place
to write tuning adjustments that running subsystems actually READ BACK. This
store is that place: a small SQLite table of named float parameters, each
registered by its consumer with a default and hard bounds. Every write is
clamped to the registered bounds and journaled through the ActionJournal
(domain "meta_learning"), so a bad self-adjustment is visible, attributable,
and bounded -- never a silent behavior shift.

Consumers read at use-time (no restart needed). Writers (the strategy
adjuster, tools, or the owner via API) go through ``set()`` and never bypass
the clamp.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AdaptiveParamStore:
    """SQLite-backed named float parameters with hard bounds + journaling."""

    def __init__(self, db_path: Optional[str] = None,
                 journal: Any = None) -> None:
        self._lock = threading.RLock()
        self._journal = journal
        self._conn = sqlite3.connect(str(db_path) if db_path else ":memory:",
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS adaptive_params (
                    name TEXT PRIMARY KEY,
                    value REAL,
                    default_value REAL NOT NULL,
                    lo REAL NOT NULL,
                    hi REAL NOT NULL,
                    description TEXT,
                    reason TEXT,
                    source TEXT,
                    updated_at REAL
                )""")
            self._conn.commit()

    def set_journal(self, journal: Any) -> None:
        """Attach the ActionJournal after boot (it is created later than
        this store in the server lifespan)."""
        self._journal = journal

    # -- registration (consumers declare their knob + safe range) ---------
    def register(self, name: str, default: float, lo: float, hi: float,
                 description: str = "") -> None:
        """Idempotently declare a parameter. Bounds/default/description are
        refreshed on re-registration; a previously set value is kept (but
        re-clamped if the new bounds exclude it)."""
        name = (name or "").strip()
        if not name:
            return
        if lo > hi:
            lo, hi = hi, lo
        default = min(max(float(default), lo), hi)
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM adaptive_params WHERE name=?",
                (name,)).fetchone()
            value = row["value"] if row is not None else None
            if value is not None:
                value = min(max(float(value), lo), hi)
            self._conn.execute(
                """INSERT INTO adaptive_params
                     (name, value, default_value, lo, hi, description, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     value=?, default_value=?, lo=?, hi=?, description=?""",
                (name, value, default, lo, hi, description, time.time(),
                 value, default, lo, hi, description))
            self._conn.commit()

    # -- reads -------------------------------------------------------------
    def get(self, name: str, default: Optional[float] = None) -> float:
        """Current value: the set value if any, else the registered default,
        else the caller-supplied fallback (0.0 if none)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value, default_value FROM adaptive_params WHERE name=?",
                ((name or "").strip(),)).fetchone()
        if row is None:
            return float(default) if default is not None else 0.0
        if row["value"] is not None:
            return float(row["value"])
        return float(row["default_value"])

    # -- writes (clamped + journaled) ---------------------------------------
    def set(self, name: str, value: float, *, reason: str = "",
            source: str = "") -> Optional[float]:
        """Set a parameter. Returns the APPLIED (clamped) value, or None if
        the parameter was never registered (unknown knobs are refused --
        a writer cannot invent parameters no consumer reads)."""
        name = (name or "").strip()
        with self._lock:
            row = self._conn.execute(
                "SELECT lo, hi, value FROM adaptive_params WHERE name=?",
                (name,)).fetchone()
            if row is None:
                logger.warning("AdaptiveParamStore: refusing unregistered "
                               "param %r", name)
                return None
            applied = min(max(float(value), row["lo"]), row["hi"])
            prior = row["value"]
            self._conn.execute(
                """UPDATE adaptive_params
                   SET value=?, reason=?, source=?, updated_at=?
                   WHERE name=?""",
                (applied, reason, source, time.time(), name))
            self._conn.commit()
        clamped = abs(applied - float(value)) > 1e-9
        logger.info("AdaptiveParamStore: %s %s -> %s%s (%s)", name,
                    prior, applied, " [clamped]" if clamped else "",
                    reason or source or "unattributed")
        if self._journal is not None:
            try:
                self._journal.record(
                    "meta_learning",
                    f"adaptive param {name}: {prior} -> {applied}"
                    + (" (clamped)" if clamped else ""),
                    reasoning=reason, decision="acted",
                    outcome="applied", ref=source or "adaptive_params")
            except Exception:
                logger.debug("param journal write failed", exc_info=True)
        return applied

    def reset(self, name: str, *, reason: str = "", source: str = "") -> None:
        """Clear a set value so the registered default applies again."""
        with self._lock:
            self._conn.execute(
                "UPDATE adaptive_params SET value=NULL, reason=?, source=?, "
                "updated_at=? WHERE name=?",
                (reason, source, time.time(), (name or "").strip()))
            self._conn.commit()

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM adaptive_params ORDER BY name").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["effective"] = (float(d["value"]) if d["value"] is not None
                              else float(d["default_value"]))
            out.append(d)
        return out

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


# Canonical parameter names (registered at boot; consumers read these).
PARAM_CONSOLIDATION_THRESHOLD = "consolidation.similarity_threshold"
PARAM_RECALL_MIN_RELEVANCE = "recall.min_relevance"


def register_core_params(store: AdaptiveParamStore) -> None:
    """Register the parameters core subsystems consume."""
    store.register(
        PARAM_CONSOLIDATION_THRESHOLD, default=0.92, lo=0.85, hi=0.98,
        description="MemoryConsolidator merge threshold: pairs at or above "
                    "this similarity are deduplicated. Hard floor 0.85 so a "
                    "self-adjustment can never mass-merge distinct memories.")
    store.register(
        PARAM_RECALL_MIN_RELEVANCE, default=0.0, lo=0.0, hi=0.5,
        description="ColonyGraph.recall drops vector hits scoring below "
                    "this. 0 = no filter; capped at 0.5 so retrieval can "
                    "never be starved by a self-adjustment.")
