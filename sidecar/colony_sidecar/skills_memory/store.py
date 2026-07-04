"""SkillStore -- SQLite persistence for procedure memory + strategy notes."""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from colony_sidecar.skills_memory.models import Skill, signature_overlap

logger = logging.getLogger(__name__)

_DEFAULT_MAX = 200
_NOTE_CAP = 600


def skills_enabled() -> bool:
    return os.environ.get("COLONY_SKILLS_ENABLED", "true").strip().lower() != "false"


def skills_distill_mode() -> str:
    m = os.environ.get("COLONY_SKILLS_DISTILL", "shadow").strip().lower()
    return m if m in ("off", "shadow", "live") else "shadow"


def skills_max() -> int:
    try:
        return max(10, int(os.environ.get("COLONY_SKILLS_MAX", str(_DEFAULT_MAX))))
    except (TypeError, ValueError):
        return _DEFAULT_MAX


class SkillStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path) if db_path else ":memory:",
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS skills (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, situation TEXT,
                    situation_signature TEXT, steps TEXT, gotchas TEXT,
                    domain TEXT, source_ref TEXT,
                    uses INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0, confidence REAL DEFAULT 0.6,
                    created_at REAL, last_used_at REAL
                )""")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS strategy_notes (
                    domain TEXT PRIMARY KEY, note TEXT, updated_at REAL
                )""")
            self._conn.commit()

    # -- skills ----------------------------------------------------------
    def add(self, skill: Skill) -> Skill:
        row = skill.to_row()
        with self._lock:
            cols = ", ".join(row); ph = ", ".join(["?"] * len(row))
            self._conn.execute(
                f"INSERT OR REPLACE INTO skills ({cols}) VALUES ({ph})",
                list(row.values()))
            self._conn.commit()
        return skill

    def get(self, skill_id: str) -> Optional[Skill]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
        return Skill.from_row(dict(r)) if r else None

    def list(self, domain: Optional[str] = None, limit: int = 100) -> List[Skill]:
        q = "SELECT * FROM skills"
        params: List[Any] = []
        if domain:
            q += " WHERE domain=?"; params.append(domain)
        q += " ORDER BY last_used_at DESC, created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [Skill.from_row(dict(r)) for r in rows]

    def count(self) -> int:
        with self._lock:
            r = self._conn.execute("SELECT COUNT(*) AS n FROM skills").fetchone()
        return int(r["n"])

    def find_similar(self, signature: str, threshold: float = 0.8) -> Optional[Skill]:
        """Existing skill whose signature overlaps `signature` above threshold."""
        best, best_ov = None, 0.0
        for s in self.list(limit=100000):
            ov = signature_overlap(signature, s.situation_signature)
            if ov > best_ov:
                best, best_ov = s, ov
        return best if best is not None and best_ov > threshold else None

    def bump_use(self, skill_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE skills SET uses=uses+1, last_used_at=? WHERE id=?",
                (time.time(), skill_id))
            self._conn.commit()

    def record_outcome(self, skill_id: str, win: bool) -> None:
        col = "wins" if win else "losses"
        with self._lock:
            self._conn.execute(
                f"UPDATE skills SET {col}={col}+1 WHERE id=?", (skill_id,))
            self._conn.commit()

    def evict_to_cap(self, cap: Optional[int] = None) -> int:
        """Drop lowest-scoring skills until at most `cap` remain."""
        cap = cap if cap is not None else skills_max()
        skills = self.list(limit=100000)
        excess = len(skills) - cap
        if excess <= 0:
            return 0
        now = time.time()
        victims = sorted(skills, key=lambda s: s.score(now))[:excess]
        with self._lock:
            for v in victims:
                self._conn.execute("DELETE FROM skills WHERE id=?", (v.id,))
            self._conn.commit()
        logger.info("skills_memory: evicted %d skill(s) to cap %d", excess, cap)
        return excess

    # -- per-domain strategy notes (failure post-mortems) -----------------
    def record_failure_note(self, domain: str, note: str) -> None:
        """Append/refresh the short strategy note for a domain (capped)."""
        domain = (domain or "unknown").strip().lower()
        note = (note or "").strip()
        if not note:
            return
        with self._lock:
            row = self._conn.execute(
                "SELECT note FROM strategy_notes WHERE domain=?", (domain,)).fetchone()
            existing = (row["note"] if row else "") or ""
            if note in existing:
                merged = existing
            else:
                merged = (existing + ("\n" if existing else "") + f"- {note}")
            # keep the newest lines within the cap
            if len(merged) > _NOTE_CAP:
                lines = merged.splitlines()
                while lines and len("\n".join(lines)) > _NOTE_CAP:
                    lines.pop(0)
                merged = "\n".join(lines)
            self._conn.execute(
                """INSERT INTO strategy_notes (domain, note, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(domain) DO UPDATE SET note=?, updated_at=?""",
                (domain, merged, time.time(), merged, time.time()))
            self._conn.commit()

    def get_note(self, domain: str) -> str:
        domain = (domain or "").strip().lower()
        with self._lock:
            row = self._conn.execute(
                "SELECT note FROM strategy_notes WHERE domain=?", (domain,)).fetchone()
        return (row["note"] if row else "") or ""

    def snapshot(self) -> Dict[str, Any]:
        return {
            "count": self.count(),
            "cap": skills_max(),
            "distill_mode": skills_distill_mode(),
            "skills": [s.to_row() for s in self.list(limit=200)],
        }
