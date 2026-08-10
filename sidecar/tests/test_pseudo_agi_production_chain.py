"""One production-path proof from owner text evidence to settled cognition."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from colony_sidecar.api.authority import RequestAuthority
from colony_sidecar.cognition.drive_governance import (
    CharterRevisionV1,
    DriveGovernance,
    DriveGovernanceStore,
    DriveV1,
    RankingBudgetV1,
    ScopeV1,
)
from colony_sidecar.cognition.evidence_pipeline import (
    CognitionEvidenceReducer,
    CognitionEvidenceStore,
)
from colony_sidecar.cognition.external_events import (
    ExternalCognitionEventV1,
    ExternalEventInboxStore,
    ExternalEventIntake,
)
from colony_sidecar.cognition.goal_spine import (
    CognitionSpine,
    CognitionSpineStore,
    ThoughtQueueAdapter,
)
from colony_sidecar.cognition.runtime import CognitionRuntimeContractV1
from colony_sidecar.initiatives.approval_authority import ApprovalAuthorityStore
from colony_sidecar.projects import ProjectEngine, ProjectStore
from colony_sidecar.projects.event_outbox import ProjectEventProjector
from colony_sidecar.self_model.event_concerns import ExternalEventConcernReducer
from colony_sidecar.self_model.store import CompetenceStore, SelfModel
from colony_sidecar.self_model.workspace import ConcernStore
from colony_sidecar.server import _compose_p7_charter_admission
from colony_sidecar.task_queue.models import JobResult, JobStatus, JobType
from colony_sidecar.work_orders import QueueWorkOrderAdapter


NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
OWNER_SCOPE = ScopeV1("person-owner", "owner", "owner_private")


class ProductionQueue:
    def __init__(self):
        self.jobs = {}

    async def get_job(self, job_id):
        return self.jobs.get(job_id)

    async def post(self, job):
        if job.job_id in self.jobs:
            raise AssertionError("production queue received a duplicate post")
        self.jobs[job.job_id] = job
        return job.job_id

    async def attest_job_success(
        self, job_id, *, report, verifier_identity, verifier_type,
    ):
        result = report.get("execution_result") or {}
        if (
            report.get("status") != "verified"
            or result.get("terminal_outcome") != "succeeded"
            or result.get("verification_result") != "verified"
            or result.get("verifier_identity") != verifier_identity
            or verifier_type != "artifact_resolver"
        ):
            return False
        self.jobs[job_id].tags["success_attested"] = "true"
        return True


class ProductionManager:
    def __init__(self):
        self.queue = ProductionQueue()


class AllowBoundaries:
    def check(self, _action):
        return SimpleNamespace(allowed=True, reason="owner boundaries allow")

    def context_brief(self):
        return "Read-only evidence gathering only; no outbound delivery."


class PlannerRouter:
    async def complete(self, _messages, *, context):
        assert context == {"task": "project_planning"}
        return SimpleNamespace(content=json.dumps([{
            "ordinal": 1,
            "description": (
                "Research the owner observation and produce a verified "
                "read-only evidence artifact"
            ),
            "action_kind": "research",
            "depends_on": [],
            "confidence": 0.82,
        }]))


class ArtifactVerifier:
    verifier_type = "artifact_resolver"

    def verify(self, *, result, **_kwargs):
        return {
            "verified": bool(result.receipt_refs),
            "receipt_refs": list(result.receipt_refs),
            "verifier_identity": "full-chain-artifact-resolver:v1",
        }


def _owner_ingest_authority():
    return RequestAuthority(
        principal_id="owner-text-cognition-publisher",
        credential_id="owner-text-key-1",
        scopes=frozenset({"cognition:events-ingest"}),
        viewer_person_id="person-owner",
        person_ids=frozenset({"person-owner"}),
        audiences=frozenset({"owner"}),
        authenticated=True,
    )


def _charter_authority():
    return RequestAuthority(
        principal_id="owner-charter-approval-service",
        credential_id="owner-charter-key-1",
        scopes=frozenset({"charter:approval-decide"}),
        viewer_person_id="person-owner",
        person_ids=frozenset({"person-owner"}),
        audiences=frozenset({"owner"}),
        authenticated=True,
    )


def _activate_owner_charter(tmp_path):
    approvals = ApprovalAuthorityStore(tmp_path / "approval-authority.db")
    store = DriveGovernanceStore(tmp_path / "drive-governance.db")
    governance = DriveGovernance(store, approvals, mode="live")
    drive = DriveV1.create(
        key="verified_owner_outcomes",
        version="v1",
        title="Verified owner outcomes",
        definition_summary="Prefer bounded, receipt-backed owner outcomes",
        max_abs_contribution=0.8,
        max_signals_per_goal=3,
        state="enabled",
        scope=OWNER_SCOPE,
        evidence_refs=("owner-directive:verified-outcomes",),
        created_at=NOW,
    )
    governance.register_drive(drive, operation_id="full-chain-drive-0001")
    revision = CharterRevisionV1.create(
        charter_key="default",
        revision_label="full-chain-v1",
        parent_revision_id=None,
        title="Owner verified-outcomes charter",
        purpose_summary="Admit bounded read-only evidence goals",
        principles=("Prefer verified reversible progress",),
        drive_weights={drive.drive_id: 0.8},
        ranking_budget=RankingBudgetV1(),
        scope=OWNER_SCOPE,
        evidence_refs=("owner-directive:verified-outcomes",),
        proposed_by="charter-drafter",
        proposed_at=NOW,
        expires_at=NOW + timedelta(days=90),
    )
    governance.propose_charter(
        revision, operation_id="full-chain-charter-proposal-0001",
    )
    request = governance.ensure_transition_request(
        revision.revision_id, transition="activate", now=NOW,
    )
    applied = governance.decide_transition_request(
        request["request_id"],
        decision="approve",
        decision_id="full-chain-charter-decision-0001",
        expected_action_digest=request["action_digest"],
        expected_request_digest=request["request_digest"],
        authority=_charter_authority(),
        now=NOW,
    )
    assert applied["status"] == "approved_applied"
    assert store.active_revision("default", now=NOW) == revision
    return governance, revision


def _complete_thought(job):
    job.status = JobStatus.COMPLETED
    job.result = JobResult(
        job_id=job.job_id,
        worker_node_id="thought-worker",
        status=JobStatus.COMPLETED,
        output={
            "result": json.dumps({
                "schema": "ThoughtOutputV1",
                "version": 1,
                "thought_job_id": job.job_id,
                "thought_job_digest": job.payload["thought_job_digest"],
                "kind": "GoalProposal",
                "title": "Research the owner text observation",
                "objective": (
                    "Research the reported deployment gap and produce a "
                    "verified read-only evidence artifact"
                ),
                "rationale": (
                    "The durable owner-authored text observation requests "
                    "bounded investigation"
                ),
                "evidence_refs": list(job.payload["source_refs"]),
                "required_capabilities": [
                    "memory:read", "reasoning", "web:read",
                ],
                "confidence": 0.84,
            }),
            "tokens_used": 160,
            "model": "production-chain-test-model",
        },
    )


def _complete_work_order(job):
    job.status = JobStatus.COMPLETED
    job.result = JobResult(
        job_id=job.job_id,
        worker_node_id="read-only-action-plane",
        status=JobStatus.COMPLETED,
        output={
            "execution_result": {
                "schema": "ExecutionResultV1",
                "version": 1,
                "work_order_id": job.payload["work_order_id"],
                "work_order_digest": job.payload["work_order_digest"],
                "work_order_version": job.payload["version"],
                "run_id": "full-chain-read-only-run-0001",
                "attempt_number": 1,
                "terminal_outcome": "succeeded",
                "started_at": "2026-07-13T12:01:00+00:00",
                "ended_at": "2026-07-13T12:01:02+00:00",
                "executor_identity": "read-only-action-plane",
                "effect_class": "read_only",
                "receipt_refs": ["artifact:full-chain-verified-evidence"],
                "verification_result": "unverified",
                "summary": "bounded evidence artifact produced",
                "error": "",
            },
        },
    )


@pytest.mark.asyncio
async def test_owner_text_event_reaches_verified_learning_and_settlement(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("COLONY_EVENT_JOURNAL_DIR", str(tmp_path / "events"))
    monkeypatch.setenv("COLONY_EVENT_JOURNAL_RETENTION", "500")
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    monkeypatch.setenv("COLONY_WORKSPACE", "live")
    monkeypatch.setenv("COLONY_EVENT_CONCERNS", "live")
    monkeypatch.setenv("COLONY_EXTERNAL_EVENT_CONCERNS", "live")
    monkeypatch.setenv("COLONY_EVENT_CONCERNS_BOOTSTRAP", "replay")
    monkeypatch.setenv("COLONY_COGNITION_SPINE", "live")
    monkeypatch.setenv("COLONY_PROJECTS_MODE", "live")
    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE", "live")
    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE_BOOTSTRAP", "beginning")
    import colony_sidecar.projects.engine as project_engine_module
    monkeypatch.setattr(project_engine_module, "projects_review_secs", lambda: 0.0)

    governance, charter = _activate_owner_charter(tmp_path)

    event = ExternalCognitionEventV1.from_authority(
        {
            "event_id": "owner-whatsapp-turn-0001",
            "kind": "text_turn_observation",
            "occurred_at": NOW.isoformat(),
            "summary": "Owner requested investigation over WhatsApp",
            "attributes": {
                "turn_id": "owner-whatsapp-turn-0001",
                "channel": "whatsapp",
                "observation": (
                    "Please investigate the reported deployment gap and "
                    "return verified read-only evidence"
                ),
            },
        },
        authority=_owner_ingest_authority(),
        now=NOW,
    )
    intake = ExternalEventIntake(ExternalEventInboxStore(
        str(tmp_path / "external-event-inbox.db"),
    ))
    receipt = intake.ingest(event, now=NOW)
    assert receipt["status"] == "projected"

    concerns = ConcernStore(str(tmp_path / "workspace.db"))
    external_reducer = ExternalEventConcernReducer(concerns)
    reduced = external_reducer.run_once()
    assert reduced["dispositions"] == {"created": 1}
    concern = concerns.active()[0]
    assert concern.subject_person_id == "person-owner"
    assert concern.viewer_scope == "owner"
    assert f"xevent:{event.event_id}" in concern.sources

    manager = ProductionManager()
    cognition = CognitionSpineStore(str(tmp_path / "cognition.db"))
    projects = ProjectStore(str(tmp_path / "projects.db"))
    work_orders = QueueWorkOrderAdapter(
        manager,
        project_store=projects,
        receipt_verifier=ArtifactVerifier(),
    )
    engine = ProjectEngine(
        projects,
        directive_manager=AllowBoundaries(),
        llm_router=PlannerRouter(),
        work_order_adapter=work_orders,
    )
    runtime = CognitionRuntimeContractV1.compose(
        requested_mode="live",
        workspace_mode="live",
        event_concern_mode="live",
        drive_governance_mode="live",
        charter_revision_id=charter.revision_id,
        charter_store_attached=True,
    )
    spine = CognitionSpine(
        concern_store=concerns,
        cognition_store=cognition,
        project_engine=engine,
        thought_queue=ThoughtQueueAdapter(manager, cognition_store=cognition),
        directive_manager=AllowBoundaries(),
        charter_validator=_compose_p7_charter_admission(
            lambda _proposal, _concern: {
                "allowed": True,
                "reason": "base_charter_accepts_read_only_goal",
                "evidence_refs": ["base-charter:read-only"],
            },
            governance.store,
            AllowBoundaries(),
        ),
        situation_validator=lambda *_args: {
            "allowed": True,
            "reason": "capacity_available",
            "evidence_refs": ["situation:capacity-available"],
        },
        available_capabilities={"memory:read", "reasoning", "web:read"},
        enforce_runtime_contract=True,
        runtime_contract_provider=lambda: runtime,
        revision_provider=lambda: {
            "policy_revision": charter.revision_id,
            "situation_revision": "situation:full-chain-v1",
        },
    )

    queued = await spine.process_concern(concern.concern_id, now=NOW)
    assert queued["status"] == "thought_queued"
    thought = manager.queue.jobs[queued["thought_job_id"]]
    assert thought.job_type is JobType.THOUGHT
    _complete_thought(thought)

    created = await spine.process_concern(
        concern.concern_id, now=NOW + timedelta(seconds=1),
    )
    assert created["status"] == "project_created"
    assert projects.count() == 1
    project = projects.get_project(created["project_id"])
    charter_decision = next(
        cognition.get_policy_decision(reference)["payload"]
        for reference in project.policy_decision_refs
        if cognition.get_policy_decision(reference)["payload"]["stage"] == "charter"
    )
    assert charter_decision["allowed"] is True
    assert charter_decision["reason"] == "active_owner_ratified_charter"
    assert charter.revision_id in charter_decision["evidence_refs"]

    first_tick = await engine.tick()
    assert first_tick["planned"] == 1
    steps = projects.steps_for(project.id)
    assert len(steps) == 1
    assert steps[0].action_kind == "research"
    work = next(
        job for job in manager.queue.jobs.values()
        if job.job_type is JobType.AGENT_ACTION
    )
    assert work.payload["schema"] == "WorkOrderV1"
    assert work.payload["risk_class"] == "read_only"
    assert sorted(work.payload["capability_allowlist"]) == [
        "memory:read", "reasoning", "web:read",
    ]
    _complete_work_order(work)

    second_tick = await engine.tick()
    assert second_tick["steps_dispatched"] == 1
    finished = projects.get_project(project.id)
    assert finished.status == "completed"
    assert finished.outcome == "succeeded"

    competence = CompetenceStore(str(tmp_path / "competence.db"))
    evidence = CognitionEvidenceStore(str(tmp_path / "cognition-evidence.db"))
    evidence_reducer = CognitionEvidenceReducer(
        evidence,
        project_store=projects,
        self_model=SelfModel(competence),
        project_event_projector=ProjectEventProjector(projects),
    )
    learned = evidence_reducer.run_once()
    assert learned["project_outbox"]["projected"] >= 1
    assert learned["dispositions"]["verified_execution_success"] == 1
    competence_events = competence.events("project")
    assert len(competence_events) == 1
    assert competence_events[0]["outcome"] == "success"
    assert competence_events[0]["evidence_status"] == "verified"
    trace = evidence.trace(project_id=project.id, viewer_scope="owner")
    assert any(
        item["authority_state"] == "verified"
        and "competence:project" in item["applied_sinks"]
        for item in trace
    )

    assert spine.settle_ready_projects() == 1
    assert concerns.get(concern.concern_id).status == "resolved"
    settlement = concerns.get_settlement(concern.concern_id)
    assert settlement["settlement_kind"] == "project_outcome"
    assert "artifact:full-chain-verified-evidence" in settlement["evidence_refs"]

    evidence.close()
    competence.close()
    cognition.close()
    projects.close()
    governance.store.close()
