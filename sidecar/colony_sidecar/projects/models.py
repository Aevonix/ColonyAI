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
    from colony_sidecar.util.autonomy_preset import resolve
    return resolve("COLONY_PROJECTS_MODE", ("off", "shadow", "live"), "shadow")


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
    outcome: str = "pending"             # lifecycle status != evidence outcome
    entity_ids: List[str] = field(default_factory=list)
    reason: str = ""                    # abandon/complete/blocked reason
    replans: int = 0
    next_review_at: float = 0.0         # 0 -> due immediately
    # P3 autonomous-goal provenance.  Legacy/owner projects leave these
    # empty; cognition-spine projects carry every upstream ID and scope into
    # WorkOrder.context_refs without copying private context text.
    concern_id: str = ""
    source_event_refs: List[str] = field(default_factory=list)
    thought_job_id: str = ""
    thought_result_ref: str = ""
    goal_proposal_id: str = ""
    evidence_refs: List[str] = field(default_factory=list)
    policy_decision_refs: List[str] = field(default_factory=list)
    subject_person_id: str = ""
    viewer_scope: str = "owner"
    shareability: str = "owner_private"
    capability_allowlist: List[str] = field(default_factory=list)
    goal_fingerprint: str = ""
    id: str = field(default_factory=lambda: f"proj-{uuid.uuid4().hex[:12]}")
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def subject_text(self) -> str:
        """Boundary-checkable subject for this project."""
        return f"{self.title} {self.objective}"

    def provenance_refs(self) -> List[str]:
        """Return bounded reference-only lineage for a WorkOrder."""

        refs: List[str] = []
        if self.concern_id:
            refs.append(f"concern:{self.concern_id}")
        refs.extend(self.source_event_refs)
        if self.thought_job_id:
            refs.append(f"thought-job:{self.thought_job_id}")
        if self.thought_result_ref:
            refs.append(self.thought_result_ref)
        if self.goal_proposal_id:
            refs.append(self.goal_proposal_id)
        refs.extend(self.evidence_refs)
        refs.extend(self.policy_decision_refs)
        return list(dict.fromkeys(ref for ref in refs if ref))[:60]

    def to_row(self) -> Dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "objective": self.objective,
            "source": self.source, "status": self.status,
            "outcome": self.outcome,
            "entity_ids": json.dumps(self.entity_ids), "reason": self.reason,
            "replans": self.replans, "next_review_at": self.next_review_at,
            "concern_id": self.concern_id,
            "source_event_refs": json.dumps(self.source_event_refs),
            "thought_job_id": self.thought_job_id,
            "thought_result_ref": self.thought_result_ref,
            "goal_proposal_id": self.goal_proposal_id,
            "evidence_refs": json.dumps(self.evidence_refs),
            "policy_decision_refs": json.dumps(self.policy_decision_refs),
            "subject_person_id": self.subject_person_id,
            "viewer_scope": self.viewer_scope,
            "shareability": self.shareability,
            "capability_allowlist": json.dumps(self.capability_allowlist),
            "goal_fingerprint": self.goal_fingerprint,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, r: Dict[str, Any]) -> "Project":
        def _list(name: str) -> List[str]:
            try:
                value = json.loads(r.get(name) or "[]")
                return [str(item) for item in value] if isinstance(value, list) else []
            except Exception:
                return []

        return cls(
            id=r["id"], title=r.get("title", "") or "",
            objective=r.get("objective", "") or "",
            source=r.get("source", "owner") or "owner",
            status=r.get("status", "planning") or "planning",
            outcome=r.get("outcome", "pending") or "pending",
            entity_ids=_list("entity_ids"),
            reason=r.get("reason", "") or "",
            replans=int(r.get("replans") or 0),
            next_review_at=float(r.get("next_review_at") or 0.0),
            concern_id=r.get("concern_id", "") or "",
            source_event_refs=_list("source_event_refs"),
            thought_job_id=r.get("thought_job_id", "") or "",
            thought_result_ref=r.get("thought_result_ref", "") or "",
            goal_proposal_id=r.get("goal_proposal_id", "") or "",
            evidence_refs=_list("evidence_refs"),
            policy_decision_refs=_list("policy_decision_refs"),
            subject_person_id=r.get("subject_person_id", "") or "",
            viewer_scope=r.get("viewer_scope", "owner") or "owner",
            shareability=r.get("shareability", "owner_private") or "owner_private",
            capability_allowlist=_list("capability_allowlist"),
            goal_fingerprint=r.get("goal_fingerprint", "") or "",
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
    work_order_ref: str = ""          # immutable project-ledger WorkOrder row
    work_order_digest: str = ""       # exact authority envelope digest
    work_order_issued_at: float = 0.0  # stable first-dispatch authority time
    result_ref: str = ""              # logical ExecutionResultV1 ledger row
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
            "work_order_ref": self.work_order_ref,
            "work_order_digest": self.work_order_digest,
            "work_order_issued_at": self.work_order_issued_at,
            "result_ref": self.result_ref,
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
            work_order_ref=r.get("work_order_ref", "") or "",
            work_order_digest=r.get("work_order_digest", "") or "",
            work_order_issued_at=float(r.get("work_order_issued_at") or 0.0),
            result_ref=r.get("result_ref", "") or "",
            boundary_subject=r.get("boundary_subject", "") or "",
            confidence=float(r.get("confidence", 0.6) or 0.6),
            created_at=float(r.get("created_at") or time.time()),
            updated_at=float(r.get("updated_at") or time.time()),
        )
