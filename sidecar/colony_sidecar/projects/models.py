"""Project + Step data model."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Fixed step-action vocabulary. Each kind routes through an existing gated
# sub-path; anything outside this set is dropped at validation.
#   analyze / research / internal -> sidecar reasoning turn (internal tools)
#   directed                      -> DirectedActionService (dry_run/approval)
#   deliver                       -> guarded proposal delivery
ACTION_KINDS = frozenset({"analyze", "research", "directed", "deliver", "internal"})

PROJECT_STATUSES = ("planning", "active", "blocked", "completed", "abandoned")
STEP_STATUSES = ("pending", "active", "done", "failed", "skipped")


def projects_mode() -> str:
    m = os.environ.get("COLONY_PROJECTS_MODE", "shadow").strip().lower()
    return m if m in ("off", "shadow", "live") else "shadow"


def projects_max_steps() -> int:
    try:
        return max(1, min(50, int(os.environ.get("COLONY_PROJECTS_MAX_STEPS", "12"))))
    except (TypeError, ValueError):
        return 12


def projects_max_replans() -> int:
    try:
        return max(0, int(os.environ.get("COLONY_PROJECTS_MAX_REPLANS", "3")))
    except (TypeError, ValueError):
        return 3


def projects_review_secs() -> float:
    try:
        return max(30.0, float(os.environ.get("COLONY_PROJECTS_REVIEW_SECS", "900")))
    except (TypeError, ValueError):
        return 900.0


@dataclass
class Project:
    title: str
    objective: str = ""
    source: str = "owner"               # owner | thinker | directive
    status: str = "planning"
    entity_ids: List[str] = field(default_factory=list)
    reason: str = ""                    # abandon/complete/blocked reason
    replans: int = 0
    next_review_at: float = 0.0         # 0 -> due immediately
    id: str = field(default_factory=lambda: f"proj-{uuid.uuid4().hex[:12]}")
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def subject_text(self) -> str:
        """Boundary-checkable subject for this project."""
        return f"{self.title} {self.objective}"

    def to_row(self) -> Dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "objective": self.objective,
            "source": self.source, "status": self.status,
            "entity_ids": json.dumps(self.entity_ids), "reason": self.reason,
            "replans": self.replans, "next_review_at": self.next_review_at,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, r: Dict[str, Any]) -> "Project":
        try:
            eids = json.loads(r.get("entity_ids") or "[]")
        except Exception:
            eids = []
        return cls(
            id=r["id"], title=r.get("title", "") or "",
            objective=r.get("objective", "") or "",
            source=r.get("source", "owner") or "owner",
            status=r.get("status", "planning") or "planning",
            entity_ids=eids if isinstance(eids, list) else [],
            reason=r.get("reason", "") or "",
            replans=int(r.get("replans") or 0),
            next_review_at=float(r.get("next_review_at") or 0.0),
            created_at=float(r.get("created_at") or time.time()),
            updated_at=float(r.get("updated_at") or time.time()),
        )


@dataclass
class Step:
    project_id: str
    ordinal: int
    description: str
    action_kind: str = "analyze"
    depends_on: List[int] = field(default_factory=list)   # ordinals within project
    status: str = "pending"
    attempts: int = 0
    result: str = ""
    boundary_subject: str = ""          # extra subject text for the guard
    confidence: float = 0.6             # planner-stated (charter contract)
    id: str = field(default_factory=lambda: f"step-{uuid.uuid4().hex[:12]}")
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_row(self) -> Dict[str, Any]:
        return {
            "id": self.id, "project_id": self.project_id, "ordinal": self.ordinal,
            "description": self.description, "action_kind": self.action_kind,
            "depends_on": json.dumps(self.depends_on), "status": self.status,
            "attempts": self.attempts, "result": self.result,
            "boundary_subject": self.boundary_subject,
            "confidence": self.confidence,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, r: Dict[str, Any]) -> "Step":
        try:
            deps = json.loads(r.get("depends_on") or "[]")
            deps = [int(d) for d in deps] if isinstance(deps, list) else []
        except Exception:
            deps = []
        return cls(
            id=r["id"], project_id=r.get("project_id", "") or "",
            ordinal=int(r.get("ordinal") or 0),
            description=r.get("description", "") or "",
            action_kind=r.get("action_kind", "analyze") or "analyze",
            depends_on=deps, status=r.get("status", "pending") or "pending",
            attempts=int(r.get("attempts") or 0),
            result=r.get("result", "") or "",
            boundary_subject=r.get("boundary_subject", "") or "",
            confidence=float(r.get("confidence", 0.6) or 0.6),
            created_at=float(r.get("created_at") or time.time()),
            updated_at=float(r.get("updated_at") or time.time()),
        )
