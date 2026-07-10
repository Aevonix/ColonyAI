"""Directed-action data model: ScopedTask + audit results.

A ScopedTask is the deterministic contract for one owner directive
("look at X, do Y") executed by DELEGATION (option A): Colony never mutates
anything itself. The scope spec is pure data -- targets, an allowed-operation
vocabulary, and mechanical limits -- so gates, the delegate, and the
post-action audit all reason over the same structure.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

# Fixed operation vocabulary. Anything outside the read set makes a task
# MUTATING and therefore owner-approval-gated.
READ_OPS = frozenset({"analyze", "read", "search", "run_tests"})
MUTATE_OPS = frozenset({"modify_files", "commit", "push_branch", "open_pr"})
ALL_OPS = READ_OPS | MUTATE_OPS

# Task lifecycle.
STATUSES = (
    "draft", "refused", "awaiting_approval", "approved",
    "dispatched", "dispatched_dry", "completed", "violated", "failed", "expired",
)


@dataclass
class ScopeLimits:
    """Mechanical limits the delegate must work within."""
    branch_prefix: str = "colony/"     # all work on a dedicated branch
    max_commits: int = 5
    force_push: bool = False           # never allowed; kept explicit for audit
    delete_allowed: bool = False
    path_globs: List[str] = field(default_factory=lambda: ["**"])
    expires_hours: float = 24.0


@dataclass
class ScopedTask:
    directive_text: str                          # the owner's words (provenance)
    objective: str = ""                          # cleaned task statement
    targets: List[Dict[str, str]] = field(default_factory=list)  # [{kind,name,ref?}]
    allowed_ops: List[str] = field(default_factory=lambda: ["analyze", "read", "search"])
    limits: ScopeLimits = field(default_factory=ScopeLimits)
    reporting: List[str] = field(default_factory=lambda: [
        "summary", "operations", "files_touched", "commits", "branch",
    ])
    status: str = "draft"
    refusal_reason: str = ""
    approval: Dict[str, Any] = field(default_factory=dict)   # {required, granted_by, standing}
    audit: Dict[str, Any] = field(default_factory=dict)      # filled post-action
    id: str = field(default_factory=lambda: f"stask-{uuid.uuid4().hex[:12]}")
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None

    def __post_init__(self) -> None:
        self.allowed_ops = [op for op in self.allowed_ops if op in ALL_OPS]
        if not self.allowed_ops:
            self.allowed_ops = ["analyze"]
        if isinstance(self.limits, dict):
            self.limits = ScopeLimits(**self.limits)
        if self.expires_at is None:
            self.expires_at = self.created_at + self.limits.expires_hours * 3600

    @property
    def mutating(self) -> bool:
        return any(op in MUTATE_OPS for op in self.allowed_ops)

    @property
    def approval_key(self) -> str:
        """Stable key for standing approvals of a repeat scope."""
        tnames = ",".join(sorted(t.get("name", "") for t in self.targets))
        ops = ",".join(sorted(self.allowed_ops))
        return f"directed:{tnames}:{ops}"

    def is_expired(self, now: Optional[float] = None) -> bool:
        return (now or time.time()) > (self.expires_at or 0)

    def searchable_text(self) -> str:
        names = " ".join(t.get("name", "") for t in self.targets)
        return f"{self.objective} {self.directive_text} {names}"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["mutating"] = self.mutating
        return d

    def to_row(self) -> Dict[str, Any]:
        return {
            "id": self.id, "directive_text": self.directive_text,
            "objective": self.objective, "targets": json.dumps(self.targets),
            "allowed_ops": json.dumps(self.allowed_ops),
            "limits": json.dumps(asdict(self.limits)),
            "reporting": json.dumps(self.reporting), "status": self.status,
            "refusal_reason": self.refusal_reason,
            "approval": json.dumps(self.approval), "audit": json.dumps(self.audit),
            "created_at": self.created_at, "expires_at": self.expires_at,
        }

    @classmethod
    def from_row(cls, r: Dict[str, Any]) -> "ScopedTask":
        def _j(key, default):
            try:
                return json.loads(r.get(key) or "")
            except Exception:
                return default
        t = cls(
            id=r["id"], directive_text=r.get("directive_text", "") or "",
            objective=r.get("objective", "") or "",
            targets=_j("targets", []), allowed_ops=_j("allowed_ops", ["analyze"]),
            limits=ScopeLimits(**_j("limits", {})),
            reporting=_j("reporting", []), status=r.get("status", "draft"),
            refusal_reason=r.get("refusal_reason", "") or "",
            approval=_j("approval", {}), audit=_j("audit", {}),
            created_at=float(r.get("created_at") or time.time()),
            expires_at=(float(r["expires_at"]) if r.get("expires_at") else None),
        )
        return t


class ScopedTaskStore:
    """SQLite persistence for ScopedTasks (survives restarts)."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path) if db_path else ":memory:",
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS scoped_tasks (
                    id TEXT PRIMARY KEY, directive_text TEXT, objective TEXT,
                    targets TEXT, allowed_ops TEXT, limits TEXT, reporting TEXT,
                    status TEXT, refusal_reason TEXT, approval TEXT, audit TEXT,
                    created_at REAL, expires_at REAL
                )""")
            # Durable dispatch log (additive; feeds the daily dispatch cap).
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS dispatch_log (
                    task_id TEXT, ts REAL, mode TEXT
                )""")
            self._conn.commit()

    def save(self, task: ScopedTask) -> ScopedTask:
        row = task.to_row()
        with self._lock:
            cols = ", ".join(row); ph = ", ".join(["?"] * len(row))
            updates = ", ".join(f"{k}=excluded.{k}" for k in row if k != "id")
            self._conn.execute(
                f"INSERT INTO scoped_tasks ({cols}) VALUES ({ph}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}", list(row.values()))
            self._conn.commit()
        return task

    def get(self, task_id: str) -> Optional[ScopedTask]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM scoped_tasks WHERE id=?", (task_id,)).fetchone()
        return ScopedTask.from_row(dict(r)) if r else None

    def log_dispatch(self, task_id: str, mode: str) -> None:
        """Record one dispatch operation (dry or live) for the daily cap."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO dispatch_log (task_id, ts, mode) VALUES (?,?,?)",
                (task_id, time.time(), mode))
            self._conn.commit()

    def dispatches_since(self, since_ts: float) -> int:
        """Count dispatch operations recorded at or after ``since_ts``."""
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) FROM dispatch_log WHERE ts >= ?",
                (since_ts,)).fetchone()
        return int(r[0]) if r else 0

    def list(self, status: Optional[str] = None, limit: int = 50) -> List[ScopedTask]:
        q = "SELECT * FROM scoped_tasks"
        params: List[Any] = []
        if status:
            q += " WHERE status=?"; params.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [ScopedTask.from_row(dict(r)) for r in rows]
