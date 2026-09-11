"""TypeFeedbackStore -- crude outcome-driven priority feedback (item 3b / 4).

Records how the owner responded to each class of proactive work (proposals /
reach-outs / initiatives) and turns that into a per-type priority multiplier:
classes the owner acts on are boosted; classes he dismisses or ignores decay.
This closes the loop the CPI actuator never did, standalone and simple.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Multiplicative nudges per outcome, with clamps so a type never fully dies or
# runs away.
_NUDGE = {"actioned": 1.12, "acted": 1.12, "dismissed": 0.85, "ignored": 0.9,
          "acknowledged": 1.0, "snoozed": 0.97}
_MIN, _MAX = 0.5, 1.5


class TypeFeedbackStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path) if db_path else ":memory:",
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS type_feedback (
                    itype TEXT PRIMARY KEY, multiplier REAL DEFAULT 1.0,
                    actioned INTEGER DEFAULT 0, dismissed INTEGER DEFAULT 0,
                    ignored INTEGER DEFAULT 0, other INTEGER DEFAULT 0,
                    updated_at REAL
                )""")
            self._conn.commit()

    def record(self, itype: str, outcome: str) -> float:
        """Record an outcome for an initiative/proposal type; return new multiplier."""
        itype = (itype or "unknown").strip().lower()
        outcome = (outcome or "").strip().lower()
        nudge = _NUDGE.get(outcome, 1.0)
        col = ("actioned" if nudge > 1.0 else
               "dismissed" if outcome == "dismissed" else
               "ignored" if outcome == "ignored" else "other")
        with self._lock:
            row = self._conn.execute(
                "SELECT multiplier FROM type_feedback WHERE itype=?", (itype,)).fetchone()
            mult = float(row["multiplier"]) if row else 1.0
            mult = max(_MIN, min(_MAX, mult * nudge))
            self._conn.execute(
                f"""INSERT INTO type_feedback (itype, multiplier, {col}, updated_at)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(itype) DO UPDATE SET
                      multiplier=?, {col}={col}+1, updated_at=?""",
                (itype, mult, time.time(), mult, time.time()))
            self._conn.commit()
        logger.info("feedback[%s] outcome=%s -> multiplier=%.3f", itype, outcome, mult)
        return mult

    def multiplier(self, itype: str) -> float:
        itype = (itype or "unknown").strip().lower()
        with self._lock:
            row = self._conn.execute(
                "SELECT multiplier FROM type_feedback WHERE itype=?", (itype,)).fetchone()
        return float(row["multiplier"]) if row else 1.0

    def snapshot(self) -> List[Dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM type_feedback ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]
