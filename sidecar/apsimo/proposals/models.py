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
    route_ref: str = ""
    result_ref: str = ""
    subject_person_id: str = ""
    viewer_scope: str = "owner"
    shareability: str = "owner_private"
    scope_digest: str = ""

    def render(self) -> str:
        """Owner-facing text handed to the composing agent."""
        lines = [self.title.strip()]
        finding = self.finding.strip()
        why = self.why_it_helps.strip()
        if finding:
            lines.append(f"What I found: {finding}")
        # Skip the why-line when it merely restates the finding (the grounded
        # why is often the finding's lead sentence) — no honest information is
        # lost and the message stays tight.
        if why and not (finding and finding.startswith(why)):
            lines.append(f"Why it helps you: {why}")
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
            "route_ref": self.route_ref, "result_ref": self.result_ref,
            "subject_person_id": self.subject_person_id,
            "viewer_scope": self.viewer_scope,
            "shareability": self.shareability,
            "scope_digest": self.scope_digest,
        }

    def visible_to(
        self,
        *,
        viewer_person_id: str,
        owner_person_id: str,
        audiences: set[str] | frozenset[str] = frozenset(),
    ) -> bool:
        viewer = str(viewer_person_id or "").strip()
        if not viewer:
            return False
        if viewer == str(owner_person_id or "").strip():
            return True
        if self.shareability == "public":
            return "global" in audiences
        if self.shareability == "shared":
            return "shared" in audiences
        if self.shareability == "subject_private":
            return viewer == self.subject_person_id
        return False

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
            route_ref=r.get("route_ref", "") or "",
            result_ref=r.get("result_ref", "") or "",
            subject_person_id=r.get("subject_person_id", "") or "",
            viewer_scope=r.get("viewer_scope", "owner") or "owner",
            shareability=r.get("shareability", "owner_private") or "owner_private",
            scope_digest=r.get("scope_digest", "") or "",
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
                    status TEXT, created_at REAL, route_ref TEXT,
                    result_ref TEXT, subject_person_id TEXT,
                    viewer_scope TEXT DEFAULT 'owner',
                    shareability TEXT DEFAULT 'owner_private', scope_digest TEXT
                )""")
            columns = {
                row[1] for row in self._conn.execute(
                    "PRAGMA table_info(proposals)"
                ).fetchall()
            }
            for name, declaration in (
                ("route_ref", "TEXT"),
                ("result_ref", "TEXT"),
                ("subject_person_id", "TEXT"),
                ("viewer_scope", "TEXT DEFAULT 'owner'"),
                ("shareability", "TEXT DEFAULT 'owner_private'"),
                ("scope_digest", "TEXT"),
            ):
                if name not in columns:
                    self._conn.execute(
                        f"ALTER TABLE proposals ADD COLUMN {name} {declaration}"
                    )
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

    def add_if_absent(self, p: Proposal) -> bool:
        """Insert a stable proposal without overwriting operator state."""

        row = p.to_row()
        with self._lock:
            cols = ", ".join(row)
            ph = ", ".join(["?"] * len(row))
            cursor = self._conn.execute(
                f"INSERT OR IGNORE INTO proposals ({cols}) VALUES ({ph})",
                list(row.values()),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def list(self, status: Optional[str] = None, limit: int = 50) -> List[Proposal]:
        q = "SELECT * FROM proposals"
        params: List[Any] = []
        if status:
            q += " WHERE status=?"; params.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [Proposal.from_row(dict(r)) for r in rows]

    def get(self, proposal_id: str) -> Optional[Proposal]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM proposals WHERE id=?", (proposal_id,),
            ).fetchone()
        return Proposal.from_row(dict(row)) if row is not None else None

    def set_status(self, proposal_id: str, status: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE proposals SET status=? WHERE id=?", (status, proposal_id))
            self._conn.commit()
            return cur.rowcount > 0

    def count(self, status: Optional[str] = None) -> int:
        return len(self.list(status=status, limit=100000))
