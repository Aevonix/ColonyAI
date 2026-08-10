"""P3: one durable Concern -> ThoughtJob -> GoalProposal -> Project spine."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from colony_sidecar.cognition.goal_spine import (
    CognitionSpine,
    CognitionSpineStore,
    ThoughtJobV1,
    ThoughtOutputError,
    ThoughtQueueAdapter,
    bind_thought_output,
    cognition_spine_exclusive,
    cognition_spine_mode,
    parse_thought_output,
)
from colony_sidecar.projects import Project, ProjectEngine, ProjectStore, Step
from colony_sidecar.router.tiers import ModelTier
from colony_sidecar.self_model.workspace import ConcernStore, WorkspaceEngine
from colony_sidecar.task_queue.handlers.inference import InferenceHandler
from colony_sidecar.task_queue.models import Job, JobResult, JobStatus, JobType
from colony_sidecar.work_orders import QueueWorkOrderAdapter


class FakeQueue:
    def __init__(self):
        self.jobs = {}
        self.posts = 0

    async def get_job(self, job_id):
        return self.jobs.get(job_id)

    async def post(self, job):
        if job.job_id in self.jobs:
            raise AssertionError("duplicate queue post")
        self.jobs[job.job_id] = job
        self.posts += 1
        return job.job_id

    async def attest_job_success(
        self, job_id, *, report, verifier_identity, verifier_type,
    ):
        job = self.jobs[job_id]
        result = report.get("execution_result", {})
        if (
            result.get("terminal_outcome") != "succeeded"
            or result.get("verification_result") != "verified"
            or result.get("verifier_identity") != verifier_identity
        ):
            return False
        job.status = JobStatus.COMPLETED
        job.tags["success_attested"] = "true"
        return True


class FakeManager:
    def __init__(self):
        self.queue = FakeQueue()


class BoundaryManager:
    def __init__(self, *, allowed=True, reason="ok"):
        self.allowed = allowed
        self.reason = reason
        self.checked = []

    def check(self, action):
        self.checked.append(action)
        return SimpleNamespace(allowed=self.allowed, reason=self.reason)

    def context_brief(self):
        return "Do not touch explicitly prohibited subjects."


class ArtifactVerifier:
    verifier_type = "artifact_resolver"

    def verify(self, *, result, **_kwargs):
        return {
            "verified": bool(result.receipt_refs),
            "receipt_refs": list(result.receipt_refs),
            "verifier_identity": "test-artifact-resolver:v1",
        }


@pytest.fixture(autouse=True)
def spine_env(monkeypatch):
    monkeypatch.setenv("COLONY_COGNITION_SPINE", "live")
    monkeypatch.setenv("COLONY_PROJECTS_MODE", "live")
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")


def concern(
    store: ConcernStore, *, key="gateway", subject="person-owner", sources=None,
):
    cursor = store.event_cursor("test")
    if cursor is None:
        cursor = store.initialize_event_cursor("test", 0, bootstrap_mode="replay")
    seq = int(cursor) + 1
    result = store.apply_event(
        consumer_id="test",
        event_seq=seq,
        event_id=f"event-{key}",
        event_type="service.degraded",
        material_digest=f"material-{key}",
        projection={
            "operation": "upsert",
            "kind": "maintenance",
            "summary": f"repair {key}",
            "salience": 0.8,
            "dedup_key": f"service:{subject}:{key}",
            "sources": list(sources or (
                f"journal:{seq}:event-{key}", f"service:{key}",
            )),
            "subject_person_id": subject,
            "viewer_scope": "owner",
            "shareability": "owner_private",
            "occurred_at": "2026-07-12T12:00:00+00:00",
            "producer_name": "test_event_reducer",
            "producer_mode": "live",
            "producer_revision": "test-event-reducer:v1",
        },
    )
    return store.get(result["concern_id"])


def goal_output(job, **updates):
    payload = {
        "schema": "ThoughtOutputV1",
        "version": 1,
        "thought_job_id": job.job_id,
        "thought_job_digest": job.payload["thought_job_digest"],
        "kind": "GoalProposal",
        "title": "Restore gateway health",
        "objective": "Diagnose the gateway regression and produce a verified repair",
        "rationale": "The durable health event says the gateway is degraded",
        "evidence_refs": list(job.payload["source_refs"]),
        "required_capabilities": ["memory:read", "reasoning", "web:read"],
        "confidence": 0.86,
    }
    payload.update(updates)
    return payload


def complete_thought(job, output):
    job.status = JobStatus.COMPLETED
    job.result = JobResult(
        job_id=job.job_id,
        worker_node_id="thought-worker",
        status=JobStatus.COMPLETED,
        output={
            "result": json.dumps(output),
            "tokens_used": 180,
            "model": "test-model",
        },
    )


def complete_work_order(job, *, receipt="artifact:verified-analysis"):
    job.status = JobStatus.COMPLETED
    job.result = JobResult(
        job_id=job.job_id,
        worker_node_id="action-plane",
        status=JobStatus.COMPLETED,
        output={
            "execution_result": {
                "schema": "ExecutionResultV1",
                "version": 1,
                "work_order_id": job.payload["work_order_id"],
                "work_order_digest": job.payload["work_order_digest"],
                "work_order_version": job.payload["version"],
                "run_id": "run-cognition-positive",
                "attempt_number": 1,
                "terminal_outcome": "succeeded",
                "started_at": "2026-07-12T13:00:00+00:00",
                "ended_at": "2026-07-12T13:00:01+00:00",
                "executor_identity": "action-plane",
                "effect_class": "none",
                "receipt_refs": [receipt],
                "verification_result": "verified",
                "summary": "analysis artifact recorded",
                "error": "",
            },
        },
    )


def make_spine(tmp_path, *, boundary=None, charter=None, situation=None):
    concerns = ConcernStore(str(tmp_path / "workspace.db"))
    cognition = CognitionSpineStore(str(tmp_path / "cognition.db"))
    projects = ProjectStore(str(tmp_path / "projects.db"))
    project_engine = ProjectEngine(projects)
    manager = FakeManager()
    adapter = ThoughtQueueAdapter(manager, cognition_store=cognition)
    spine = CognitionSpine(
        concern_store=concerns,
        cognition_store=cognition,
        project_engine=project_engine,
        thought_queue=adapter,
        directive_manager=boundary or BoundaryManager(),
        charter_validator=charter or (lambda _proposal, _concern: (True, "in_charter")),
        situation_validator=situation or (lambda _proposal, _concern: (True, "capacity_available")),
        available_capabilities={"memory:read", "reasoning", "web:read"},
    )
    return spine, concerns, cognition, projects, manager


def test_flag_defaults_off_and_live_is_exclusive(monkeypatch):
    monkeypatch.delenv("COLONY_COGNITION_SPINE", raising=False)
    assert cognition_spine_mode() == "off"
    assert not cognition_spine_exclusive()
    monkeypatch.setenv("COLONY_COGNITION_SPINE", "shadow")
    assert not cognition_spine_exclusive()
    monkeypatch.setenv("COLONY_COGNITION_SPINE", "live")
    assert cognition_spine_exclusive()


def test_thought_job_is_deterministic_scoped_bounded_and_read_only(tmp_path):
    _, concerns, _, _, _ = make_spine(tmp_path)
    item = concern(concerns)
    now = datetime(2026, 7, 12, 12, 30, tzinfo=timezone.utc)
    first = ThoughtJobV1.for_concern(
        item,
        attempt_number=1,
        allowed_read_capabilities=("memory:read", "web:read", "reasoning"),
        now=now,
    )
    replay = ThoughtJobV1.for_concern(
        item,
        attempt_number=1,
        allowed_read_capabilities=("reasoning", "memory:read", "web:read"),
        now=now,
    )
    assert first == replay
    assert first.thought_job_id.startswith("thought-")
    assert first.viewer_scope == "owner"
    assert first.subject_person_id == "person-owner"
    assert first.source_event_refs == ("event:event-gateway",)
    assert first.allowed_read_capabilities == (
        "memory:read", "reasoning", "web:read",
    )
    assert first.max_output_tokens <= 1024
    assert first.max_runtime_seconds <= 180
    assert datetime.fromisoformat(first.deadline) > now
    assert all(not cap.endswith(":write") for cap in first.allowed_read_capabilities)


@pytest.mark.asyncio
async def test_full_event_to_work_order_trace_is_idempotent(tmp_path):
    spine, concerns, cognition, projects, manager = make_spine(tmp_path)
    item = concern(concerns)

    queued = await spine.process_concern(
        item.concern_id,
        now=datetime(2026, 7, 12, 12, 30, tzinfo=timezone.utc),
    )
    assert queued["status"] == "thought_queued"
    thought_job = manager.queue.jobs[queued["thought_job_id"]]
    assert thought_job.job_type == JobType.THOUGHT
    assert thought_job.required_capabilities() == ["cognition_scoped"]
    assert thought_job.payload["concern_id"] == item.concern_id
    assert manager.queue.posts == 1

    complete_thought(thought_job, goal_output(thought_job))
    created = await spine.process_concern(item.concern_id)
    replay = await spine.process_concern(item.concern_id)
    assert created["status"] == replay["status"] == "project_created"
    assert created["project_id"] == replay["project_id"]
    assert projects.count() == 1
    assert manager.queue.posts == 1

    project = projects.get_project(created["project_id"])
    assert project.concern_id == item.concern_id
    assert project.thought_job_id == thought_job.job_id
    assert project.goal_proposal_id == created["goal_proposal_id"]
    assert project.source_event_refs == ["event:event-gateway"]
    assert len(project.policy_decision_refs) == 5
    assert all(cognition.get_policy_decision(ref) for ref in project.policy_decision_refs)

    owner_health = spine.health_snapshot(
        viewer_person_id="person-owner",
        owner_person_id="person-owner",
        audiences={"owner"},
    )
    guest_health = spine.health_snapshot(
        viewer_person_id="person-guest",
        owner_person_id="person-owner",
        audiences={"global"},
    )
    chain = owner_health["cognition_trace"][0]
    assert chain["thought_job"]["thought_job_id"] == thought_job.job_id
    assert chain["thought_result"]["result_ref"] == created[
        "thought_result_ref"
    ]
    assert chain["goal_proposal"]["proposal_id"] == created[
        "goal_proposal_id"
    ]
    assert len(chain["policy_decisions"]) == 5
    assert chain["project_link"]["project_id"] == created["project_id"]
    assert guest_health["cognition_trace"] == []

    # Planning is intentionally a separate ProjectEngine concern. Inject one
    # validated step here so the test follows the real WorkOrder bridge.
    project.status = "active"
    projects.save_project(project)
    projects.save_step(Step(
        id="step-cognition-trace",
        project_id=project.id,
        ordinal=1,
        description="Research the regression and return receipt-backed evidence",
        action_kind="research",
    ))
    work_adapter = QueueWorkOrderAdapter(
        manager, project_store=projects,
    )
    spine.project_engine._work_orders = work_adapter
    await spine.project_engine.tick()

    work_jobs = [j for j in manager.queue.jobs.values()
                 if j.job_type == JobType.AGENT_ACTION]
    assert len(work_jobs) == 1
    refs = set(work_jobs[0].payload["context_refs"])
    assert work_jobs[0].payload["recipient_scope"] == "owner"
    assert f"concern:{item.concern_id}" in refs
    assert f"thought-job:{thought_job.job_id}" in refs
    assert created["goal_proposal_id"] in refs
    assert "event:event-gateway" in refs
    assert all(f"policy-decision:{ref.split(':', 1)[-1]}" in refs
               for ref in project.policy_decision_refs)


@pytest.mark.asyncio
async def test_charter_invalid_proposal_cannot_create_project(tmp_path):
    spine, concerns, cognition, projects, manager = make_spine(
        tmp_path,
        charter=lambda _proposal, _concern: (False, "outside_owner_charter"),
    )
    item = concern(concerns)
    queued = await spine.process_concern(item.concern_id)
    job = manager.queue.jobs[queued["thought_job_id"]]
    complete_thought(job, goal_output(job))

    result = await spine.process_concern(item.concern_id)
    assert result["status"] == "proposal_rejected"
    assert result["stage"] == "charter"
    assert projects.count() == 0
    assert concerns.get(item.concern_id).status == "active"
    assert cognition.get_proposal(result["goal_proposal_id"])["status"] == "rejected"


@pytest.mark.asyncio
async def test_boundary_unavailable_or_refusal_fails_closed(tmp_path):
    spine, concerns, _, projects, manager = make_spine(
        tmp_path,
        boundary=BoundaryManager(allowed=False, reason="leave gateway alone"),
    )
    item = concern(concerns)
    queued = await spine.process_concern(item.concern_id)
    job = manager.queue.jobs[queued["thought_job_id"]]
    complete_thought(job, goal_output(job))
    result = await spine.process_concern(item.concern_id)
    assert result["status"] == "proposal_rejected"
    assert result["stage"] == "boundary"
    assert projects.count() == 0


@pytest.mark.asyncio
async def test_autonomous_delivery_is_held_without_attested_envelope(tmp_path):
    spine, concerns, _, projects, manager = make_spine(tmp_path)
    # Even listing messaging as generally available is insufficient: P3 has
    # no digest-bound recipient/artifact attestation yet.
    spine._available_capabilities = frozenset({
        *spine._available_capabilities, "messaging:send",
    })
    item = concern(concerns)
    queued = await spine.process_concern(item.concern_id)
    job = manager.queue.jobs[queued["thought_job_id"]]
    complete_thought(job, goal_output(
        job,
        required_capabilities=["reasoning", "messaging:send"],
    ))
    result = await spine.process_concern(item.concern_id)
    assert result["status"] == "proposal_rejected"
    assert result["stage"] == "authority"
    assert result["reason"] == (
        "p3_deliver_held_missing_attested_recipient_artifact_envelope"
    )
    assert projects.count() == 0


@pytest.mark.asyncio
async def test_timeout_and_budget_exhaustion_leave_concern_resumable(tmp_path):
    spine, concerns, cognition, _, manager = make_spine(tmp_path)
    item = concern(concerns)
    now = datetime(2026, 7, 12, 12, 30, tzinfo=timezone.utc)
    first = await spine.process_concern(item.concern_id, now=now)
    job = manager.queue.jobs[first["thought_job_id"]]
    job.status = JobStatus.FAILED
    job.result = JobResult(
        job_id=job.job_id,
        worker_node_id="thought-worker",
        status=JobStatus.FAILED,
        output={},
        error="Timed out after 180s",
    )
    failed = await spine.process_concern(item.concern_id, now=now + timedelta(minutes=4))
    assert failed["status"] == "thought_failed"
    assert concerns.get(item.concern_id).status == "active"
    assert concerns.get(item.concern_id).thoughts_spent == 0

    resumed = await spine.process_concern(item.concern_id, now=now + timedelta(minutes=5))
    assert resumed["status"] == "thought_queued"
    assert resumed["thought_job_id"] != first["thought_job_id"]
    assert cognition.get_job(resumed["thought_job_id"])["attempt_number"] == 2


@pytest.mark.asyncio
async def test_tampered_worker_scope_is_held_not_retried(tmp_path):
    spine, concerns, _, projects, manager = make_spine(tmp_path)
    item = concern(concerns)
    queued = await spine.process_concern(item.concern_id)
    job = manager.queue.jobs[queued["thought_job_id"]]
    job.capabilities = []
    complete_thought(job, goal_output(job))
    result = await spine.process_concern(item.concern_id)
    assert result["status"] == "thought_authority_conflict"
    assert result["resumable"] is False
    assert concerns.get(item.concern_id).status == "active"
    assert projects.count() == 0


@pytest.mark.asyncio
async def test_duplicate_output_and_proposal_never_duplicate_project(tmp_path):
    spine, concerns, _, projects, manager = make_spine(tmp_path)
    item = concern(concerns)
    queued = await spine.process_concern(item.concern_id)
    job = manager.queue.jobs[queued["thought_job_id"]]
    output = goal_output(job)
    complete_thought(job, output)
    first = await spine.process_concern(item.concern_id)
    second = await spine.process_concern(item.concern_id)
    assert first["project_id"] == second["project_id"]
    assert projects.count() == 1

    forged = projects.get_project(first["project_id"])
    forged.viewer_scope = "public"
    with pytest.raises(ValueError, match="immutable cognition project provenance"):
        projects.save_project(forged)

    # A second material concern proposing the same scoped goal is rejected as
    # duplicate rather than creating a competing ledger entry.
    other = concern(concerns, key="gateway-duplicate")
    queued2 = await spine.process_concern(other.concern_id)
    job2 = manager.queue.jobs[queued2["thought_job_id"]]
    duplicate = goal_output(job2)
    complete_thought(job2, duplicate)
    rejected = await spine.process_concern(other.concern_id)
    assert rejected["status"] == "proposal_rejected"
    assert rejected["stage"] == "duplicate"
    assert projects.count() == 1


@pytest.mark.asyncio
async def test_existing_owner_project_for_source_event_blocks_llm_duplicate(tmp_path):
    spine, concerns, _, projects, manager = make_spine(tmp_path)
    external_event_id = "owner-goal-event-0001"
    exact_source_ref = f"xevent:{external_event_id}"
    host_event_id = "owner-goal-journal-event-0001"
    thought_event_ref = f"event:{host_event_id}"
    item = concern(
        concerns,
        key="owner-goal-source",
        sources=(
            f"journal:1:{host_event_id}",
            exact_source_ref,
            "xdigest:" + "a" * 64,
            "external_kind:text_turn_observation",
        ),
    )
    queued = await spine.process_concern(item.concern_id)
    job = manager.queue.jobs[queued["thought_job_id"]]
    # Thought authority deliberately reduces journal provenance to the
    # server-owned host event ID; it does not copy the producer's xevent ref.
    assert exact_source_ref not in job.payload["source_event_refs"]
    assert thought_event_ref in job.payload["source_event_refs"]
    projects.save_project(Project(
        id="proj-owner-goal-source",
        title="Owner-authored goal",
        objective="The deterministic owner path already accepted this goal",
        source="owner",
        status="active",
        source_event_refs=[exact_source_ref, thought_event_ref],
        subject_person_id="person-owner",
        viewer_scope="owner",
        shareability="owner_private",
    ))
    complete_thought(job, goal_output(
        job,
        title="Model chose different words",
        objective="A differently worded objective must not bypass source dedup",
    ))

    rejected = await spine.process_concern(item.concern_id)

    assert rejected["status"] == "proposal_rejected"
    assert rejected["stage"] == "duplicate"
    assert rejected["reason"] == (
        "source_event_already_projected:proj-owner-goal-source"
    )
    assert projects.count() == 1


def test_only_five_typed_outputs_and_no_model_resolve(tmp_path):
    _, concerns, _, _, _ = make_spine(tmp_path)
    item = concern(concerns)
    job = ThoughtJobV1.for_concern(
        item,
        attempt_number=1,
        allowed_read_capabilities=("memory:read", "reasoning"),
        now=datetime(2026, 7, 12, tzinfo=timezone.utc),
    )
    base = {
        "schema": "ThoughtOutputV1", "version": 1,
        "thought_job_id": job.thought_job_id,
        "thought_job_digest": job.thought_job_digest,
        "confidence": 0.8,
        "evidence_refs": ["journal:1:event-gateway"],
    }
    for kind, fields in {
        "Note": {"note": "The issue needs more observation"},
        "MemoryWriteProposal": {"content": "Gateway degraded at noon"},
        "GoalProposal": {
            "title": "Repair gateway", "objective": "Repair gateway safely",
            "rationale": "durable event", "required_capabilities": ["reasoning"],
        },
        "ExperimentProposal": {
            "hypothesis": "A health probe distinguishes the failure",
            "metric": "gateway.health", "variant": "probe-v2",
        },
        "NoAction": {
            "reason_code": "already_handled", "reason": "A verified repair exists",
        },
    }.items():
        parsed = parse_thought_output({**base, "kind": kind, **fields}, job)
        assert parsed.kind == kind

    with pytest.raises(ThoughtOutputError):
        parse_thought_output({**base, "kind": "Resolve", "resolve": True}, job)


@pytest.mark.asyncio
async def test_model_resolve_cannot_settle_external_source(tmp_path):
    spine, concerns, _, projects, manager = make_spine(tmp_path)
    item = concern(concerns)
    queued = await spine.process_concern(item.concern_id)
    job = manager.queue.jobs[queued["thought_job_id"]]
    bad = goal_output(job, kind="Resolve", resolve=True)
    complete_thought(job, bad)
    result = await spine.process_concern(item.concern_id)
    assert result["status"] == "thought_output_rejected"
    assert concerns.get(item.concern_id).status == "active"
    assert projects.count() == 0


@pytest.mark.asyncio
async def test_reviewable_no_action_settles_only_the_concern(tmp_path):
    spine, concerns, cognition, projects, manager = make_spine(tmp_path)
    item = concern(concerns)
    queued = await spine.process_concern(item.concern_id)
    job = manager.queue.jobs[queued["thought_job_id"]]
    complete_thought(job, {
        "schema": "ThoughtOutputV1", "version": 1,
        "thought_job_id": job.job_id,
        "thought_job_digest": job.payload["thought_job_digest"],
        "kind": "NoAction",
        "reason_code": "already_handled",
        "reason": "The source evidence points to an already verified repair",
        "evidence_refs": ["journal:1:event-gateway"],
        "confidence": 0.91,
    })
    result = await spine.process_concern(item.concern_id)
    assert result["status"] == "concern_settled_no_action"
    assert concerns.get(item.concern_id).status == "resolved"
    assert projects.count() == 0
    settlement = concerns.get_settlement(item.concern_id)
    assert settlement["settlement_kind"] == "no_action"
    assert settlement["evidence_refs"] == ["journal:1:event-gateway"]
    assert cognition.get_result(result["thought_result_ref"])["kind"] == "NoAction"
    # P3 never writes to the upstream commitment/service store; only its
    # scoped cognitive concern is settled.


@pytest.mark.asyncio
async def test_project_settlement_requires_verified_receipts(tmp_path):
    spine, concerns, _, projects, manager = make_spine(tmp_path)
    item = concern(concerns)
    queued = await spine.process_concern(item.concern_id)
    job = manager.queue.jobs[queued["thought_job_id"]]
    complete_thought(job, goal_output(job))
    created = await spine.process_concern(item.concern_id)
    project = projects.get_project(created["project_id"])
    project.status = "completed"
    project.outcome = "unverified"
    projects.save_project(project)
    projects.save_step(Step(
        project_id=project.id, ordinal=1, description="claimed repair",
        action_kind="directed", status="done", result="looks good",
    ))
    assert spine.settle_ready_projects() == 0
    assert concerns.get(item.concern_id).status == "active"


@pytest.mark.asyncio
async def test_verified_terminal_project_receipts_settle_concern(tmp_path):
    spine, concerns, _, projects, manager = make_spine(tmp_path)
    item = concern(concerns)
    queued = await spine.process_concern(item.concern_id)
    thought = manager.queue.jobs[queued["thought_job_id"]]
    complete_thought(thought, goal_output(
        thought,
        required_capabilities=["memory:read", "reasoning"],
    ))
    created = await spine.process_concern(item.concern_id)
    project = projects.get_project(created["project_id"])
    project.status = "active"
    projects.save_project(project)
    projects.save_step(Step(
        id="step-settlement-positive",
        project_id=project.id,
        ordinal=1,
        description="Analyze the durable evidence and write a bounded artifact",
        action_kind="analyze",
    ))
    spine.project_engine._work_orders = QueueWorkOrderAdapter(
        manager,
        project_store=projects,
        receipt_verifier=ArtifactVerifier(),
    )
    await spine.project_engine.tick()
    work = next(job for job in manager.queue.jobs.values()
                if job.job_type == JobType.AGENT_ACTION)
    complete_work_order(work)
    project = projects.get_project(project.id)
    project.next_review_at = 0.0
    projects.save_project(project)
    await spine.project_engine.tick()
    finished = projects.get_project(project.id)
    assert finished.status == "completed"
    assert finished.outcome == "succeeded"
    assert spine.settle_ready_projects() == 1
    assert concerns.get(item.concern_id).status == "resolved"
    settlement = concerns.get_settlement(item.concern_id)
    assert settlement["settlement_kind"] == "project_outcome"
    assert "artifact:verified-analysis" in settlement["evidence_refs"]


@pytest.mark.asyncio
async def test_shadow_records_decisions_without_projects_or_settlement(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_COGNITION_SPINE", "shadow")
    spine, concerns, _, projects, manager = make_spine(tmp_path)
    item = concern(concerns)
    queued = await spine.process_concern(item.concern_id)
    job = manager.queue.jobs[queued["thought_job_id"]]
    complete_thought(job, goal_output(job))
    result = await spine.process_concern(item.concern_id)
    assert result["status"] == "shadow_project_candidate"
    assert projects.count() == 0
    assert concerns.get(item.concern_id).status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "fields", "status"),
    [
        ("Note", {"note": "Observe one more sample"}, "note_recorded"),
        (
            "MemoryWriteProposal",
            {"content": "Candidate memory; not yet authoritative"},
            "memory_write_proposed",
        ),
        (
            "ExperimentProposal",
            {
                "hypothesis": "A second probe improves confidence",
                "metric": "gateway.health",
                "variant": "probe-v2",
            },
            "experiment_proposed",
        ),
    ],
)
async def test_non_action_thought_routes_once_without_execution(
    tmp_path, kind, fields, status,
):
    spine, concerns, cognition, projects, manager = make_spine(tmp_path)
    item = concern(concerns, key=f"route-{kind}")
    queued = await spine.process_concern(item.concern_id)
    job = manager.queue.jobs[queued["thought_job_id"]]
    complete_thought(job, {
        "schema": "ThoughtOutputV1",
        "version": 1,
        "thought_job_id": job.job_id,
        "thought_job_digest": job.payload["thought_job_digest"],
        "kind": kind,
        "evidence_refs": list(job.payload["source_refs"]),
        "confidence": 0.8,
        **fields,
    })

    first = await spine.process_concern(item.concern_id)
    replay = await spine.process_concern(item.concern_id)
    routed = cognition.routed_outputs(concern_id=item.concern_id)

    assert first == replay
    assert first["status"] == status
    assert first["effect_executed"] is False
    assert first["route_ref"] == routed[0]["route_ref"]
    assert len(routed) == 1
    assert routed[0]["effect_executed"] is False
    assert projects.count() == 0


def _revisioned_runtime(spine, revision_state):
    from colony_sidecar.cognition.runtime import CognitionRuntimeContractV1

    spine._enforce_runtime_contract = True
    spine._runtime_contract_provider = lambda: CognitionRuntimeContractV1.compose(
        requested_mode=cognition_spine_mode(),
        workspace_mode="live",
        event_concern_mode="live",
        drive_governance_mode="off",
    )
    spine._revision_provider = lambda: {
        "policy_revision": revision_state["policy"],
        "situation_revision": revision_state["situation"],
    }


@pytest.mark.asyncio
async def test_revision_change_reopens_transient_situation_denial(tmp_path):
    allowed = {"value": False}
    spine, concerns, _, projects, manager = make_spine(
        tmp_path,
        situation=lambda _proposal, _concern: (
            allowed["value"],
            "situation_ready" if allowed["value"] else "situation_temporarily_held",
        ),
    )
    revisions = {"policy": "policy:v1", "situation": "situation:v1"}
    _revisioned_runtime(spine, revisions)
    item = concern(concerns, key="revision-retry")
    queued = await spine.process_concern(item.concern_id, now=datetime(
        2026, 7, 12, 12, 30, tzinfo=timezone.utc,
    ))
    job = manager.queue.jobs[queued["thought_job_id"]]
    complete_thought(job, goal_output(job))
    denied = await spine.process_concern(item.concern_id)
    assert denied["status"] == "proposal_rejected"
    assert denied["stage"] == "situation"

    allowed["value"] = True
    revisions["situation"] = "situation:v2"
    recovered = await spine.process_concern(item.concern_id)
    assert recovered["status"] == "project_created"
    assert projects.count() == 1
    assert recovered["admission_ref"] != denied["admission_ref"]


@pytest.mark.asyncio
async def test_policy_revision_reopens_transient_boundary_denial(tmp_path):
    boundary = BoundaryManager(allowed=False, reason="temporary policy hold")
    spine, concerns, _, projects, manager = make_spine(
        tmp_path, boundary=boundary,
    )
    revisions = {"policy": "policy:v1", "situation": "situation:v1"}
    _revisioned_runtime(spine, revisions)
    item = concern(concerns, key="boundary-revision-retry")
    queued = await spine.process_concern(item.concern_id)
    job = manager.queue.jobs[queued["thought_job_id"]]
    complete_thought(job, goal_output(job))

    denied = await spine.process_concern(item.concern_id)
    assert denied["status"] == "proposal_rejected"
    assert denied["stage"] == "boundary"

    boundary.allowed = True
    revisions["policy"] = "policy:v2"
    recovered = await spine.process_concern(item.concern_id)
    assert recovered["status"] == "project_created"
    assert projects.count() == 1
    assert recovered["admission_ref"] != denied["admission_ref"]


@pytest.mark.asyncio
async def test_capability_revision_reopens_transient_authority_denial(tmp_path):
    spine, concerns, _, projects, manager = make_spine(tmp_path)
    spine._available_capabilities = frozenset({"memory:read", "reasoning"})
    revisions = {"policy": "capabilities:v1", "situation": "situation:v1"}
    _revisioned_runtime(spine, revisions)
    item = concern(concerns, key="authority-revision-retry")
    queued = await spine.process_concern(item.concern_id)
    job = manager.queue.jobs[queued["thought_job_id"]]
    complete_thought(job, goal_output(job))

    denied = await spine.process_concern(item.concern_id)
    assert denied["status"] == "proposal_rejected"
    assert denied["stage"] == "authority"

    spine._available_capabilities = frozenset({
        "memory:read", "reasoning", "web:read",
    })
    revisions["policy"] = "capabilities:v2"
    recovered = await spine.process_concern(item.concern_id)
    assert recovered["status"] == "project_created"
    assert projects.count() == 1
    assert recovered["admission_ref"] != denied["admission_ref"]


@pytest.mark.asyncio
async def test_malformed_output_retries_with_backoff_then_quarantines(
    tmp_path,
):
    spine, concerns, cognition, projects, manager = make_spine(tmp_path)
    revisions = {"policy": "policy:v1", "situation": "situation:v1"}
    _revisioned_runtime(spine, revisions)
    item = concern(concerns, key="malformed-retry")
    now = datetime(2026, 7, 12, 12, 30, tzinfo=timezone.utc)

    for attempt in range(1, 4):
        queued = await spine.process_concern(item.concern_id, now=now)
        assert queued["status"] == "thought_queued"
        job = manager.queue.jobs[queued["thought_job_id"]]
        complete_thought(job, goal_output(job, kind="Resolve", resolve=True))
        rejected = await spine.process_concern(item.concern_id, now=now)
        if attempt < 3:
            assert rejected["status"] == "thought_output_rejected"
            assert rejected["resumable"] is True
            early = await spine.process_concern(
                item.concern_id, now=now + timedelta(seconds=1),
            )
            assert early["status"] == "thought_retry_backoff"
            now += timedelta(seconds=5 * (2 ** (attempt - 1)) + 1)
        else:
            assert rejected["status"] == "thought_output_quarantined"
            assert rejected["resumable"] is False
            assert rejected["quarantined"] is True

    assert cognition.latest_job(
        item.concern_id, item.last_material_digest,
    )["attempt_number"] == 3
    assert projects.count() == 0


@pytest.mark.asyncio
async def test_shadow_goal_requires_exact_owner_promotion_before_live_project(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_COGNITION_SPINE", "shadow")
    situation = {"allowed": True}
    spine, concerns, cognition, projects, manager = make_spine(
        tmp_path,
        situation=lambda _proposal, _concern: (
            situation["allowed"],
            "situation_ready" if situation["allowed"] else "capacity_held",
        ),
    )
    revisions = {"policy": "policy:v1", "situation": "situation:v1"}
    _revisioned_runtime(spine, revisions)
    item = concern(concerns, key="shadow-promotion")
    queued = await spine.process_concern(item.concern_id)
    job = manager.queue.jobs[queued["thought_job_id"]]
    complete_thought(job, goal_output(job))
    shadow = await spine.process_concern(item.concern_id)
    assert shadow["status"] == "shadow_project_candidate"
    assert projects.count() == 0

    monkeypatch.setenv("COLONY_COGNITION_SPINE", "live")
    held = await spine.process_concern(item.concern_id)
    assert held["status"] == "shadow_goal_requires_owner_promotion"
    assert projects.count() == 0

    situation["allowed"] = False
    revisions["situation"] = "situation:v2"
    blocked = await spine.promote_goal_proposal(
        shadow["goal_proposal_id"],
        expected_thought_result_ref=shadow["thought_result_ref"],
        promotion_ref="owner-goal-promotion:test",
    )
    blocked_replay = await spine.promote_goal_proposal(
        shadow["goal_proposal_id"],
        expected_thought_result_ref=shadow["thought_result_ref"],
        promotion_ref="owner-goal-promotion:rotated-credential",
    )
    assert blocked == blocked_replay
    assert blocked["status"] == "goal_promotion_blocked_retryable"
    assert cognition.get_proposal(shadow["goal_proposal_id"])["status"] == (
        "shadow_accepted"
    )
    assert projects.count() == 0

    situation["allowed"] = True
    revisions["situation"] = "situation:v3"
    promoted = await spine.promote_goal_proposal(
        shadow["goal_proposal_id"],
        expected_thought_result_ref=shadow["thought_result_ref"],
        promotion_ref="owner-goal-promotion:test",
    )
    replay = await spine.promote_goal_proposal(
        shadow["goal_proposal_id"],
        expected_thought_result_ref=shadow["thought_result_ref"],
        promotion_ref="owner-goal-promotion:test",
    )
    assert promoted["status"] == replay["status"] == "project_created"
    assert promoted["project_id"] == replay["project_id"]
    assert projects.count() == 1
    attempts = cognition.cognition_trace(limit=10)[0]["goal_promotions"]
    assert [attempt["status"] for attempt in attempts] == [
        "blocked_retryable", "applied",
    ]
    assert len({attempt["attempt_ref"] for attempt in attempts}) == 2


@pytest.mark.asyncio
async def test_shadow_promotion_terminal_rejection_requires_semantic_classification(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_COGNITION_SPINE", "shadow")
    spine, concerns, cognition, projects, manager = make_spine(tmp_path)
    revisions = {"policy": "policy:v1", "situation": "situation:v1"}
    _revisioned_runtime(spine, revisions)
    item = concern(concerns, key="shadow-semantic-rejection")
    queued = await spine.process_concern(item.concern_id)
    job = manager.queue.jobs[queued["thought_job_id"]]
    complete_thought(job, goal_output(job))
    shadow = await spine.process_concern(item.concern_id)
    proposal = cognition.get_proposal(shadow["goal_proposal_id"])

    projects.save_project(Project(
        id="proj-existing-semantic-duplicate",
        title="Existing equivalent goal",
        goal_fingerprint=proposal["goal_fingerprint"],
    ))
    monkeypatch.setenv("COLONY_COGNITION_SPINE", "live")
    result = await spine.promote_goal_proposal(
        shadow["goal_proposal_id"],
        expected_thought_result_ref=shadow["thought_result_ref"],
        promotion_ref="owner-goal-promotion:semantic-duplicate",
    )

    assert result["status"] == "goal_promotion_rejected_permanent"
    assert result["rejection_classification"] == "permanent_semantic"
    assert result["stage"] == "duplicate"
    assert cognition.get_proposal(shadow["goal_proposal_id"])["status"] == (
        "rejected"
    )


def test_cognition_store_reopens_without_duplicate_state(tmp_path):
    path = tmp_path / "cognition.db"
    store = CognitionSpineStore(str(path))
    payload = {
        "schema": "ThoughtJobV1", "version": 1,
        "thought_job_id": "thought-1", "thought_job_digest": "d" * 64,
        "concern_id": "c-1", "attempt_number": 1,
    }
    store.save_job_payload(payload)
    reopened = CognitionSpineStore(str(path))
    assert reopened.get_job("thought-1")["payload"] == payload


@pytest.mark.asyncio
async def test_live_spine_disables_legacy_direct_workspace_thinker(tmp_path):
    calls = {"n": 0}

    async def old_thinker(_concern):
        calls["n"] += 1
        return {"resolve": True, "progress": True, "note": "model says done"}

    store = ConcernStore(str(tmp_path / "legacy-workspace.db"))
    workspace = WorkspaceEngine(store, thinker=old_thinker)
    workspace.bump(kind="question", summary="still open", dedup_key="open")
    assert await workspace.think_once() is None
    assert calls["n"] == 0
    assert store.active()[0].status == "active"


def test_live_spine_refuses_legacy_thinker_project_writer():
    engine = ProjectEngine(ProjectStore())
    project, reason = engine.create_project(
        "parallel autonomous objective", source="thinker",
    )
    assert project is None
    assert reason == "legacy_autonomous_project_writer_read_only"
    forged, forged_reason = engine.create_project(
        "skip the typed spine", source="cognition_spine",
    )
    assert forged is None
    assert forged_reason == "typed_goal_proposal_required"


@pytest.mark.asyncio
async def test_thought_inference_server_binds_authority_and_exact_json_fence(
        tmp_path):
    class Router:
        def __init__(self):
            self.context = None
            self.recorded = False
            self.messages = None
            self.force_tier = None
            self.content_override = None

        async def complete(self, messages, **kwargs):
            self.messages = messages
            self.context = kwargs.get("context")
            self.force_tier = kwargs.get("force_tier")
            semantic = {
                "kind": "Note",
                "evidence_refs": list(thought.source_refs),
                "confidence": 0.82,
                "note": "The concern remains open for bounded review.",
            }
            content = self.content_override or (
                "```json\n" + json.dumps(semantic) + "\n```"
            )
            return SimpleNamespace(
                usage={"total_tokens": 140, "completion_tokens": 40},
                tier_used=ModelTier.SMALL,
                model_id="test-small",
                cost_usd=0.0,
                latency_ms=1,
                request_id="req-thought",
                content=content,
            )

        def record_outcome(self, **_kwargs):
            self.recorded = True

    class World:
        async def connect(self):
            raise AssertionError("strict thought must not connect implicit world context")

    router = Router()
    handler = InferenceHandler(router=router, world_model_store=World())
    item = concern(ConcernStore(str(tmp_path / "handler-concerns.db")))
    thought = ThoughtJobV1.for_concern(
        item,
        attempt_number=1,
        allowed_read_capabilities=("memory:read", "reasoning"),
        now=datetime(2026, 7, 12, tzinfo=timezone.utc),
        max_output_tokens=128,
    )
    job = Job(
        job_id=thought.thought_job_id,
        job_type=JobType.THOUGHT,
        payload=thought.payload(),
    )
    result = await handler.execute(job)
    assert result["completion_tokens"] == 40
    assert router.context == {"task": "thought_job", "max_output_tokens": 128}
    assert router.force_tier is ModelTier.SMALL
    assert router.messages == [
        {"role": "system", "content": thought.system_prompt},
        {"role": "user", "content": thought.prompt},
    ]
    assert thought.thought_job_id not in thought.system_prompt
    assert thought.thought_job_digest not in thought.system_prompt
    assert "server-owned fields" in thought.system_prompt
    assert "kind-specific fields are top-level" in thought.system_prompt
    for reason_code in (
        "already_handled", "not_actionable", "outside_charter",
        "insufficient_evidence", "duplicate_work", "defer_to_owner",
    ):
        assert reason_code in thought.system_prompt
    parsed = parse_thought_output(result["thought_output"], thought)
    assert parsed.kind == "Note"
    assert parsed.thought_job_id == thought.thought_job_id
    assert parsed.thought_job_digest == thought.thought_job_digest
    assert parsed.payload["note"] == "The concern remains open for bounded review."
    assert result["result"] == result["thought_output"]
    assert result["summary"] == result["thought_output"]
    assert router.recorded is False
    assert handler._background_tasks == set()

    router.content_override = json.dumps({
        "kind": "NoAction",
        "evidence_refs": list(thought.source_refs),
        "confidence": 0.5,
        "reason_code": "no_actionable_content",
        "reason": "Unsupported aliases must not be accepted.",
    })
    with pytest.raises(ThoughtOutputError, match="unsupported reason code"):
        await handler.execute(job)
    router.content_override = "not-json"
    with pytest.raises(ThoughtOutputError, match="not valid JSON"):
        await handler.execute(job)

    drifted = dict(thought.payload())
    drifted["prompt"] += " injected context"
    with pytest.raises(ValueError, match="prompt mismatch"):
        await handler.execute(Job(
            job_id=thought.thought_job_id,
            job_type=JobType.THOUGHT,
            payload=drifted,
        ))


def test_thought_model_output_binding_owns_authority_and_rejects_ambiguity(
        tmp_path):
    item = concern(ConcernStore(str(tmp_path / "binding-concerns.db")))
    thought = ThoughtJobV1.for_concern(
        item,
        attempt_number=1,
        allowed_read_capabilities=("memory:read", "reasoning"),
        now=datetime(2026, 7, 12, tzinfo=timezone.utc),
    )
    semantic = {
        "kind": "NoAction",
        "evidence_refs": list(thought.source_refs),
        "confidence": 0.74,
        "reason_code": "not_actionable",
        "reason": "The evidence does not identify a bounded next action.",
    }
    bound = bind_thought_output(json.dumps(semantic), thought)
    assert bound.thought_job_id == thought.thought_job_id
    assert bound.payload["reason_code"] == "not_actionable"

    rebound = bind_thought_output(json.dumps({
        **semantic,
        "schema": "ForgedOutput",
        "version": 999,
        "thought_job_id": "thought-forged",
        "thought_job_digest": "0" * 64,
    }), thought)
    assert rebound.thought_job_id == thought.thought_job_id
    assert rebound.thought_job_digest == thought.thought_job_digest

    nested_raw = {
        "schema": "ThoughtOutputV1",
        "version": 1,
        "thought_job_id": item.concern_id,
        "thought_job_digest": "x",
        "kind": "NoAction",
        "evidence_refs": list(thought.source_refs),
        "confidence": 0.74,
        "NoAction": {
            "schema": "NestedForgedOutput",
            "version": -1,
            "thought_job_id": "thought-nested-forged",
            "thought_job_digest": "f" * 64,
            "reason_code": "not_actionable",
            "reason": "The exact nested shape is normalized before validation.",
        },
    }
    nested = bind_thought_output(
        "```json\n" + json.dumps(nested_raw) + "\n```",
        thought,
    )
    assert nested.payload["reason_code"] == "not_actionable"
    assert nested.thought_job_id == thought.thought_job_id
    assert nested.thought_job_digest == thought.thought_job_digest
    with pytest.raises(ThoughtOutputError, match="conflict with top-level"):
        bind_thought_output(json.dumps({
            **semantic,
            "NoAction": {
                "reason_code": "defer_to_owner",
                "reason": "Conflicting duplicate fields must fail closed.",
            },
        }), thought)
    with pytest.raises(ThoughtOutputError, match="unsupported fields"):
        bind_thought_output(json.dumps({
            **semantic,
            "unexpected": "must fail closed",
        }), thought)
    with pytest.raises(ThoughtOutputError, match="cite supplied"):
        bind_thought_output(json.dumps({
            **semantic,
            "evidence_refs": ["event:not-supplied"],
        }), thought)
    with pytest.raises(ThoughtOutputError, match="confidence must be finite"):
        bind_thought_output(json.dumps({
            **semantic,
            "confidence": float("nan"),
        }), thought)
    with pytest.raises(ThoughtOutputError, match="invalid JSON fence"):
        bind_thought_output(
            "preface\n```json\n" + json.dumps(semantic) + "\n```",
            thought,
        )


@pytest.mark.asyncio
async def test_b689_persisted_rejections_reopen_and_recover_additively(
        tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_COGNITION_SPINE", "shadow")
    spine, concerns, cognition, projects, manager = make_spine(tmp_path)
    now = datetime(2026, 8, 5, 9, 15, tzinfo=timezone.utc)
    first_item = concern(concerns, key="b689-first")
    second_item = concern(concerns, key="b689-second")
    first_queued = await spine.process_concern(first_item.concern_id, now=now)
    second_queued = await spine.process_concern(second_item.concern_id, now=now)
    first_job = manager.queue.jobs[first_queued["thought_job_id"]]
    second_job = manager.queue.jobs[second_queued["thought_job_id"]]

    first_raw = "```json\n" + json.dumps({
        "schema": "ThoughtOutputV1",
        "version": 1,
        "thought_job_id": first_item.concern_id,
        "thought_job_digest": "event-digest-prefix",
        "kind": "NoAction",
        "evidence_refs": list(first_job.payload["source_refs"]),
        "confidence": 0.7,
        "NoAction": {
            "reason_code": "no_actionable_content",
            "reason": "Historical malformed attempt one.",
        },
    }) + "\n```"
    second_raw = json.dumps({
        "schema": "ThoughtOutputV1",
        "version": 1,
        "thought_job_id": second_item.concern_id,
        "thought_job_digest": "x",
        "kind": "Note",
        "evidence_refs": list(second_job.payload["source_refs"]),
        "confidence": 0.7,
        "note": "Historical wrong-authority attempt one.",
    })

    def complete_raw(job, raw):
        job.status = JobStatus.COMPLETED
        job.result = JobResult(
            job_id=job.job_id,
            worker_node_id="historical-thought-worker",
            status=JobStatus.COMPLETED,
            output={
                "result": raw,
                "thought_output": raw,
                "tokens_used": 120,
                "completion_tokens": 40,
            },
        )

    complete_raw(first_job, first_raw)
    complete_raw(second_job, second_raw)
    rejected_second = await spine.process_concern(
        second_item.concern_id, now=now,
    )
    assert rejected_second["status"] == "thought_output_rejected"
    assert cognition.get_job(first_job.job_id)["status"] == "queued"
    assert cognition.get_job(second_job.job_id)["status"] == "processed"

    reopened_concerns = ConcernStore(str(tmp_path / "workspace.db"))
    reopened_cognition = CognitionSpineStore(str(tmp_path / "cognition.db"))
    reopened_projects = ProjectStore(str(tmp_path / "projects.db"))
    reopened_spine = CognitionSpine(
        concern_store=reopened_concerns,
        cognition_store=reopened_cognition,
        project_engine=ProjectEngine(reopened_projects),
        thought_queue=ThoughtQueueAdapter(
            manager, cognition_store=reopened_cognition,
        ),
        directive_manager=BoundaryManager(),
        charter_validator=lambda _proposal, _concern: (True, "in_charter"),
        situation_validator=lambda _proposal, _concern: (
            True, "capacity_available",
        ),
        available_capabilities={"memory:read", "reasoning", "web:read"},
    )
    rejected_first = await reopened_spine.process_concern(
        first_item.concern_id, now=now,
    )
    assert rejected_first["status"] == "thought_output_rejected"

    retry_at = now + timedelta(seconds=6)
    first_retry = await reopened_spine.process_concern(
        first_item.concern_id, now=retry_at,
    )
    second_retry = await reopened_spine.process_concern(
        second_item.concern_id, now=retry_at,
    )
    assert first_retry["status"] == second_retry["status"] == "thought_queued"
    retry_jobs = [
        manager.queue.jobs[first_retry["thought_job_id"]],
        manager.queue.jobs[second_retry["thought_job_id"]],
    ]
    assert all(job.payload["attempt_number"] == 2 for job in retry_jobs)

    class SemanticRouter:
        def __init__(self, refs):
            self.refs = list(refs)

        async def complete(self, _messages, **_kwargs):
            return SimpleNamespace(
                usage={"total_tokens": 120, "completion_tokens": 40},
                tier_used=ModelTier.SMALL,
                model_id="test-small",
                cost_usd=0.0,
                latency_ms=1,
                request_id="req-b689-recovery",
                content=json.dumps({
                    "kind": "NoAction",
                    "evidence_refs": self.refs,
                    "confidence": 0.78,
                    "reason_code": "not_actionable",
                    "reason": "No bounded action follows from this calibration.",
                }),
            )

    class NoWorld:
        async def connect(self):
            raise AssertionError("strict thought must not connect world context")

    for retry_job in retry_jobs:
        handler = InferenceHandler(
            router=SemanticRouter(retry_job.payload["source_refs"]),
            world_model_store=NoWorld(),
        )
        output = await handler.execute(retry_job)
        retry_job.status = JobStatus.COMPLETED
        retry_job.result = JobResult(
            job_id=retry_job.job_id,
            worker_node_id="fixed-thought-worker",
            status=JobStatus.COMPLETED,
            output=output,
        )

    first_done = await reopened_spine.process_concern(
        first_item.concern_id, now=retry_at,
    )
    second_done = await reopened_spine.process_concern(
        second_item.concern_id, now=retry_at,
    )
    assert first_done["status"] == second_done["status"] == "shadow_no_action"
    assert first_done["effect_executed"] is False
    assert second_done["effect_executed"] is False
    assert manager.queue.jobs[first_job.job_id].result.output["thought_output"] == first_raw
    assert manager.queue.jobs[second_job.job_id].result.output["thought_output"] == second_raw
    chains = reopened_cognition.cognition_trace()
    assert len(chains) == 4
    assert sum(chain["thought_result"] is not None for chain in chains) == 2
    assert reopened_cognition.routed_outputs() == []
    assert reopened_cognition.project_links() == []
    assert reopened_projects.count() == projects.count() == 0
