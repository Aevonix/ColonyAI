"""ProjectStore -- SQLite persistence for projects + steps (survives restarts)."""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, List, Optional

from colony_sidecar.projects.models import Project, Step

class ProjectStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path) if db_path else ":memory:",
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, objective TEXT,
                    source TEXT, status TEXT, entity_ids TEXT, reason TEXT,
                    replans INTEGER DEFAULT 0, next_review_at REAL,
                    created_at REAL, updated_at REAL
                )""")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS steps (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    ordinal INTEGER, description TEXT, action_kind TEXT,
                    depends_on TEXT, status TEXT, attempts INTEGER DEFAULT 0,
                    result TEXT, boundary_subject TEXT,
                    confidence REAL DEFAULT 0.6,
                    created_at REAL, updated_at REAL
                )""")
            # Migration: confidence added after first ship (charter contract).
            try:
                cols = {r[1] for r in self._conn.execute(
                    "PRAGMA table_info(steps)").fetchall()}
                if "confidence" not in cols:
                    self._conn.execute(
                        "ALTER TABLE steps ADD COLUMN confidence REAL DEFAULT 0.6")
            except Exception:
                pass
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_steps_project ON steps(project_id)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status)")
            self._conn.commit()

    # -- projects ---------------------------------------------------------
    def save_project(self, p: Project) -> Project:
        p.updated_at = time.time()
        row = p.to_row()
        with self._lock:
            cols = ", ".join(row); ph = ", ".join(["?"] * len(row))
            updates = ", ".join(f"{k}=excluded.{k}" for k in row if k != "id")
            self._conn.execute(
                f"INSERT INTO projects ({cols}) VALUES ({ph}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}", list(row.values()))
            self._conn.commit()
        return p

    def get_project(self, project_id: str) -> Optional[Project]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return Project.from_row(dict(r)) if r else None

    def list_projects(self, status: Optional[str] = None,
                      limit: int = 50) -> List[Project]:
        q = "SELECT * FROM projects"
        params: List[Any] = []
        if status:
            q += " WHERE status=?"; params.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [Project.from_row(dict(r)) for r in rows]

    def count(self, status: Optional[str] = None) -> int:
        q = "SELECT COUNT(*) AS n FROM projects"
        params: List[Any] = []
        if status:
            q += " WHERE status=?"; params.append(status)
        with self._lock:
            r = self._conn.execute(q, params).fetchone()
        return int(r["n"])

    def due_for_review(self, now: Optional[float] = None,
                       limit: int = 20) -> List[Project]:
        """Active projects whose next_review_at has passed."""
        now = now or time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM projects WHERE status='active' AND "
                "coalesce(next_review_at, 0) <= ? "
                "ORDER BY next_review_at ASC LIMIT ?", (now, limit)).fetchall()
        return [Project.from_row(dict(r)) for r in rows]

    # -- steps --------------------------------------------------------------
    def save_step(self, s: Step) -> Step:
        s.updated_at = time.time()
        row = s.to_row()
        with self._lock:
            cols = ", ".join(row); ph = ", ".join(["?"] * len(row))
            updates = ", ".join(f"{k}=excluded.{k}" for k in row if k != "id")
            self._conn.execute(
                f"INSERT INTO steps ({cols}) VALUES ({ph}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}", list(row.values()))
            self._conn.commit()
        return s

    def steps_for(self, project_id: str) -> List[Step]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM steps WHERE project_id=? ORDER BY ordinal ASC",
                (project_id,)).fetchall()
        return [Step.from_row(dict(r)) for r in rows]

    def delete_steps(self, project_id: str,
                     statuses: Optional[List[str]] = None) -> int:
        """Delete steps of a project (optionally only certain statuses)."""
        q = "DELETE FROM steps WHERE project_id=?"
        params: List[Any] = [project_id]
        if statuses:
            q += f" AND status IN ({','.join(['?'] * len(statuses))})"
            params.extend(statuses)
        with self._lock:
            cur = self._conn.execute(q, params)
            self._conn.commit()
        return cur.rowcount
