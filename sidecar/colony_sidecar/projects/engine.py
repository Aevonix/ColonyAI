"""ProjectEngine -- sustained multi-tick pursuit of durable objectives.

Pursued from the autonomy loop's ``_phase_projects`` (after
``_phase_execute``). Each tick: adopt qualifying project-type initiatives,
plan any project still in ``planning`` (one LLM pass, deterministically
validated), then advance due active projects by one ready step each.

Safety posture:
- Every step is boundary-checked (DirectiveGuard) before dispatch; a blocked
  step blocks the whole project (visible, never silent).
- Step dispatch routes through the sub-path that already gates that action
  kind: reasoning turn (internal tools) for analyze/research/internal,
  DirectedActionService (approval tiering + dry_run) for directed, the
  guarded proposal path for deliver. This engine adds NO new outbound or
  mutating primitive of its own; it is orchestration over existing gates.
- COLONY_PROJECTS_MODE=shadow (default): plans for real, logs the exact
  intended step action with its boundary verdict, simulates advancement, and
  stores milestone proposals with status "shadow" WITHOUT routing them to
  delivery. Nothing leaves the machine.
- Uses the self-model (item 4) to defer pursuit under load, and skills memory
  (item 3) to inform planning and distill procedures from completions.

For step EXECUTION beyond the sidecar, deployments point the directed
pipeline's env-configured delegate endpoint at their host framework's job
surface (e.g. a kanban/runs API); this engine deliberately reuses that seam
instead of growing a private execution runner.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple
import uuid

from colony_sidecar.projects.models import (
    Project, Step, projects_max_replans, projects_mode, projects_review_secs,
)
from colony_sidecar.projects.store import ProjectStore

logger = logging.getLogger(__name__)

# Canonical ``internal`` WorkOrders may quote a standalone owner safety clause,
# e.g. ``; do not contact anyone``.  Rechecking that quote as affirmative
# action intent self-blocks the safe WorkOrder.  Split only at hard clause
# boundaries and remove only a plainly negated whole clause.  Reversal and
# double-negation language remains fail-closed.
_BOUNDARY_CLAUSE_SPLIT = re.compile(r"(?:[;\n]+|(?<=[.!?])\s+)")
_NEGATED_CONSTRAINT = re.compile(
    r"^\s*(?:and\s+)?"
    r"(?:do\s+not|don['’]t|never|must\s+not|shall\s+not|no)\b",
    re.IGNORECASE,
)
_NEGATION_REVERSAL = re.compile(
    r"\b(?:not|avoid\w*|refrain\w*|fail\w*|refus\w*|declin\w*|stop\w*)\b",
    re.IGNORECASE,
)


def _boundary_intent_text(*parts: object) -> str:
    """Return executable clauses from an internal action description."""

    kept: List[str] = []
    for part in parts:
        for clause in _BOUNDARY_CLAUSE_SPLIT.split(str(part or "")):
            clause = clause.strip()
            if not clause:
                continue
            match = _NEGATED_CONSTRAINT.match(clause)
            if match and not _NEGATION_REVERSAL.search(clause[match.end():]):
                continue
            kept.append(clause)
    return " ".join(kept)


def _step_boundary_action(project: Project, step: Step):
    """Build the boundary action without weakening effectful step checks."""

    from colony_sidecar.directives import Action
    from colony_sidecar.work_orders import action_authority

    risk = action_authority(step.action_kind)[2]
    if risk == "internal":
        return Action(
            kind=step.action_kind,
            text=_boundary_intent_text(
                step.description, project.title, project.objective,
            ),
            target=step.boundary_subject,
            high_risk=True,
        )
    return Action(
        kind=step.action_kind,
        text=f"{step.description} {step.boundary_subject}",
        target=project.subject_text(),
        high_risk=True,
    )

# Step execution composes through the shared cognition charter (role
# "executor"); this block scopes the turn to ONE project step.
_STEP_SCOPE_CONTEXT = """\
You are executing ONE STEP of a long-running project. Complete THIS STEP
ONLY, then summarize the outcome in 2-4 sentences; later steps are separate
work sessions. If the step cannot be completed, say precisely what is
missing."""

_MAX_TOOL_ROUNDS = 4
_AUTHORITY_BOUND_PROJECT_SOURCES = frozenset({
    "cognition_spine", "governed_action",
})
_GOVERNED_HOLD_REASONS = frozenset({
    "governed_action_requires_live_projects_mode",
    "governed_action_requires_canonical_work_order_adapter",
})
_TURN_CONCERN_HOLD_REASON = "turn_concerns_current_mode_not_live"
_PROJECT_HOLD_REASON_UNAVAILABLE = "project_hold_reason_unavailable"
_TRANSIENT_PROJECT_HOLD_REASONS = frozenset({
    _TURN_CONCERN_HOLD_REASON,
    _PROJECT_HOLD_REASON_UNAVAILABLE,
})


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


class ProjectEngine:
    def __init__(
        self,
        store: ProjectStore,
        *,
        directive_manager: Any = None,
        llm_router: Any = None,          # planning + distillation passes
        reasoning_loop: Any = None,      # analyze/research/internal steps
        tool_executor: Any = None,
        directed_service: Any = None,    # directed steps
        proposal_store: Any = None,
        feedback_store: Any = None,
        self_model: Any = None,
        skill_store: Any = None,
        delivery_router: Any = None,     # async callable(payload) -> bool
        initiative_store: Any = None,    # adoption of project-type initiatives
        work_order_adapter: Any = None,  # canonical external execution bridge
        project_hold_reason: Optional[Callable[[Project], str]] = None,
    ) -> None:
        self.store = store
        self._directives = directive_manager
        self._router = llm_router
        self._reasoning = reasoning_loop
        self._tools = tool_executor
        self._directed = directed_service
        self._proposals = proposal_store
        self._feedback = feedback_store
        self._self_model = self_model
        self._skills = skill_store
        self._deliver = delivery_router
        self._initiatives = initiative_store
        self._work_orders = work_order_adapter
        self._project_hold_reason = project_hold_reason

    def _apply_project_hold(self, project: Project) -> str:
        reason = ""
        if self._project_hold_reason is not None:
            try:
                reason = str(self._project_hold_reason(project) or "")[:200]
            except Exception:
                logger.exception("project hold callback failed closed")
                reason = _PROJECT_HOLD_REASON_UNAVAILABLE
        if reason in _TRANSIENT_PROJECT_HOLD_REASONS:
            # Transient concern-mode holds are visible when they do not mask a
            # pre-existing, unrelated project reason.  The returned reason is
            # still authoritative for eligibility either way.
            if (
                project.reason in _TRANSIENT_PROJECT_HOLD_REASONS
                or not project.reason
            ) and project.reason != reason:
                project.reason = reason
                self.store.save_project(project)
            return reason
        if project.reason in _TRANSIENT_PROJECT_HOLD_REASONS:
            project.reason = ""
            self.store.save_project(project)
        return reason

    def open_capacity_used(self) -> int:
        """Count open work after reconciling exact turn-mode holds.

        Only the exact, known turn-mode hold releases a capacity slot.  A
        callback outage and every ordinary/external/governed project remain
        capacity-bearing and therefore fail conservatively.
        """

        used = 0
        for status in ("planning", "active"):
            offset = 0
            while True:
                page = self.store.list_projects(
                    status=status, limit=100, offset=offset,
                )
                if not page:
                    break
                offset += len(page)
                for project in page:
                    hold = self._apply_project_hold(project)
                    if hold != _TURN_CONCERN_HOLD_REASON:
                        used += 1
                if len(page) < 100:
                    break
        return used

    def _eligible_projects(
        self,
        *,
        status: str,
        limit: int,
        held_ids: Optional[Set[str]] = None,
        due: bool = False,
    ) -> List[Project]:
        """Scan past held rows so they cannot starve ordinary work."""

        selected: List[Project] = []
        offset = 0
        page_size = max(25, limit)
        while len(selected) < limit:
            page = (
                self.store.due_for_review(limit=page_size, offset=offset)
                if due else
                self.store.list_projects(
                    status=status, limit=page_size, offset=offset,
                )
            )
            if not page:
                break
            offset += len(page)
            for project in page:
                if self._apply_project_hold(project):
                    if held_ids is not None:
                        held_ids.add(project.id)
                    continue
                selected.append(project)
                if len(selected) >= limit:
                    break
            if len(page) < page_size:
                break
        return selected

    def _has_canonical_work_order_adapter(self) -> bool:
        try:
            from colony_sidecar.work_orders import QueueWorkOrderAdapter
        except Exception:
            return False
        return (
            isinstance(self._work_orders, QueueWorkOrderAdapter)
            and self._work_orders.project_store is self.store
        )

    def _governed_hold_reason(self, mode: str) -> str:
        if mode != "live" or projects_mode() != "live":
            return "governed_action_requires_live_projects_mode"
        if not self._has_canonical_work_order_adapter():
            return "governed_action_requires_canonical_work_order_adapter"
        return ""

    @staticmethod
    def _governed_identity(project: Project) -> str:
        source_refs = list(project.source_event_refs)
        evidence_refs = list(project.evidence_refs)
        if (
            len(source_refs) != 2
            or len(evidence_refs) != 4
            or not source_refs[0].startswith("governed-action:")
            or not source_refs[1].startswith("governed-intent:")
            or not evidence_refs[0].startswith("action-digest:")
            or not evidence_refs[1].startswith("intent-digest:")
            or not evidence_refs[2].startswith("args-digest:")
            or not evidence_refs[3].startswith("execution-digest:")
        ):
            raise ValueError("governed research provenance is incomplete")
        action_id = source_refs[0].split(":", 1)[1]
        intent_id = source_refs[1].split(":", 1)[1]
        try:
            parsed_action_id = uuid.UUID(action_id)
        except (AttributeError, ValueError) as exc:
            raise ValueError("governed research action ID is invalid") from exc
        if (
            parsed_action_id.version != 4
            or str(parsed_action_id) != action_id
            or not re.fullmatch(r"hti_[0-9a-f]{32}", intent_id)
        ):
            raise ValueError("governed research identity is invalid")
        digests = []
        for index, prefix in enumerate((
            "action-digest:", "intent-digest:", "args-digest:",
            "execution-digest:",
        )):
            if not evidence_refs[index].startswith(prefix):
                raise ValueError("governed research digest provenance is invalid")
            digests.append(evidence_refs[index].split(":", 1)[1])
        if any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in digests
        ):
            raise ValueError("governed research identity is invalid")
        action_digest, _intent_digest, _args_digest, execution_digest = digests
        material = {
            "schema": "ColonyGovernedResearchProjectIdentityV1",
            "version": 1,
            "owner_person_id": project.subject_person_id,
            "action_id": action_id,
            "action_digest": action_digest,
            "execution_digest": execution_digest,
        }
        encoded = json.dumps(
            material, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def prepare_governed_research(self, project: Project) -> None:
        """Validate one owner-approved, read-only durable project handoff."""

        if not isinstance(project, Project) or project.source != "governed_action":
            raise RuntimeError("governed research requires a governed Project")
        if projects_mode() != "live":
            raise RuntimeError("governed research requires ProjectEngine live mode")
        if not self._has_canonical_work_order_adapter():
            raise RuntimeError(
                "governed research requires the canonical WorkOrder adapter"
            )
        try:
            from colony_sidecar.work_orders import action_authority
            capabilities = list(action_authority("research")[1])
        except Exception as exc:
            raise RuntimeError("governed research WorkOrder authority is unavailable") from exc
        identity = self._governed_identity(project)
        expected_id = "proj-governed-" + identity[:20]
        expected_title = "Governed research " + identity[:8]
        if (
            project.id != expected_id
            or project.goal_fingerprint != identity
            or project.title != expected_title
            or project.status != "planning"
            or project.outcome != "pending"
            or project.reason
            or project.replans != 0
            or project.next_review_at != 0.0
            or project.entity_ids
            or not project.subject_person_id
            or project.viewer_scope != "owner"
            or project.shareability != "owner_private"
            or project.capability_allowlist != capabilities
        ):
            raise RuntimeError("governed research Project authority is invalid")
        prefix = "Research topic:\n"
        if not project.objective.startswith(prefix) or "\nDepth: " not in project.objective:
            raise RuntimeError("governed research objective is invalid")
        topic, depth = project.objective[len(prefix):].rsplit("\nDepth: ", 1)
        if not topic or len(topic) > 1400 or depth not in {"quick", "standard", "deep"}:
            raise RuntimeError("governed research objective is outside its bound")
        policy_refs = list(project.policy_decision_refs)
        if (
            len(policy_refs) != 4
            or not policy_refs[0].startswith("approval:")
            or not policy_refs[1].startswith("decision:")
            or not policy_refs[2].startswith("approval-revision:")
            or not policy_refs[3].startswith("authorization-receipt-digest:")
        ):
            raise RuntimeError("governed research decision provenance is incomplete")
        approval_id = policy_refs[0].split(":", 1)[1]
        decision_id = policy_refs[1].split(":", 1)[1]
        revision = policy_refs[2].split(":", 1)[1]
        receipt_digest = policy_refs[3].split(":", 1)[1]
        if (
            not re.fullmatch(r"APR-[A-Z0-9]{12}", approval_id)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", decision_id)
            or revision != "1"
            or not re.fullmatch(r"[0-9a-f]{64}", receipt_digest)
        ):
            raise RuntimeError("governed research decision provenance is invalid")
        refs = project.provenance_refs()
        if (
            len(refs) != 10
            or len(set(refs)) != 10
            or any(
                not isinstance(ref, str)
                or not ref
                or len(ref) > 256
                or any(ord(character) < 0x20 for character in ref)
                for ref in refs
            )
        ):
            raise RuntimeError("governed research provenance is invalid")

        # Existing exact replays already passed their boundary before insert.
        # A new row must still pass the current standing project boundary.
        if self.store.get_project(project.id) is None and self._directives is not None:
            try:
                from colony_sidecar.directives import Action
                verdict = self._directives.check(Action(
                    kind="project",
                    text=project.objective,
                    target=project.subject_person_id,
                    high_risk=True,
                ))
            except Exception as exc:
                raise RuntimeError(
                    "governed research boundary check failed closed"
                ) from exc
            if getattr(verdict, "allowed", None) is not True:
                raise RuntimeError("governed research boundary refused the project")

    def enqueue_governed_research(self, project: Project) -> Project:
        """Durably enqueue or exactly replay one governed research Project."""

        self.prepare_governed_research(project)
        stored, _created = self.store.insert_authority_bound_project(project)
        return stored

    @staticmethod
    def _authority_gaps(
        project: Project, steps: List[Step],
    ) -> Dict[int, List[str]]:
        if project.source not in _AUTHORITY_BOUND_PROJECT_SOURCES:
            return {}
        from colony_sidecar.work_orders import action_authority
        allowed = set(project.capability_allowlist)
        missing: Dict[int, List[str]] = {}
        for step in steps:
            if project.source == "cognition_spine" and step.action_kind == "deliver":
                missing[step.ordinal] = [
                    "p3_deliver_held_missing_attested_recipient_artifact_envelope"
                ]
                continue
            required = set(action_authority(step.action_kind)[1])
            excess = sorted(required - allowed)
            if excess:
                missing[step.ordinal] = excess
        return missing

    # ------------------------------------------------------------------
    # Creation / adoption / abandonment
    # ------------------------------------------------------------------

    def create_project(self, objective: str, *, title: str = "",
                       source: str = "owner",
                       entity_ids: Optional[List[str]] = None,
                       ) -> Tuple[Optional[Project], str]:
        """Boundary-gated project creation (planning only; steps gate at
        dispatch). Returns (project, reason)."""
        objective = (objective or "").strip()
        if not objective:
            return None, "objective_required"
        try:
            from colony_sidecar.cognition.goal_spine import cognition_spine_exclusive
            if cognition_spine_exclusive():
                if source == "cognition_spine":
                    return None, "typed_goal_proposal_required"
                if source not in {"owner", "directive"}:
                    return None, "legacy_autonomous_project_writer_read_only"
        except Exception:
            pass
        if self._directives is not None:
            try:
                from colony_sidecar.directives import Action
                verdict = self._directives.check(Action(
                    kind="project", text=objective, target=title or objective,
                    high_risk=True))
                if not verdict.allowed:
                    logger.warning("Project creation REFUSED by boundary: %s",
                                   verdict.reason)
                    return None, verdict.reason
            except Exception:
                logger.warning(
                    "Project creation blocked: boundary check failed closed",
                    exc_info=True,
                )
                return None, "boundary_check_failed_closed"
        title = (title or objective.split(".")[0]).strip()[:120]
        project = Project(title=title, objective=objective, source=source,
                          entity_ids=list(entity_ids or []))
        self.store.save_project(project)
        logger.info("Project created: %s %r (source=%s, mode=%s)",
                    project.id, title, source, projects_mode())
        return project, "ok"

    async def create_owner_goal_work_order(
        self,
        objective: str,
        *,
        external_event_id: str,
        external_event_digest: str,
        intake_receipt: Mapping[str, Any],
        subject_person_id: str,
        viewer_scope: str,
        shareability: str,
        occurred_at: str,
    ) -> Dict[str, Any]:
        """Create one deterministic owner Project and initial WorkOrder.

        This is the narrow durable target for a transport adapter that has
        already authenticated an owner ``Goal:`` request.  Intent recognition
        happens before this method; no model decides whether the Project
        exists.  The first bounded ``analyze`` step lets the normal canonical
        WorkOrder/Action Plane continue the goal without granting an external
        effect at ingestion time.

        Every identity and authority-bearing timestamp is derived from the
        immutable external event.  Replaying the event therefore validates and
        returns the same Project, Step, and WorkOrder instead of creating work
        twice.  The external intake receipt and journal identity are reference
        linked into the WorkOrder; external producer text never becomes an
        authority or an execution receipt by itself.
        """

        from colony_sidecar.work_orders import WorkOrderV1

        normalized = " ".join(str(objective or "").split()).strip()
        event_id = str(external_event_id or "").strip()
        event_digest = str(external_event_digest or "").strip()
        subject = str(subject_person_id or "").strip()
        scope = str(viewer_scope or "").strip()
        sharing = str(shareability or "").strip()
        if not normalized or len(normalized) > 500:
            raise ValueError("owner goal objective is outside bounds")
        if not event_id or len(event_id) > 192:
            raise ValueError("owner goal external event ID is outside bounds")
        if not re.fullmatch(r"[0-9a-f]{64}", event_digest):
            raise ValueError("owner goal external event digest is invalid")
        if not subject or not scope or sharing != "owner_private":
            raise ValueError("owner goal scope is invalid")
        try:
            issued = datetime.fromisoformat(
                str(occurred_at or "").replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError("owner goal occurrence time is invalid") from exc
        if issued.tzinfo is None:
            raise ValueError("owner goal occurrence time requires a timezone")
        issued = issued.astimezone(timezone.utc)
        issued_at = issued.timestamp()

        receipt = dict(intake_receipt or {})
        receipt_ref = str(receipt.get("receipt_ref") or "").strip()
        journal_event_id = str(receipt.get("journal_event_id") or "").strip()
        journal_seq = receipt.get("journal_seq")
        if (
            receipt.get("status") != "projected"
            or receipt.get("event_id") != event_id
            or not receipt_ref.startswith("external-event-receipt:")
            or not journal_event_id
            or isinstance(journal_seq, bool)
            or not isinstance(journal_seq, int)
            or journal_seq < 1
        ):
            raise ValueError("owner goal intake receipt is incomplete")

        identity = hashlib.sha256(
            ("owner-goal-v1\0" + event_id).encode("utf-8")
        ).hexdigest()
        project_id = f"proj-owner-goal-{identity[:20]}"
        step_id = f"step-owner-goal-{identity[:20]}"
        title = normalized.split(".", 1)[0].strip()[:120] or "Owner goal"
        goal_fingerprint = hashlib.sha256(json.dumps(
            {
                "title": title.casefold(),
                "objective": normalized.casefold(),
                "subject_person_id": subject,
                "viewer_scope": scope,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        event_ref = f"xevent:{event_id}"
        host_event_ref = f"event:{journal_event_id}"
        evidence_refs = [
            receipt_ref,
            f"xdigest:{event_digest}",
            f"journal:{journal_seq}:{journal_event_id}",
        ]
        expected_project = Project(
            id=project_id,
            title=title,
            objective=normalized,
            source="owner",
            status="active",
            outcome="pending",
            # ``xevent`` is the immutable producer identity.  The cognition
            # ThoughtJob intentionally carries only the server journal event
            # identity, so retaining both lets its duplicate gate meet this
            # Project without copying producer text into either authority.
            source_event_refs=[event_ref, host_event_ref],
            evidence_refs=evidence_refs,
            subject_person_id=subject,
            viewer_scope=scope,
            shareability=sharing,
            goal_fingerprint=goal_fingerprint,
            created_at=issued_at,
            updated_at=issued_at,
        )
        expected_step = Step(
            id=step_id,
            project_id=project_id,
            ordinal=1,
            description=(
                "Analyze and advance the owner-authored goal, returning a "
                f"receipt-backed result: {normalized}"
            ),
            action_kind="analyze",
            status="pending",
            boundary_subject=subject,
            confidence=1.0,
            work_order_issued_at=issued_at,
            created_at=issued_at,
            updated_at=issued_at,
        )

        project = self.store.get_project(project_id)
        if project is None:
            project = expected_project
        else:
            immutable = (
                "title", "objective", "source", "source_event_refs",
                "evidence_refs", "subject_person_id", "viewer_scope",
                "shareability", "goal_fingerprint",
            )
            if any(
                getattr(project, field) != getattr(expected_project, field)
                for field in immutable
            ):
                raise ValueError("deterministic owner goal Project collision")

        steps = self.store.steps_for(project_id)
        step = next((item for item in steps if item.id == step_id), None)
        if step is None:
            if steps:
                raise ValueError("deterministic owner goal Step collision")
            step = expected_step
        else:
            authority_fields = (
                "project_id", "ordinal", "description", "action_kind",
                "depends_on", "boundary_subject", "confidence",
            )
            if any(
                getattr(step, field) != getattr(expected_step, field)
                for field in authority_fields
            ):
                raise ValueError("deterministic owner goal Step collision")
            if (
                step.work_order_issued_at
                and abs(step.work_order_issued_at - issued_at) > 0.001
            ):
                raise ValueError("owner goal WorkOrder issue time drifted")
            if not step.work_order_issued_at:
                step.work_order_issued_at = issued_at

        refs = tuple(project.provenance_refs())
        order = WorkOrderV1.for_project_step(
            project, step, context_refs=refs, now=issued,
        )
        existing_order = self.store.get_work_order(order.work_order_id)
        if existing_order is not None and (
            existing_order["work_order_digest"] != order.work_order_digest
            or existing_order["payload"] != order.payload()
        ):
            raise ValueError("deterministic owner goal WorkOrder collision")

        terminal = project.status in {"completed", "abandoned"} or (
            step.status in {"done", "failed", "skipped"}
        )
        if terminal:
            if existing_order is None:
                raise ValueError("terminal owner goal is missing its WorkOrder")
            return {
                "schema": "OwnerGoalPromotionReceiptV1",
                "status": "existing_terminal",
                "external_event_id": event_id,
                "external_receipt_ref": receipt_ref,
                "project_id": project_id,
                "step_id": step_id,
                "work_order_ref": f"work-order:{order.work_order_id}",
                "work_order_id": order.work_order_id,
                "work_order_digest": order.work_order_digest,
            }

        blocked_reason = ""
        if project.status == "blocked":
            blocked_reason = project.reason or "owner_goal_blocked"
        elif self._directives is not None:
            try:
                from colony_sidecar.directives import Action
                for action in (
                    Action(
                        kind="project",
                        text=_boundary_intent_text(normalized, title),
                        target="",
                        high_risk=True,
                    ),
                    _step_boundary_action(project, step),
                ):
                    verdict = self._directives.check(action)
                    if not verdict.allowed:
                        blocked_reason = str(
                            verdict.reason or "owner_goal_boundary_refused"
                        )[:500]
                        break
            except Exception:
                blocked_reason = "boundary_check_failed_closed"

        if blocked_reason:
            if self.store.get_project(project_id) is None:
                project.status = "blocked"
                project.reason = blocked_reason
            self.store.prepare_work_order(project, step, order)
            return {
                "schema": "OwnerGoalPromotionReceiptV1",
                "status": "blocked",
                "reason": blocked_reason,
                "external_event_id": event_id,
                "external_receipt_ref": receipt_ref,
                "project_id": project_id,
                "step_id": step_id,
                "work_order_ref": f"work-order:{order.work_order_id}",
                "work_order_id": order.work_order_id,
                "work_order_digest": order.work_order_digest,
            }

        if not self._has_canonical_work_order_adapter():
            raise RuntimeError("canonical WorkOrder adapter is unavailable")
        ok, result = await self._work_orders.execute(
            project, step, context_refs=refs,
        )
        if ok is False:
            raise RuntimeError(result or "owner goal WorkOrder issue failed")
        persisted = self.store.get_work_order(order.work_order_id)
        if persisted is None:
            raise RuntimeError("owner goal WorkOrder did not persist")
        return {
            "schema": "OwnerGoalPromotionReceiptV1",
            "status": "issued",
            "queue_state": str(result or "").rsplit(":", 1)[-1],
            "external_event_id": event_id,
            "external_receipt_ref": receipt_ref,
            "project_id": project_id,
            "step_id": step_id,
            "work_order_ref": persisted["work_order_ref"],
            "work_order_id": order.work_order_id,
            "work_order_digest": order.work_order_digest,
        }

    def abandon(self, project_id: str, reason: str = "owner_request",
                ) -> Optional[Project]:
        project = self.store.get_project(project_id)
        if project is None or project.status in ("completed", "abandoned"):
            return project
        project.status = "abandoned"
        project.outcome = "failed"
        project.reason = reason
        self.store.save_project(project)
        self._record_outcome("failure")
        if self._feedback is not None:
            try:
                self._feedback.record("project", "dismissed")
            except Exception:
                pass
        logger.info("Project %s abandoned: %s", project_id, reason)
        return project

    def project_status(self, project_id: str) -> Optional[Dict[str, Any]]:
        project = self.store.get_project(project_id)
        if project is None:
            return None
        steps = self.store.steps_for(project_id)
        return {
            "project": project.to_row(),
            "steps": [s.to_row() for s in steps],
            "done": sum(1 for s in steps if s.status == "done"),
            "total": len(steps),
        }

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def _effective_mode(self) -> str:
        """Env mode, graduated by the trust engine (Amendment 1.2).

        "off" and "live" are owner overrides. "shadow" is the calibration
        stage: once the "project" trust domain graduates (clean calibration
        runs), pursuit goes live THROUGH the sub-gates, which carry their own
        ask/approval semantics for anything outbound or mutating.
        """
        mode = projects_mode()
        if mode in ("off", "live"):
            return mode
        trust = getattr(self._self_model, "trust", None)
        if trust is None:
            return mode
        try:
            stage = trust.stage("project", default="shadow")
        except Exception:
            return mode
        return "shadow" if stage == "shadow" else "live"

    async def tick(self) -> Dict[str, Any]:
        mode = self._effective_mode()
        report: Dict[str, Any] = {"mode": mode, "adopted": 0, "planned": 0,
                                  "steps_dispatched": 0, "deferred": False}
        if mode == "off":
            return report
        report["held"] = 0

        # Pursue-vs-defer via the self-model: under heavy load, hold off.
        if self._self_model is not None:
            try:
                load = self._self_model.load()
                if int(load.get("total") or 0) >= _int_env(
                        "COLONY_PROJECTS_DEFER_LOAD", 10):
                    logger.info("Project pursuit deferred (load=%s)", load)
                    report["deferred"] = True
                    return report
            except Exception:
                pass

        try:
            report["adopted"] = await self._adopt_initiatives()
        except Exception:
            logger.debug("project adoption failed", exc_info=True)
        try:
            held_ids: Set[str] = set()
            report["planned"] = await self._plan_pending(mode, held_ids)
        except Exception:
            logger.debug("project planning failed", exc_info=True)
        try:
            report["steps_dispatched"] = await self._pursue_active(
                mode, held_ids,
            )
        except Exception:
            logger.debug("project pursuit failed", exc_info=True)
        report["held"] = len(held_ids)
        return report

    async def reconcile_terminal_results(
        self, *, limit: int = 25,
    ) -> Dict[str, Any]:
        """Reconcile durable queue outcomes without planning or pursuit.

        This is intentionally separate from ``tick``: result projection is
        bookkeeping for work that already ran, so it must not wait behind an
        LLM planning pass, self-model deferral, review cadence, or a Project
        boundary that changed after dispatch.
        """

        reconcile = getattr(
            self._work_orders, "reconcile_terminal_results", None,
        )
        if not callable(reconcile):
            return {
                "checked": 0,
                "terminal": 0,
                "projected": 0,
                "errors": 0,
            }
        return await reconcile(limit=limit)

    # ------------------------------------------------------------------
    # Adoption: project-type initiatives become durable projects
    # ------------------------------------------------------------------

    async def _adopt_initiatives(self) -> int:
        try:
            from colony_sidecar.cognition.goal_spine import cognition_spine_exclusive
            if cognition_spine_exclusive():
                return 0
        except Exception:
            pass
        if self._initiatives is None:
            return 0
        open_projects = self.open_capacity_used()
        if open_projects >= _int_env("COLONY_PROJECTS_MAX_CONCURRENT", 3):
            return 0
        try:
            loop = asyncio.get_event_loop()
            pending = await loop.run_in_executor(
                None, lambda: self._initiatives.list(
                    status=["pending"], type="project", limit=5))
        except Exception:
            return 0
        existing_titles = {p.title.strip().lower()
                           for p in self.store.list_projects(limit=200)}
        for init in pending or []:
            desc = (getattr(init, "description", "") or "").strip()
            if not desc or desc.split(".")[0].strip()[:120].lower() in existing_titles:
                continue
            rationale = (getattr(init, "rationale", "") or "").strip()
            objective = desc if not rationale else f"{desc}\n\nWhy: {rationale}"
            project, reason = self.create_project(
                objective, title=desc.split(".")[0][:120], source="thinker")
            if project is None:
                continue
            try:
                iid = getattr(init, "id", "")
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self._initiatives.complete(
                        iid, agent_id="project-engine",
                        result=f"adopted as project {project.id}"))
            except Exception:
                logger.debug("initiative adoption closure failed", exc_info=True)
            logger.info("Adopted initiative as project %s: %r",
                        project.id, project.title)
            return 1
        return 0

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    async def _plan_pending(
        self, mode: str, held_ids: Optional[Set[str]] = None,
    ) -> int:
        from colony_sidecar.projects.planner import plan_project
        planned = 0
        now = time.time()
        for project in self._eligible_projects(
            status="planning", limit=10, held_ids=held_ids,
        ):
            if project.next_review_at and project.next_review_at > now:
                continue
            if project.source == "governed_action":
                hold_reason = self._governed_hold_reason(mode)
                if hold_reason:
                    project.reason = hold_reason
                    project.next_review_at = now + projects_review_secs()
                    self.store.save_project(project)
                    continue
                if project.reason in _GOVERNED_HOLD_REASONS:
                    project.reason = ""
            skills_block = self._skills_block(project.objective)
            brief = self._self_brief()
            planning_context = ""
            if project.source == "cognition_spine":
                planning_context = (
                    "This autonomous goal is bound to these exact capabilities: "
                    + ", ".join(project.capability_allowlist)
                    + ". Do not create a deliver step: P3 delivery is held until "
                    "WorkOrder carries a transport-attested recipient and a "
                    "bounded message/artifact reference in its authority digest. "
                    "End with an internal receipt-backed artifact instead."
                )
            elif project.source == "governed_action":
                planning_context = (
                    "This owner-governed research goal is bound to these exact "
                    "read-only capabilities: "
                    + ", ".join(project.capability_allowlist)
                    + ". Do not create directed or deliver steps, or any step "
                    "requiring capabilities outside this list. End with a "
                    "receipt-backed internal artifact."
                )
            steps = await plan_project(
                self._router, project.objective, project_id=project.id,
                context=planning_context,
                skills_block=skills_block, self_brief=brief,
                boundaries=self._boundaries_brief())
            if self._apply_project_hold(project):
                if held_ids is not None:
                    held_ids.add(project.id)
                continue
            if not steps:
                project.replans += 1
                if project.replans > projects_max_replans():
                    project.status = "abandoned"
                    project.outcome = "failed"
                    project.reason = "planning_failed"
                    self.store.save_project(project)
                    self._record_outcome("failure")
                    logger.warning("Project %s abandoned: planning failed %d times",
                                   project.id, project.replans)
                else:
                    project.next_review_at = now + projects_review_secs()
                    self.store.save_project(project)
                continue
            if project.source in _AUTHORITY_BOUND_PROJECT_SOURCES:
                try:
                    missing = self._authority_gaps(project, steps)
                except Exception as exc:
                    project.status = "blocked"
                    project.reason = f"capability_validation_failed:{exc}"
                    self.store.save_project(project)
                    continue
                if missing:
                    project.status = "blocked"
                    project.reason = "goal_authority_missing:" + json.dumps(
                        missing, sort_keys=True, separators=(",", ":"),
                    )
                    self.store.save_project(project)
                    continue
            for s in steps:
                self.store.save_step(s)
            project.status = "active"
            project.next_review_at = 0.0
            self.store.save_project(project)
            planned += 1
            logger.info(
                "Project %s planned[%s]: %d step(s): %s",
                project.id, mode, len(steps),
                " | ".join(f"{s.ordinal}.{s.action_kind}:{s.description[:60]}"
                           for s in steps))
        return planned

    # ------------------------------------------------------------------
    # Pursuit
    # ------------------------------------------------------------------

    async def _pursue_active(
        self, mode: str, held_ids: Optional[Set[str]] = None,
    ) -> int:
        dispatched = 0
        for project in self._eligible_projects(
            status="active", limit=3, held_ids=held_ids, due=True,
        ):
            try:
                advanced = await self._advance_project(
                    project, mode, held_ids=held_ids,
                )
                dispatched += 1 if advanced else 0
            except Exception:
                logger.error("project %s advance failed", project.id,
                             exc_info=True)
        return dispatched

    async def _advance_project(
        self, project: Project, mode: str,
        held_ids: Optional[Set[str]] = None,
    ) -> bool:
        if self._apply_project_hold(project):
            if held_ids is not None:
                held_ids.add(project.id)
            return False
        if project.source == "governed_action":
            hold_reason = self._governed_hold_reason(mode)
            if hold_reason:
                project.reason = hold_reason
                project.next_review_at = time.time() + projects_review_secs()
                self.store.save_project(project)
                return False
            if project.reason in _GOVERNED_HOLD_REASONS:
                project.reason = ""
        steps = self.store.steps_for(project.id)
        if not steps:
            # active project with no steps: send back to planning
            project.status = "planning"
            self.store.save_project(project)
            return False

        # Terminal check: everything done/skipped -> complete.
        if all(s.status in ("done", "skipped") for s in steps):
            await self._complete_project(project, steps, mode)
            return False

        # A failed step triggers a bounded replan of the remaining work.
        if any(s.status == "failed" for s in steps):
            await self._replan_remaining(project, steps, mode)
            return False

        done_ordinals = {s.ordinal for s in steps if s.status in ("done", "skipped")}
        ready = [s for s in steps if s.status == "pending"
                 and all(d in done_ordinals for d in s.depends_on)]
        if not ready:
            # nothing ready (steps active or deps unmet) -- check again later
            project.next_review_at = time.time() + projects_review_secs()
            self.store.save_project(project)
            return False
        step = ready[0]

        # A cognition-spine project is bound to the GoalProposal authority.
        # Recheck at dispatch so a hand-edited or stale plan cannot widen it.
        if project.source in _AUTHORITY_BOUND_PROJECT_SOURCES:
            try:
                gaps = self._authority_gaps(project, [step])
                missing = gaps.get(step.ordinal, [])
            except Exception as exc:
                missing = [f"validation_error:{exc}"]
            if missing:
                project.status = "blocked"
                project.reason = "goal_authority_missing:" + ",".join(missing)
                self.store.save_project(project)
                return False

        # Boundary gate on the concrete step.
        if self._directives is not None:
            try:
                verdict = self._directives.check(
                    _step_boundary_action(project, step)
                )
                if not verdict.allowed:
                    project.status = "blocked"
                    project.reason = verdict.reason
                    self.store.save_project(project)
                    logger.warning(
                        "Project %s BLOCKED at step %d by boundary: %s",
                        project.id, step.ordinal, verdict.reason)
                    await self._milestone(
                        project, "blocked",
                        f"Step {step.ordinal} ({step.description[:120]}) hit a "
                        f"standing boundary: {verdict.reason}", mode)
                    return False
            except Exception:
                project.status = "blocked"
                project.reason = "boundary_check_failed_closed"
                self.store.save_project(project)
                logger.warning(
                    "Project %s BLOCKED at step %d: boundary check failed closed",
                    project.id,
                    step.ordinal,
                    exc_info=True,
                )
                return False

        # A boundary check may itself observe or trigger a concurrent mode
        # transition.  Re-evaluate before either shadow bookkeeping or a live
        # dispatch so the ready step remains pending and exactly resumable.
        if self._apply_project_hold(project):
            if held_ids is not None:
                held_ids.add(project.id)
            return False

        if mode == "shadow":
            logger.info(
                "SHADOW-PROJECT %s step %d/%d [%s]: would dispatch %r "
                "(boundary=allowed)",
                project.id, step.ordinal, len(steps), step.action_kind,
                step.description[:160])
            step.status = "skipped"
            step.result = "SKIPPED: SHADOW simulation (no action taken)"
            self.store.save_step(step)
            project.next_review_at = time.time() + projects_review_secs()
            self.store.save_project(project)
            # re-check terminal state so a finished shadow run completes
            steps = self.store.steps_for(project.id)
            if all(s.status in ("done", "skipped") for s in steps):
                await self._complete_project(project, steps, mode)
            return True

        # LIVE dispatch through the kind's own gated sub-path.
        if self._apply_project_hold(project):
            if held_ids is not None:
                held_ids.add(project.id)
            return False
        step.status = "active"
        step.attempts += 1
        self.store.save_step(step)
        started = time.monotonic()
        ok, result = await self._dispatch_step(project, step)
        latency = time.monotonic() - started

        if ok is None:
            # waiting on an external gate (e.g. directed approval): not an
            # attempt, re-check at next review.
            step.status = "pending"
            step.attempts = max(0, step.attempts - 1)
            step.result = result[:1000]
            self.store.save_step(step)
        elif ok:
            skipped = result.startswith("SKIPPED:")
            step.status = "skipped" if skipped else "done"
            step.result = result[:2000]
            self.store.save_step(step)
            if not skipped:
                receipt = (
                    self.store.get_execution_result(step.result_ref)
                    if step.result_ref else None
                )
                if (
                    receipt
                    and receipt.get("terminal_outcome") == "succeeded"
                    and receipt.get("verification_result") == "verified"
                ):
                    self._record_outcome(
                        "success", latency,
                        stated_confidence=step.confidence,
                    )
        else:
            self._record_outcome(
                "timeout" if "timeout" in (result or "").lower() else "failure",
                latency, stated_confidence=step.confidence)
            if step.attempts >= 2:
                step.status = "failed"
                step.result = result[:1000]
                self.store.save_step(step)
                if self._skills is not None:
                    try:
                        self._skills.record_failure_note(
                            "project",
                            f"step '{step.description[:80]}' failed: {result[:120]}")
                    except Exception:
                        pass
            else:
                step.status = "pending"
                step.result = f"attempt {step.attempts} failed: {result[:500]}"
                self.store.save_step(step)

        project.next_review_at = time.time() + projects_review_secs()
        self.store.save_project(project)

        steps = self.store.steps_for(project.id)
        if all(s.status in ("done", "skipped") for s in steps):
            await self._complete_project(project, steps, mode)
        return True

    # ------------------------------------------------------------------
    # Step dispatch by kind (LIVE mode only)
    # ------------------------------------------------------------------

    async def _dispatch_step(self, project: Project, step: Step,
                             ) -> Tuple[Optional[bool], str]:
        if (
            project.source == "governed_action"
            and not self._has_canonical_work_order_adapter()
        ):
            return None, (
                "governed_action_requires_canonical_work_order_adapter"
            )
        if self._work_orders is not None:
            completed_refs = tuple(project.provenance_refs()) + tuple(
                "project-step:%s:%s" % (project.id, item.id)
                for item in self.store.steps_for(project.id)
                if item.status == "done"
            )
            return await self._work_orders.execute(
                project, step, context_refs=completed_refs
            )
        kind = step.action_kind
        try:
            if kind in ("analyze", "research", "internal"):
                return await self._run_reasoning_step(project, step)
            if kind == "directed":
                return await self._run_directed_step(project, step)
            if kind == "deliver":
                return await self._run_deliver_step(project, step)
        except Exception as exc:
            return False, f"dispatch error: {exc}"
        return False, f"unknown action_kind {kind}"

    async def _run_reasoning_step(self, project: Project, step: Step,
                                  ) -> Tuple[Optional[bool], str]:
        if self._reasoning is None:
            return False, "no reasoning loop wired"
        done = [s for s in self.store.steps_for(project.id)
                if s.status == "done" and s.result]
        context = "\n".join(
            f"- step {s.ordinal}: {s.result[:200]}" for s in done[-5:])
        prompt = (f"## Project: {project.title}\n"
                  f"Objective: {project.objective[:800]}\n\n"
                  + (f"## Completed so far\n{context}\n\n" if context else "")
                  + f"## Current step ({step.action_kind})\n{step.description}")
        try:
            from colony_sidecar.cognition.charter import build_system_prompt
            system = build_system_prompt(
                "executor",
                self_brief=self._self_brief() or None,
                boundaries=self._boundaries_brief() or None,
                skills=self._skills_block(step.description) or None,
                extra=_STEP_SCOPE_CONTEXT)
        except Exception:
            logger.debug("charter compose failed; minimal fallback",
                         exc_info=True)
            system = _STEP_SCOPE_CONTEXT
            db = self._boundaries_brief()
            if db:
                system += ("\n## Standing boundaries from the owner "
                           "(obey without exception)\n" + db)

        working: List[Dict[str, Any]] = [{"role": "user", "content": prompt}]
        tier = os.environ.get("COLONY_PROJECTS_MODEL_TIER", "small")
        for _round in range(_MAX_TOOL_ROUNDS):
            result = await self._reasoning.run_turn(
                session_id=f"project-{project.id}-s{step.ordinal}",
                messages=working, system_prompt=system, model_override=tier)
            if result.status == "completed":
                text = (result.message or {}).get("content", "") or ""
                return True, text
            if result.status == "error":
                return False, result.error or "reasoning error"
            if result.status == "needs_tool":
                pending = list(result.tool_calls or [])
                if not pending or self._tools is None:
                    return False, "needs_tool with no executable tool path"
                pending = self._boundary_filter_tools(pending)
                if not pending:
                    working.append({
                        "role": "user",
                        "content": "Those actions violate a standing boundary "
                                   "and were refused. Summarise what you can "
                                   "and stop."})
                    continue
                results = await self._tools.execute_batch(
                    pending, session_id=f"project-{project.id}-s{step.ordinal}")
                working.append(_assistant_msg(
                    (result.message or {}).get("content", ""), pending))
                for tr in results:
                    working.append({"role": "tool",
                                    "tool_call_id": tr.get("tool_call_id", ""),
                                    "content": tr.get("content", "")})
                continue
            return False, f"unexpected reasoning status {result.status}"
        return False, f"tool-loop cap reached ({_MAX_TOOL_ROUNDS} rounds)"

    def _boundary_filter_tools(self, pending: List[dict]) -> List[dict]:
        if self._directives is None:
            return pending
        try:
            from colony_sidecar.directives import Action
        except Exception:
            logger.warning(
                "Project tool batch blocked: boundary action contract unavailable",
                exc_info=True,
            )
            return []
        survivors = []
        for tc in pending:
            name = tc.get("name", "")
            args = tc.get("arguments", {}) if isinstance(
                tc.get("arguments"), dict) else {}
            try:
                verdict = self._directives.check(Action(
                    kind="execute_tool", tool_name=name, args=args, text=name,
                    high_risk=True))
            except Exception:
                logger.warning(
                    "Project tool call %s REFUSED: boundary check failed closed",
                    name,
                    exc_info=True,
                )
                continue
            if getattr(verdict, "allowed", None) is not True:
                logger.warning("Project tool call %s REFUSED by boundary: %s",
                               name, getattr(verdict, "reason", "invalid_verdict"))
                continue
            survivors.append(tc)
        return survivors

    async def _run_directed_step(self, project: Project, step: Step,
                                 ) -> Tuple[Optional[bool], str]:
        if self._directed is None:
            return False, "no directed service wired"
        # An existing awaiting-approval task for this step: keep waiting.
        prior = (step.result or "")
        if prior.startswith("awaiting_approval:"):
            task_id = prior.split(":", 1)[1].strip()
            task = self._directed.store.get(task_id)
            if task is not None:
                if task.status == "awaiting_approval":
                    return None, prior
                if task.status in ("approved",):
                    out = await self._directed.dispatch(task.id)
                    return True, f"directed task {task.id} dispatched: {json.dumps(out, default=str)[:300]}"
                if task.status in ("dispatched", "dispatched_dry", "completed"):
                    return True, f"directed task {task.id} {task.status}"
                if task.status in ("refused", "violated", "failed", "expired"):
                    return False, f"directed task {task.id} {task.status}: {task.refusal_reason}"
        task = await self._directed.intake(step.description)
        if task.status == "refused":
            return False, f"directed intake refused: {task.refusal_reason}"
        if task.status == "awaiting_approval":
            return None, f"awaiting_approval:{task.id}"
        out = await self._directed.dispatch(task.id)
        status = "dry_run" if out.get("dry_run") else (
            "dispatched" if out.get("dispatched") else
            f"not dispatched ({out.get('reason', '?')})")
        ok = bool(out.get("dispatched") or out.get("dry_run"))
        return (True, f"directed task {task.id}: {status}") if ok else (
            False, f"directed task {task.id}: {status}")

    async def _run_deliver_step(self, project: Project, step: Step,
                                ) -> Tuple[Optional[bool], str]:
        done = [s for s in self.store.steps_for(project.id)
                if s.status == "done" and s.result
                and not s.result.startswith("SHADOW")]
        finding = "\n".join(
            f"- {s.result[:300]}" for s in done[-4:]) or project.objective[:300]
        try:
            from colony_sidecar.proposals import Proposal, proposal_to_payload
            prop = Proposal(
                title=f"Project update: {project.title[:70]}",
                finding=f"{step.description[:200]}\n{finding}"[:1200],
                why_it_helps=f"progress on your project: {project.title[:100]}",
                suggested_action="Tell me to continue, adjust, or stop this project.",
                source=project.id, initiative_type="proposal", confidence=0.7)
            if self._proposals is not None:
                self._proposals.add(prop)
            if self._deliver is not None:
                ok = bool(await self._deliver(proposal_to_payload(prop)))
                return True, ("delivered" if ok else
                              "routed to guarded delivery (held/gated)")
            return True, "proposal stored (no delivery router)"
        except Exception as exc:
            return False, f"deliver failed: {exc}"

    # ------------------------------------------------------------------
    # Replan / complete / milestones
    # ------------------------------------------------------------------

    async def _replan_remaining(self, project: Project, steps: List[Step],
                                mode: str) -> None:
        from colony_sidecar.projects.planner import plan_project
        next_replans = project.replans + 1
        if next_replans > projects_max_replans():
            project.replans = next_replans
            project.status = "abandoned"
            project.reason = "replan_limit"
            self.store.save_project(project)
            self._record_outcome("failure")
            if self._feedback is not None:
                try:
                    self._feedback.record("project", "dismissed")
                except Exception:
                    pass
            await self._milestone(
                project, "abandoned",
                f"replan limit reached after {project.replans - 1} replans", mode)
            return

        done = [s for s in steps if s.status == "done"]
        failed = [s for s in steps if s.status == "failed"]
        context = ""
        if done:
            context += "Completed steps:\n" + "\n".join(
                f"- {s.description[:120]}: {s.result[:150]}" for s in done[-5:])
        if failed:
            context += "\nFailed steps (plan around these failures):\n" + "\n".join(
                f"- {s.description[:120]}: {s.result[:150]}" for s in failed[-3:])

        new_steps = await plan_project(
            self._router,
            f"{project.objective}\n\nRe-plan ONLY the remaining work.",
            project_id=project.id, context=context,
            skills_block=self._skills_block(project.objective),
            self_brief=self._self_brief(),
            boundaries=self._boundaries_brief())
        if self._apply_project_hold(project):
            return
        project.replans = next_replans
        if project.source in _AUTHORITY_BOUND_PROJECT_SOURCES:
            try:
                missing = self._authority_gaps(project, new_steps)
            except Exception as exc:
                project.status = "blocked"
                project.reason = f"capability_validation_failed:{exc}"
                self.store.save_project(project)
                return
            if missing:
                project.status = "blocked"
                project.reason = "goal_authority_missing:" + json.dumps(
                    missing, sort_keys=True, separators=(",", ":"),
                )
                self.store.save_project(project)
                return
        self.store.delete_steps(project.id, statuses=["pending", "failed", "active"])
        base = max((s.ordinal for s in done), default=0)
        for s in new_steps:
            s.ordinal += base
            s.depends_on = [d + base for d in s.depends_on]
            self.store.save_step(s)
        project.next_review_at = time.time() + projects_review_secs()
        if not new_steps and not done:
            project.status = "abandoned"
            project.reason = "replan_produced_no_steps"
            self._record_outcome("failure")
        elif not new_steps:
            # nothing left to do beyond what completed
            self.store.save_project(project)
            await self._complete_project(
                project, self.store.steps_for(project.id), mode)
            return
        self.store.save_project(project)
        logger.info("Project %s replanned (replan %d/%d): %d new step(s)",
                    project.id, project.replans, projects_max_replans(),
                    len(new_steps))

    async def _complete_project(self, project: Project, steps: List[Step],
                                mode: str) -> None:
        if project.status == "completed":
            return
        project.status = "completed"
        done = [step for step in steps if step.status == "done"]
        skipped = [step for step in steps if step.status == "skipped"]
        all_steps_done = bool(steps) and len(done) == len(steps)
        receipts_verified = all_steps_done
        if receipts_verified:
            for step in done:
                result = (
                    self.store.get_execution_result(step.result_ref)
                    if step.result_ref else None
                )
                payload = result.get("payload", {}) if result else {}
                if (
                    not result
                    or result.get("terminal_outcome") != "succeeded"
                    or result.get("verification_result") != "verified"
                    or not payload.get("receipt_refs")
                ):
                    receipts_verified = False
                    break
        if all_steps_done:
            project.reason = "all_steps_done"
        elif done:
            project.reason = "completed_with_skips"
        else:
            project.reason = "all_steps_skipped"
        if mode == "shadow" or not all_steps_done:
            project.outcome = "neutral"
        elif receipts_verified:
            project.outcome = "succeeded"
        else:
            project.outcome = "unverified"
        self.store.save_project(project)
        # A skip means no outcome was observed.  It is neither success nor
        # failure and must never graduate competence/trust.  In particular,
        # shadow simulation is audit evidence, not a successful action.
        if project.outcome == "succeeded":
            self._record_outcome("success", shadow=(mode == "shadow"))
        if project.outcome == "succeeded" and self._feedback is not None:
            try:
                self._feedback.record("project", "actioned")
            except Exception:
                pass
        summary = "; ".join(
            s.result[:100] for s in steps if s.status == "done" and s.result)[:800]
        event = (
            "completed" if project.outcome == "succeeded"
            else "unverified" if all_steps_done and mode != "shadow"
            else "skipped"
        )
        neutral_summary = (
            f"{len(done)} completed step(s); {len(skipped)} skipped; "
            "no receipt-backed overall success recorded"
        )
        await self._milestone(
            project, event,
            summary if project.outcome == "succeeded" and summary else neutral_summary,
            mode,
        )
        # Skill hook (item 3): non-trivial completion -> distill a procedure.
        if project.outcome == "succeeded" and len(steps) >= 3 and mode == "live":
            try:
                from colony_sidecar.skills_memory import distill_from_completion
                transcript = "\n".join(
                    f"step {s.ordinal} ({s.action_kind}): {s.description[:150]} "
                    f"-> {s.result[:200]}" for s in steps)
                await distill_from_completion(
                    self._router, self._skills, domain="project",
                    task_text=project.objective, result_text=transcript,
                    source_ref=project.id)
            except Exception:
                logger.debug("project skill distillation failed", exc_info=True)
        logger.info(
            "Project %s %s[%s]: %s (done=%d skipped=%d)",
            project.id,
            "COMPLETED" if project.outcome == "succeeded" else "NEUTRAL",
            mode,
            project.title,
            len(done),
            len(skipped),
        )

    async def _milestone(self, project: Project, event: str, detail: str,
                         mode: str) -> None:
        """Status-change report as a Proposal. Shadow: stored + logged only."""
        try:
            from colony_sidecar.proposals import Proposal, proposal_to_payload
            prop = Proposal(
                title=f"Project {event}: {project.title[:60]}",
                finding=detail[:1000],
                why_it_helps=("keeps you in control of work I am pursuing "
                              "on your behalf"),
                suggested_action=(
                    "Review and tell me whether to proceed differently"
                    if event in ("blocked", "abandoned")
                    else "Review the outcome; tell me any follow-ups"),
                source=project.id, initiative_type="proposal",
                confidence=0.85 if event == "blocked" else 0.7)
            if self._proposals is not None:
                self._proposals.add(prop)
            if mode == "shadow" or self._deliver is None:
                logger.info(
                    "SHADOW-PROJECT-MILESTONE %s [%s]: %s -- %s",
                    project.id, event, prop.title, detail[:200])
                return
            await self._deliver(proposal_to_payload(prop))
        except Exception:
            logger.debug("project milestone failed", exc_info=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _skills_block(self, situation: str) -> str:
        if self._skills is None:
            return ""
        try:
            from colony_sidecar.skills_memory import (
                format_block, relevant_skills, skills_enabled,
            )
            if not skills_enabled():
                return ""
            return format_block(
                relevant_skills(self._skills, situation, k=3, domain="project"),
                strategy_note=self._skills.get_note("project"))
        except Exception:
            return ""

    def _boundaries_brief(self) -> str:
        if self._directives is None:
            return ""
        try:
            return self._directives.context_brief() or ""
        except Exception:
            return ""

    def _self_brief(self) -> str:
        if self._self_model is None:
            return ""
        try:
            return self._self_model.brief()
        except Exception:
            return ""

    def _record_outcome(self, outcome: str,
                        latency: Optional[float] = None,
                        shadow: bool = False,
                        stated_confidence: Optional[float] = None) -> None:
        if self._self_model is None:
            return
        try:
            from colony_sidecar.cognition.evidence_pipeline import (
                cognition_evidence_mode,
            )
            if cognition_evidence_mode() in {"shadow", "live"}:
                # The receipt-bound reducer is the sole project-competence
                # writer in live mode, while shadow must remain observation-
                # only. Direct in-process status is not durable evidence.
                return
        except Exception:
            # Import/configuration failure must not manufacture a new direct
            # success while the evidence path is requested live.
            if os.environ.get(
                "COLONY_COGNITION_EVIDENCE", "off"
            ).strip().lower() in {"shadow", "live"}:
                return
        try:
            self._self_model.record("project", outcome, latency_secs=latency,
                                    shadow=shadow,
                                    stated_confidence=stated_confidence)
        except Exception:
            pass


def _assistant_msg(content: str, tool_calls: List[dict]) -> Dict[str, Any]:
    """OpenAI-shaped assistant message carrying tool calls (mirrors the
    initiative executor's continuation shape)."""
    return {
        "role": "assistant",
        "content": content or None,
        "tool_calls": [
            {"id": tc.get("id", ""), "type": "function",
             "function": {"name": tc.get("name", ""),
                          "arguments": (tc["arguments"]
                                        if isinstance(tc.get("arguments"), str)
                                        else json.dumps(tc.get("arguments", {})))}}
            for tc in tool_calls
        ],
    }
