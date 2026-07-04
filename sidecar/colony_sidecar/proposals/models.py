"""Proposal artifact + store.

A Proposal is a well-formed thing Colony wants to put in front of the owner:
a finding (what it noticed / learned), why it helps him, a concrete suggested
action, and citations. It is a DEDICATED type, distinct from routine reach-out
initiatives, delivered through the same guarded (shadow-gated, boundary-checked,
rate-limited) path.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Proposal:
    title: str
    finding: str = ""                 # what Colony noticed / learned
    why_it_helps: str = ""            # relevance to the owner
    suggested_action: str = ""        # concrete next step
    citations: List[Dict[str, str]] = field(default_factory=list)  # [{title,url}]
    source: str = "thinker"           # thinker | research | <initiative_id>
    initiative_type: str = "research"
    confidence: float = 0.6
    status: str = "shadow"            # shadow | delivered | dismissed | draft
    id: str = field(default_factory=lambda: f"prop-{uuid.uuid4().hex[:12]}")
    created_at: float = field(default_factory=time.time)

    def render(self) -> str:
        """Owner-facing text handed to the composing agent."""
        lines = [self.title.strip()]
        if self.finding:
            lines.append(f"What I found: {self.finding.strip()}")
        if self.why_it_helps:
            lines.append(f"Why it helps you: {self.why_it_helps.strip()}")
        if self.suggested_action:
            lines.append(f"Suggested next step: {self.suggested_action.strip()}")
        if self.citations:
            cites = "; ".join(
                (c.get("title") or c.get("url") or "").strip()
                for c in self.citations if (c.get("title") or c.get("url"))
            )
            if cites:
                lines.append(f"Sources: {cites}")
        return "\n".join(lines)

    def to_row(self) -> Dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "finding": self.finding,
            "why_it_helps": self.why_it_helps, "suggested_action": self.suggested_action,
            "citations": json.dumps(self.citations), "source": self.source,
            "initiative_type": self.initiative_type, "confidence": self.confidence,
            "status": self.status, "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, r: Dict[str, Any]) -> "Proposal":
        try:
            cites = json.loads(r.get("citations") or "[]")
        except Exception:
            cites = []
        return cls(
            id=r["id"], title=r["title"], finding=r.get("finding", "") or "",
            why_it_helps=r.get("why_it_helps", "") or "",
            suggested_action=r.get("suggested_action", "") or "",
            citations=cites, source=r.get("source", "thinker") or "thinker",
            initiative_type=r.get("initiative_type", "research") or "research",
            confidence=float(r.get("confidence", 0.6) or 0.6),
            status=r.get("status", "shadow") or "shadow",
            created_at=float(r.get("created_at") or time.time()),
        )


class ProposalStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path) if db_path else ":memory:",
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, finding TEXT,
                    why_it_helps TEXT, suggested_action TEXT, citations TEXT,
                    source TEXT, initiative_type TEXT, confidence REAL,
                    status TEXT, created_at REAL
                )""")
            self._conn.commit()

    def add(self, p: Proposal) -> Proposal:
        row = p.to_row()
        with self._lock:
            cols = ", ".join(row); ph = ", ".join(["?"] * len(row))
            self._conn.execute(
                f"INSERT OR REPLACE INTO proposals ({cols}) VALUES ({ph})",
                list(row.values()))
            self._conn.commit()
        return p

    def list(self, status: Optional[str] = None, limit: int = 50) -> List[Proposal]:
        q = "SELECT * FROM proposals"
        params: List[Any] = []
        if status:
            q += " WHERE status=?"; params.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [Proposal.from_row(dict(r)) for r in rows]

    def set_status(self, proposal_id: str, status: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE proposals SET status=? WHERE id=?", (status, proposal_id))
            self._conn.commit()
            return cur.rowcount > 0

    def count(self, status: Optional[str] = None) -> int:
        return len(self.list(status=status, limit=100000))
