"""Server-side worker enforcement: the WorkerGovernor (Phase B item 5).

Never trust the worker: capability coverage and owner boundaries are
re-decided server-side at claim time, and the report is audited at completion
(a mutation on a read-only job is a violation). Shadow observes without
blocking; live enforces; each job type is its own trust domain.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest

from colony_sidecar.directives import (
    Directive,
    DirectiveGuard,
    DirectiveStore,
    Polarity,
    Verdict,
)
from colony_sidecar.self_model import (
    ActionJournal, CompetenceStore, SelfModel, TrustEngine,
)
from colony_sidecar.task_queue.governor import WorkerGovernor
from colony_sidecar.task_queue.models import (
    Job, JobCapabilityRequirement, JobStatus, JobType, WorkerCapabilities,
)
from colony_sidecar.task_queue.queue_manager import TaskQueueManager
from colony_sidecar.workers import colony_worker as cw
from colony_sidecar.work_orders import WorkOrderV1


def _self_model(*, earned: bool = True):
    store = CompetenceStore()
    journal = ActionJournal()
    trust = TrustEngine(store, journal=journal)
    if earned:
        for job_type in JobType:
            trust.set_stage(
                f"worker:{job_type.value}", "act_first", notify=False,
            )
        trust.confidence = lambda domain: 1.0
    return SelfModel(store, trust=trust, journal=journal)


class _FakeDirectives:
    """Minimal DirectiveManager stand-in: returns a fixed verdict."""

    def __init__(self, allowed: bool, reason: str = "ok") -> None:
        self._v = Verdict(allowed=allowed, reason=reason)

    def check(self, action):  # noqa: ARG002
        return self._v


class _ExplodingDirectives:
    def check(self, action):  # noqa: ARG002
        raise RuntimeError("directive store unavailable")


class _ExplodingGovernor:
    def ready_for_live_claims(self):
        return True

    def evaluate_claim(self, *args, **kwargs):
        raise RuntimeError("governor unavailable")


class _MalformedGovernor:
    def __init__(self, verdict):
        self.verdict = verdict

    def ready_for_live_claims(self):
        return True

    def evaluate_claim(self, *args, **kwargs):
        return self.verdict


def _read_job(**payload):
    p = {"risk": "read_only", "description": "summarize the changelog"}
    p.update(payload)
    return Job(job_type=JobType.RESEARCH, payload=p,
               capabilities=[JobCapabilityRequirement(name="research")])


def _mutating_job(**payload):
    p = {"risk": "high", "description": "refactor module X"}
    p.update(payload)
    return Job(job_type=JobType.AGENT_ACTION, payload=p,
               tags={"approved_by": "owner"})


# -- claim gate: capability coverage (server-side) -----------------------

def test_live_refuses_worker_lacking_required_capability(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    gov = WorkerGovernor(self_model=_self_model())
    job = _read_job()  # requires "research"
    v = gov.evaluate_claim(job, worker_capabilities={"analyst"}, worker_node_id="w1")
    assert v["allowed"] is False
    assert v["capability_ok"] is False
    assert "research" in v["missing_capabilities"]


def test_live_allows_worker_that_covers_capabilities(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    gov = WorkerGovernor(self_model=_self_model())
    job = _read_job()
    v = gov.evaluate_claim(job, worker_capabilities={"research", "read"})
    assert v["allowed"] is True and v["capability_ok"] is True


def test_required_capability_via_tag_enforced(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    gov = WorkerGovernor(self_model=_self_model())
    job = _read_job()
    job.tags["required_capability"] = "gpu"
    v = gov.evaluate_claim(job, worker_capabilities={"research"})
    assert v["allowed"] is False and "gpu" in v["missing_capabilities"]


def test_effect_capability_cannot_be_mislabeled_read_only(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    gov = WorkerGovernor(
        directive_manager=_FakeDirectives(True),
        self_model=_self_model(),
        boundary_required=True,
    )
    job = Job(
        job_type=JobType.AGENT_ACTION,
        payload={"risk": "read_only", "action_hint": "safe-looking"},
        capabilities=[JobCapabilityRequirement(name="messaging:send")],
    )
    verdict = gov.evaluate_claim(
        job, worker_capabilities={"messaging:send"},
    )
    assert verdict.allowed is False
    assert verdict.trust_reason == "action_approval_required"


# -- claim gate: boundary re-check ---------------------------------------

def test_live_refuses_boundaried_job(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    gov = WorkerGovernor(directive_manager=_FakeDirectives(False, "leave X alone"),
                         self_model=_self_model())
    job = _read_job(description="analyze X")
    v = gov.evaluate_claim(job, worker_capabilities={"research"})
    assert v["allowed"] is False and v["boundary_ok"] is False


def test_live_allows_when_boundary_clear(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    gov = WorkerGovernor(directive_manager=_FakeDirectives(True),
                         self_model=_self_model())
    job = _read_job()
    v = gov.evaluate_claim(job, worker_capabilities={"research"})
    assert v["allowed"] is True and v["boundary_ok"] is True


def test_injected_prompt_context_cannot_match_its_own_boundary(
        tmp_path, monkeypatch):
    """Control/context prose is not the autonomous action's subject."""

    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    store = DirectiveStore(db_path=tmp_path / "directives.db")
    store.add(Directive(
        subject="private-surface-blackout",
        polarity=Polarity.PROHIBIT,
        raw_text="do not act on private-surface-blackout",
    ))
    governor = WorkerGovernor(
        directive_manager=DirectiveGuard(store),
        self_model=_self_model(),
        boundary_required=True,
    )
    job = _read_job(
        description="bounded reflection on one owner concern",
        prompt="Consider the supplied evidence without taking action.",
        system_prompt=(
            "<boundaries>do not act on private-surface-blackout</boundaries>"
        ),
        context="Active boundary: private-surface-blackout",
    )

    verdict = governor.evaluate_claim(job, worker_capabilities={"research"})

    assert verdict.allowed is True
    assert verdict.boundary_ok is True


@pytest.mark.parametrize("subject_field", ["target", "command", "path"])
def test_genuine_job_subject_fields_still_match_boundaries(
        subject_field, tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    store = DirectiveStore(db_path=tmp_path / "directives.db")
    store.add(Directive(
        subject="private-surface-blackout",
        polarity=Polarity.PROHIBIT,
        raw_text="do not act on private-surface-blackout",
    ))
    governor = WorkerGovernor(
        directive_manager=DirectiveGuard(store),
        self_model=_self_model(),
        boundary_required=True,
    )
    job = _read_job(
        description="bounded work",
        **{subject_field: "private-surface-blackout"},
    )

    verdict = governor.evaluate_claim(job, worker_capabilities={"research"})

    assert verdict.allowed is False
    assert verdict.boundary_ok is False
    assert "private-surface-blackout" in verdict.boundary_reason


def test_live_refuses_act_first_worker_when_confidence_is_not_earned(
        monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    sm = _self_model(earned=False)
    sm.trust.set_stage("worker:research", "act_first", notify=False)
    gov = WorkerGovernor(
        directive_manager=_FakeDirectives(True),
        self_model=sm,
        boundary_required=True,
    )
    verdict = gov.evaluate_claim(
        _read_job(), worker_capabilities={"research"},
    )
    assert verdict.allowed is False
    assert verdict.trust_ok is False
    assert "confidence" in verdict.trust_reason


@pytest.mark.parametrize("description", [
    "wire $500 to the vendor",
    "rm -rf the old production data",
    "rotate the API key",
    "broadcast a text to everyone",
])
def test_worker_immutable_floor_requires_exact_owner_approval(
        description, monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    gov = WorkerGovernor(
        directive_manager=_FakeDirectives(True),
        self_model=_self_model(),
        boundary_required=True,
    )
    job = _read_job(description=description)
    denied = gov.evaluate_claim(job, worker_capabilities={"research"})
    assert denied.allowed is False
    assert denied.trust_reason.startswith("immutable_floor_")
    job.tags["approved_by"] = "owner-principal"
    allowed = gov.evaluate_claim(job, worker_capabilities={"research"})
    assert allowed.allowed is True


def test_live_refuses_when_boundary_check_raises(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    gov = WorkerGovernor(
        directive_manager=_ExplodingDirectives(),
        self_model=_self_model(),
    )
    v = gov.evaluate_claim(
        _read_job(), worker_capabilities={"research"}, worker_node_id="w1")
    assert v["allowed"] is False
    assert v["boundary_ok"] is False
    assert v["would_refuse"] is True
    assert v["reason"] == "job boundary unavailable: boundary_check_failed_closed"


def test_live_refuses_real_closed_directive_sqlite_store(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    store = DirectiveStore(db_path=tmp_path / "directives.db")
    store.add(Directive(
        subject="all autonomous action",
        polarity=Polarity.PROHIBIT,
        raw_text="stop acting",
    ))
    guard = DirectiveGuard(store)
    store._conn.close()
    governor = WorkerGovernor(
        directive_manager=guard,
        self_model=_self_model(),
        boundary_required=True,
    )
    verdict = governor.evaluate_claim(
        _mutating_job(),
        worker_capabilities=set(),
        worker_node_id="worker-a",
    )
    assert verdict.allowed is False
    assert verdict.boundary_ok is False
    assert verdict.would_refuse is True
    assert verdict.boundary_reason == "boundary_check_failed_closed"
    assert verdict.reason == "job boundary unavailable: boundary_check_failed_closed"


def test_live_refuses_when_configured_boundary_dependency_is_missing(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    gov = WorkerGovernor(
        boundary_required=True,
        self_model=_self_model(),
    )
    v = gov.evaluate_claim(
        _read_job(), worker_capabilities={"research"}, worker_node_id="w1")
    assert v["allowed"] is False
    assert v["boundary_ok"] is False
    assert v["boundary_reason"] == "boundary_checker_unavailable"
    assert v["reason"] == "job boundary unavailable: boundary_checker_unavailable"


# -- mode semantics -------------------------------------------------------

def test_shadow_observes_but_never_blocks(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "shadow")
    gov = WorkerGovernor(directive_manager=_FakeDirectives(False, "boundaried"),
                         self_model=_self_model())
    job = _read_job()
    # Missing cap AND boundaried -> would refuse, but shadow allows (calibration).
    v = gov.evaluate_claim(job, worker_capabilities=set())
    assert v["allowed"] is True
    assert v["would_refuse"] is True
    assert v["shadow"] is True and v["enforced"] is False


def test_shadow_records_boundary_failure_without_blocking(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "shadow")
    gov = WorkerGovernor(
        directive_manager=_ExplodingDirectives(),
        self_model=_self_model(),
    )
    v = gov.evaluate_claim(
        _read_job(), worker_capabilities={"research"}, worker_node_id="w1")
    assert v["allowed"] is True
    assert v["boundary_ok"] is False
    assert v["would_refuse"] is True
    assert v["reason"] == "job boundary unavailable: boundary_check_failed_closed"


def test_off_disables_governor(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    gov = WorkerGovernor(self_model=_self_model())
    v = gov.evaluate_claim(_read_job(), worker_capabilities=set())
    assert v["allowed"] is True and v["enforced"] is False
    assert v["reason"] == "governor_off"


def test_off_mode_reports_unchecked_not_pass(monkeypatch):
    """Mode "off" checks nothing, so capability/boundary/trust must be None
    ("unchecked"), never a fabricated True ("checked and passed")."""
    monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
    gov = WorkerGovernor(self_model=_self_model())
    v = gov.evaluate_claim(_read_job(), worker_capabilities=set())
    assert v["capability_ok"] is None
    assert v["boundary_ok"] is None
    assert v["trust_ok"] is None
    assert v["boundary_reason"] == "governor_off_unchecked"


@pytest.mark.asyncio
@pytest.mark.parametrize("governor", [None, _ExplodingGovernor()])
async def test_live_claim_route_holds_job_when_governor_unavailable(
    tmp_path, monkeypatch, governor,
):
    from colony_sidecar.api.routers import host as host_router
    from colony_sidecar.api.routers import task_queue as queue_router

    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    previous = host_router._worker_governor
    host_router.set_worker_governor(governor)
    try:
        job = _read_job()
        await manager.queue.post(job)
        response = await queue_router.claim_job(queue_router.JobClaimRequest(
            node_id="w1",
            capabilities=["research"],
            job_types=["research"],
        ))
        assert response is None
        held = await manager.queue.get_job(job.job_id)
        assert held.status.value == "blocked"
        assert held.tags["blocked_reason"] == "governor_unavailable"
        assert held.tags["hold_kind"] == "governor_unavailable"
        assert held.tags["governor_reason"] in {
            "governor_unavailable", "governor_claim_unavailable",
        }
        assert held.claimed_by is None
        assert held.claimed_at is None
        assert held.last_heartbeat is None
        status = await manager.queue.governance_status()
        assert status["holds"]["governor_unavailable"] == 1
        assert status["governance_held_jobs"] == [{
            "job_id": job.job_id,
            "job_type": "research",
            "hold_kind": "governor_unavailable",
            "reason": held.tags["governor_reason"],
            "posted_at": job.posted_at.isoformat(),
        }]
    finally:
        host_router.set_worker_governor(previous)
        await manager.stop()
        TaskQueueManager._instance = None


@pytest.mark.asyncio
async def test_shadow_claim_route_reports_governor_failure_without_blocking(
    tmp_path, monkeypatch,
):
    from colony_sidecar.api.routers import host as host_router
    from colony_sidecar.api.routers import task_queue as queue_router

    monkeypatch.setenv("COLONY_WORKERS_MODE", "shadow")
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    previous = host_router._worker_governor
    host_router.set_worker_governor(_ExplodingGovernor())
    try:
        job = _read_job()
        await manager.queue.post(job)
        response = await queue_router.claim_job(queue_router.JobClaimRequest(
            node_id="w1",
            capabilities=["research"],
            job_types=["research"],
        ))
        assert response is not None
        assert response["job_id"] == job.job_id
        assert response["governor"] == {
            "mode": "shadow",
            "enforced": False,
            "would_refuse": True,
            "error": "governor_claim_unavailable",
        }
        claimed = await manager.queue.get_job(job.job_id)
        assert claimed.status.value == "claimed"
    finally:
        host_router.set_worker_governor(previous)
        await manager.stop()
        TaskQueueManager._instance = None


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [
    {},
    {"allowed": True},
    {"allowed": "false", "boundary_ok": False},
    {"allowed": False},
])
async def test_live_central_claim_rejects_malformed_verdicts(
    tmp_path, monkeypatch, malformed,
):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    manager.queue.configure_governance(_MalformedGovernor(malformed))
    try:
        job = _read_job()
        await manager.queue.post(job)
        claimed = await manager.queue.claim_job(
            "raw-bypass",
            WorkerCapabilities(
                node_id="raw-bypass",
                capabilities={"research"},
                job_types={JobType.RESEARCH},
            ),
        )
        assert claimed is None
        held = await manager.queue.get_job(job.job_id)
        assert held.status is JobStatus.BLOCKED
        assert held.tags["hold_kind"] == "governor_unavailable"
        assert held.tags["governor_reason"] == "governor_verdict_invalid"
        assert held.claimed_by is None
    finally:
        await manager.stop()
        TaskQueueManager._instance = None


@pytest.mark.asyncio
async def test_live_central_claim_rejects_compatibility_governor(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    manager.queue.configure_governance(WorkerGovernor(
        directive_manager=_FakeDirectives(True),
        boundary_required=False,
    ))
    try:
        job = _read_job()
        await manager.queue.post(job)
        assert await manager.queue.claim_job(
            "raw", WorkerCapabilities(
                node_id="raw", capabilities={"research"},
                job_types={JobType.RESEARCH},
            )) is None
        held = await manager.queue.get_job(job.job_id)
        assert held.tags["governor_reason"] == "governor_not_live_ready"
        assert held.claimed_by is None
    finally:
        await manager.stop()
        TaskQueueManager._instance = None


@pytest.mark.asyncio
async def test_shadow_and_off_central_claims_preserve_usability(
    tmp_path, monkeypatch,
):
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        monkeypatch.setenv("COLONY_WORKERS_MODE", "shadow")
        shadow_job = Job(job_type=JobType.CUSTOM)
        await manager.queue.post(shadow_job)
        shadow = await manager.queue.claim_job(
            "shadow-worker", WorkerCapabilities(node_id="shadow-worker"))
        assert shadow is not None
        assert shadow.tags["governor_mode"] == "shadow"
        assert shadow.tags["governor_would_refuse"] == "true"
        assert shadow.tags["governor_error"] == "governor_unavailable"
        await manager.queue.release_job(
            shadow.job_id, "shadow-worker", shadow.claim_attempt_id,
        )

        monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
        off = await manager.queue.claim_job(
            "off-worker", WorkerCapabilities(node_id="off-worker"))
        assert off is not None and off.job_id == shadow_job.job_id
        assert off.tags["governor_mode"] == "off"
    finally:
        await manager.stop()
        TaskQueueManager._instance = None


@pytest.mark.asyncio
async def test_embedded_worker_cannot_bypass_missing_live_authority(
    tmp_path, monkeypatch,
):
    from colony_sidecar.task_queue.worker import JobHandler, WorkerNode

    class Handler(JobHandler):
        def __init__(self):
            self.called = False

        async def execute(self, job):
            self.called = True
            return {"status": "completed"}

    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    handler = Handler()
    worker = WorkerNode(
        node_id="embedded",
        queue=manager.queue,
        handlers={JobType.CUSTOM: handler},
        poll_interval_secs=0.02,
        heartbeat_interval_secs=60,
    )
    worker_task = asyncio.create_task(worker.start())
    try:
        job = Job(job_type=JobType.CUSTOM)
        await manager.queue.post(job)
        deadline = asyncio.get_running_loop().time() + 2
        held = None
        while asyncio.get_running_loop().time() < deadline:
            held = await manager.queue.get_job(job.job_id)
            if held is not None and held.status is JobStatus.BLOCKED:
                break
            await asyncio.sleep(0.02)
        assert held is not None and held.status is JobStatus.BLOCKED
        assert held.tags["hold_kind"] == "governor_unavailable"
        assert held.claimed_by is None
        assert handler.called is False
    finally:
        await worker.stop(drain_timeout=1)
        worker_task.cancel()
        try:
            await worker_task
        except (asyncio.CancelledError, Exception):
            pass
        await manager.stop()
        TaskQueueManager._instance = None


@pytest.mark.asyncio
async def test_dependency_unblock_cannot_release_governance_hold(
    tmp_path, monkeypatch,
):
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
        dependency = Job(job_type=JobType.CUSTOM)
        await manager.queue.post(dependency)
        claimed_dep = await manager.queue.claim_job(
            "dep-worker", WorkerCapabilities(node_id="dep-worker"))
        assert claimed_dep is not None
        assert await manager.queue.start_job(
            dependency.job_id, "dep-worker", claimed_dep.claim_attempt_id,
        )
        await manager.queue.complete_job(
            dependency.job_id,
            "dep-worker",
            {"status": "completed"},
            claim_attempt_id=claimed_dep.claim_attempt_id,
        )

        dependent = Job(
            job_type=JobType.CUSTOM,
            depends_on=[dependency.job_id],
        )
        await manager.queue.post(dependent)
        monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
        assert await manager.queue.claim_job(
            "raw", WorkerCapabilities(node_id="raw")) is None
        held = await manager.queue.get_job(dependent.job_id)
        assert held.tags["hold_kind"] == "governor_unavailable"
        assert await manager.queue.unblock_ready_jobs() == 0
        still_held = await manager.queue.get_job(dependent.job_id)
        assert still_held.status is JobStatus.BLOCKED
        assert still_held.claimed_by is None
    finally:
        await manager.stop()
        TaskQueueManager._instance = None


@pytest.mark.asyncio
async def test_governance_hold_recovery_and_mode_rollback_are_exactly_once(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    try:
        first = Job(job_type=JobType.CUSTOM)
        await manager.queue.post(first)
        assert await manager.queue.claim_job(
            "raw", WorkerCapabilities(node_id="raw")) is None
        manager.queue.configure_governance(WorkerGovernor(
            directive_manager=_FakeDirectives(True),
            self_model=_self_model(),
            boundary_required=True,
        ))
        assert await manager.queue.reconcile_governance_holds(force=True) == 1
        assert await manager.queue.reconcile_governance_holds(force=True) == 0
        recovered = await manager.queue.get_job(first.job_id)
        assert recovered.status is JobStatus.QUEUED
        claimed_first = await manager.queue.claim_job(
            "healthy", WorkerCapabilities(node_id="healthy"))
        assert claimed_first is not None and claimed_first.job_id == first.job_id
        assert await manager.queue.start_job(
            first.job_id, "healthy", claimed_first.claim_attempt_id,
        )
        await manager.queue.complete_job(
            first.job_id,
            "healthy",
            {"status": "completed"},
            claim_attempt_id=claimed_first.claim_attempt_id,
        )

        # Put a second job on hold, then prove the explicit off rollback
        # releases only that hold once.
        manager.queue.configure_governance(None)
        second = Job(job_type=JobType.CUSTOM)
        await manager.queue.post(second)
        assert await manager.queue.claim_job(
            "raw", WorkerCapabilities(node_id="raw")) is None
        monkeypatch.setenv("COLONY_WORKERS_MODE", "off")
        assert await manager.queue.reconcile_governance_holds(force=True) == 1
        assert await manager.queue.reconcile_governance_holds(force=True) == 0
        rolled_back = await manager.queue.get_job(second.job_id)
        assert rolled_back.status is JobStatus.QUEUED
    finally:
        await manager.stop()
        TaskQueueManager._instance = None


@pytest.mark.asyncio
async def test_directive_lift_releases_only_boundary_hold(
    tmp_path, monkeypatch,
):
    class MutableDirectives:
        allowed = False

        def check(self, action):
            return Verdict(
                allowed=self.allowed,
                reason="ok" if self.allowed else "leave target alone",
            )

    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    directives = MutableDirectives()
    manager.queue.configure_governance(WorkerGovernor(
        directive_manager=directives,
        self_model=_self_model(),
        boundary_required=True,
    ))
    try:
        job = Job(
            job_type=JobType.SYSTEM_MAINTENANCE,
            payload={"action": "cleanup", "target_path": "/protected"},
            tags={"approved_by": "owner-principal"},
        )
        await manager.queue.post(job)
        assert await manager.queue.claim_job(
            "maintenance", WorkerCapabilities(
                node_id="maintenance",
                job_types={JobType.SYSTEM_MAINTENANCE},
            )) is None
        held = await manager.queue.get_job(job.job_id)
        assert held.tags["hold_kind"] == "boundary"
        directives.allowed = True
        assert await manager.queue.reconcile_governance_holds(force=True) == 1
        assert (await manager.queue.get_job(job.job_id)).status is JobStatus.QUEUED
    finally:
        await manager.stop()
        TaskQueueManager._instance = None


def test_job_action_includes_type_specific_command_path_and_endpoint(monkeypatch):
    class CaptureDirectives:
        action = None

        def check(self, action):
            self.action = action
            return Verdict(allowed=True, reason="ok")

    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    directives = CaptureDirectives()
    governor = WorkerGovernor(
        directive_manager=directives,
        self_model=_self_model(),
        boundary_required=True,
    )
    job = Job(
        job_type=JobType.SYSTEM_MAINTENANCE,
        payload={
            "action": "vacuum_database",
            "target_path": "/srv/private.db",
            "endpoint": "https://internal.invalid/health",
        },
        tags={"approved_by": "owner-principal"},
    )
    verdict = governor.evaluate_claim(job, set(), "maintenance")
    assert verdict.allowed is True
    assert directives.action.target == "/srv/private.db"
    assert "vacuum_database" in directives.action.text
    assert "https://internal.invalid/health" in directives.action.text
    assert directives.action.args["target_path"] == "/srv/private.db"


def test_global_act_pause_refuses_worker_claim(tmp_path, monkeypatch):
    from colony_sidecar.directives import DirectiveManager, DirectiveStore

    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    manager = DirectiveManager(DirectiveStore(
        db_path=str(tmp_path / "directives.db")))
    captured = manager.capture_from_message("pause autonomy")
    assert captured.captured
    governor = WorkerGovernor(
        directive_manager=manager,
        self_model=_self_model(),
        boundary_required=True,
    )
    verdict = governor.evaluate_claim(
        Job(
            job_type=JobType.SYSTEM_MAINTENANCE,
            payload={"action": "vacuum_database", "target_path": "/srv/db"},
        ),
        set(),
        "maintenance",
    )
    assert verdict.allowed is False
    assert verdict.boundary_ok is False
    assert verdict.boundary_reason == "global_pause_active"


def test_malformed_directive_verdict_fails_closed(monkeypatch):
    class MalformedDirectives:
        def check(self, action):
            return {"allowed": True}

    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    governor = WorkerGovernor(
        directive_manager=MalformedDirectives(),
        self_model=_self_model(),
        boundary_required=True,
    )
    verdict = governor.evaluate_claim(Job(), set(), "worker")
    assert verdict.allowed is False
    assert verdict.boundary_ok is False
    assert verdict.boundary_reason == "boundary_check_failed_closed"


def test_worker_governor_setter_syncs_and_clears_singleton_queue(
    tmp_path,
):
    from colony_sidecar.api.routers import host as host_router

    class Queue:
        def __init__(self):
            self.values = []

        def configure_governance(self, value):
            self.values.append(value)

    previous_instance = TaskQueueManager._instance
    previous_governor = host_router._worker_governor
    queue = Queue()
    TaskQueueManager._instance = type("Manager", (), {"queue": queue})()
    governor = object()
    try:
        host_router.set_worker_governor(governor)
        host_router.set_worker_governor(None)
        assert queue.values == [governor, None]
        assert host_router._worker_governor is None
    finally:
        TaskQueueManager._instance = previous_instance
        host_router.set_worker_governor(previous_governor)


def test_server_orders_and_clears_governor_around_embedded_worker():
    import inspect
    from colony_sidecar import server

    source = inspect.getsource(server.lifespan)
    clear_at_entry = source.index("set_worker_governor(None)")
    queue_init = source.index("TaskQueueManager.initialize")
    install = source.index("set_worker_governor(_worker_gov)")
    bridge_start = source.index("agent_bridge_service.start()")
    worker_start = source.index("WorkerNode(")
    clear_at_shutdown = source.rindex("set_worker_governor(None)")
    assert (
        clear_at_entry < queue_init < install < bridge_start
        < worker_start < clear_at_shutdown
    )


def test_embedded_worker_enablement_is_explicit_and_default_compatible(
        monkeypatch):
    from colony_sidecar import server

    monkeypatch.delenv("COLONY_EMBEDDED_WORKER_ENABLED", raising=False)
    assert server._embedded_worker_enabled() is True
    assert server._configured_embedded_worker_enabled() is True
    monkeypatch.setenv("COLONY_EMBEDDED_WORKER_ENABLED", "false")
    assert server._embedded_worker_enabled() is False
    assert server._configured_embedded_worker_enabled() is False
    monkeypatch.setenv("COLONY_EMBEDDED_WORKER_ENABLED", "invalid")
    with pytest.raises(RuntimeError, match="must be true or false"):
        server._embedded_worker_enabled()
    with pytest.raises(RuntimeError, match="must be true or false"):
        server._configured_embedded_worker_enabled()


def test_embedded_worker_helper_matches_release_attestation_shape():
    """Keep Colony compatible with the host's reviewed AST release gate."""

    import ast
    import inspect
    import textwrap

    from colony_sidecar import server

    helper = ast.parse(textwrap.dedent(
        inspect.getsource(server._embedded_worker_enabled)
    )).body[0]
    body = list(helper.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    assert isinstance(body[0], ast.Assign)
    assert isinstance(body[0].targets[0], ast.Name)
    assert body[0].targets[0].id == "raw"
    assert all(
        isinstance(statement.test, ast.Compare)
        and isinstance(statement.test.left, ast.Name)
        and statement.test.left.id == "raw"
        for statement in body[1:3]
    )


def test_embedded_worker_helper_has_one_lifecycle_guard_call_site():
    """The release verifier permits the strict helper only in its worker gate."""

    import ast
    import inspect

    from colony_sidecar import server

    tree = ast.parse(inspect.getsource(server))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_embedded_worker_enabled"
    ]
    assert len(calls) == 1
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    call = calls[0]
    assert isinstance(parents[call], ast.BoolOp)
    guard = parents[parents[call]]
    assert isinstance(guard, ast.If)
    assert isinstance(guard.test, ast.BoolOp)
    assert isinstance(guard.test.op, ast.And)


def test_invalid_worker_governor_mode_fails_closed(monkeypatch):
    from colony_sidecar.task_queue.governor import workers_mode

    monkeypatch.setenv("COLONY_WORKERS_MODE", "typo-live")
    with pytest.raises(RuntimeError, match="must be off, shadow, or live"):
        workers_mode()


def test_queue_scheduler_is_independent_from_embedded_worker_source_gate():
    import inspect
    from colony_sidecar import server

    source = inspect.getsource(server.lifespan)
    scheduler_start = source.index("queue_scheduler = Scheduler(")
    embedded_gate = source.index(
        "if task_queue is not None and _embedded_worker_enabled():"
    )
    scheduler_stop = source.index("await queue_scheduler.stop()")
    queue_stop = source.index("await _task_queue.queue.stop()")
    assert scheduler_start < embedded_gate < scheduler_stop < queue_stop


@pytest.mark.asyncio
async def test_embedded_completion_always_uses_central_auditor(
    tmp_path, monkeypatch,
):
    from colony_sidecar.task_queue.worker import JobHandler, WorkerNode

    class TrackingGovernor(WorkerGovernor):
        def __init__(self):
            super().__init__(
                directive_manager=_FakeDirectives(True),
                self_model=_self_model(),
                boundary_required=True,
            )
            self.audited = []
            self.recorded = []

        def audit_report(self, job, report):
            self.audited.append((job.job_id, dict(report)))
            return super().audit_report(job, report)

        async def record_outcome(self, job, report, verdict, **kwargs):
            self.recorded.append((job.job_id, verdict, kwargs.get("outcome")))

    class Handler(JobHandler):
        async def execute(self, job):
            return {
                "status": "completed",
                "summary": "verified maintenance",
                "operations": ["analyze"],
                "action_plane": {"state": "completed"},
            }

    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    governor = TrackingGovernor()
    manager.queue.configure_governance(governor)
    worker = WorkerNode(
        node_id="embedded-audited",
        queue=manager.queue,
        handlers={JobType.CUSTOM: Handler()},
        poll_interval_secs=0.02,
        heartbeat_interval_secs=60,
    )
    task = asyncio.create_task(worker.start())
    try:
        job = Job(job_type=JobType.CUSTOM, payload={"risk": "read_only"})
        await manager.queue.post(job)
        deadline = asyncio.get_running_loop().time() + 3
        completed = None
        while asyncio.get_running_loop().time() < deadline:
            completed = await manager.queue.get_job(job.job_id)
            if completed is not None and completed.status is JobStatus.COMPLETED:
                break
            await asyncio.sleep(0.02)
        assert completed is not None and completed.status is JobStatus.COMPLETED
        assert governor.audited and governor.audited[0][0] == job.job_id
        assert governor.recorded == [(job.job_id, "clean", "success")]
        assert completed.tags["governor_verdict"] == "clean"
        assert completed.tags["governor_outcome"] == "success"
    finally:
        await worker.stop(drain_timeout=1)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        await manager.stop()
        TaskQueueManager._instance = None


# -- completion audit: never trust the report ----------------------------

def test_audit_flags_mutation_on_read_only_job():
    gov = WorkerGovernor()
    job = _read_job()
    audit = gov.audit_report(job, {"summary": "done", "operations": ["commit"],
                                   "commits": 2})
    assert audit["verdict"] == "violation"
    assert audit["findings"]


def test_audit_clean_on_read_only_read_report():
    gov = WorkerGovernor()
    job = _read_job()
    audit = gov.audit_report(job, {"summary": "analysis complete",
                                   "operations": ["analyze", "read"],
                                   "commits": 0})
    assert audit["verdict"] == "clean"


def test_audit_allows_mutation_on_authorized_job():
    gov = WorkerGovernor()
    job = _mutating_job()
    audit = gov.audit_report(job, {"summary": "patched", "operations": ["commit"],
                                   "commits": 1, "branch": "colony/fix"})
    assert audit["verdict"] == "clean"


def test_audit_force_push_always_violation():
    gov = WorkerGovernor()
    job = _mutating_job()
    audit = gov.audit_report(job, {"summary": "x", "force_push": True})
    assert audit["verdict"] == "violation"


def test_audit_empty_report_is_unverified():
    gov = WorkerGovernor()
    assert gov.audit_report(_read_job(), {})["verdict"] == "unverified"


# -- outcome recording feeds the trust engine ----------------------------

@pytest.mark.asyncio
async def test_read_only_worker_earns_bounded_live_trials_then_act_first(
    monkeypatch,
):
    """Fresh no-effect work can graduate without an approval deadlock."""

    monkeypatch.setenv("COLONY_TRUST_ASK_MIN_N", "3")
    monkeypatch.setenv("COLONY_TRUST_ACT_MIN_N", "5")
    monkeypatch.setenv("COLONY_TRUST_READ_ONLY_TRIAL_MAX", "5")
    sm = _self_model(earned=False)
    governor = WorkerGovernor(
        directive_manager=_FakeDirectives(True),
        self_model=sm,
        boundary_required=True,
    )
    job = _read_job()
    report = {
        "status": "verified",
        "summary": "fresh read-only result was independently verified",
    }

    monkeypatch.setenv("COLONY_WORKERS_MODE", "shadow")
    for index in range(3):
        claim = governor.evaluate_claim(job, {"research"}, "reader")
        assert claim.allowed is True
        await governor.record_outcome(
            job,
            report,
            "clean",
            event_id=f"read-calibration-{index}",
            event_mode="shadow",
            success_attested=True,
        )
    assert sm.trust.stage("worker:research") == "ask_first"

    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    floor_job = _read_job(description="rotate the API key")
    assert governor.evaluate_claim(
        floor_job, {"research"}, "reader",
    ).trust_reason == "immutable_floor_credential_change"
    effect_job = Job(
        job_type=JobType.AGENT_ACTION,
        payload={"risk": "high", "description": "change state"},
    )
    effect_claim = governor.evaluate_claim(effect_job, set(), "reader")
    assert effect_claim.allowed is False
    assert effect_claim.trust_reason == "action_approval_required"

    for index in range(5):
        claim = governor.evaluate_claim(job, {"research"}, "reader")
        assert claim.allowed is True
        assert claim.trust_reason == (
            f"bounded_read_only_live_trial_{index + 1}_of_5"
        )
        await governor.record_outcome(
            job,
            report,
            "clean",
            event_id=f"read-live-trial-{index}",
            event_mode="live",
            success_attested=True,
        )

    assert sm.trust.stage("worker:research") == "act_first"
    autonomous = governor.evaluate_claim(job, {"research"}, "reader")
    assert autonomous.allowed is True
    assert autonomous.trust_reason.startswith(
        "worker_trust_act_first_confidence_"
    )


@pytest.mark.asyncio
async def test_read_only_live_trial_budget_counts_unverified_attempts(
    monkeypatch,
):
    """A worker cannot get unlimited trials by withholding attestation."""

    monkeypatch.setenv("COLONY_TRUST_ASK_MIN_N", "1")
    monkeypatch.setenv("COLONY_TRUST_ACT_MIN_N", "2")
    monkeypatch.setenv("COLONY_TRUST_READ_ONLY_TRIAL_MAX", "1")
    sm = _self_model(earned=False)
    governor = WorkerGovernor(
        directive_manager=_FakeDirectives(True),
        self_model=sm,
        boundary_required=True,
    )
    job = _read_job()
    report = {"status": "verified", "summary": "read-only result"}
    await governor.record_outcome(
        job,
        report,
        "clean",
        event_id="bounded-calibration",
        event_mode="shadow",
        success_attested=True,
    )
    assert sm.trust.stage("worker:research") == "ask_first"

    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    assert governor.evaluate_claim(job, {"research"}).allowed is True
    await governor.record_outcome(
        job,
        report,
        "clean",
        event_id="bounded-unverified-real-attempt",
        event_mode="live",
        success_attested=False,
    )
    exhausted = governor.evaluate_claim(job, {"research"})
    assert exhausted.allowed is False
    assert exhausted.trust_reason == "read_only_live_trial_budget_exhausted_1"


@pytest.mark.asyncio
async def test_record_outcome_live_feeds_real_trust_domain(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    sm = _self_model(earned=False)
    gov = WorkerGovernor(self_model=sm)
    job = _read_job()
    await gov.record_outcome(
        job,
        {
            "status": "completed",
            "summary": "ok",
            "confidence": 0.9,
            "action_plane": {"state": "completed"},
        },
        "clean",
        latency=1.0,
    )
    events = sm.store.events("worker:research", include_shadow=False)
    assert len(events) == 1 and events[0]["outcome"] == "success"
    assert events[0]["shadow"] == 0
    assert events[0]["evidence_status"] == "observed"
    assert sm.trust.confidence("worker:research") == 0.5
    # journaled
    assert sm.journal.recent(domain="worker:research")


@pytest.mark.asyncio
async def test_stable_worker_event_id_deduplicates_competence(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    sm = _self_model()
    gov = WorkerGovernor(self_model=sm)
    job = _read_job()
    report = {
        "status": "verified",
        "summary": "done",
        "confidence": 0.9,
    }
    await gov.record_outcome(
        job, report, "clean", outcome="success", event_id="worker-event-1",
    )
    await gov.record_outcome(
        job, report, "clean", outcome="success", event_id="worker-event-1",
    )
    assert len(sm.store.events("worker:research", include_shadow=False)) == 1
    assert len(sm.journal.recent(domain="worker:research")) == 1


@pytest.mark.asyncio
async def test_duplicate_success_replay_does_not_redistill_skill(monkeypatch):
    from colony_sidecar.skills_memory import SkillStore

    class _LLM:
        def __init__(self):
            self.calls = 0

        async def complete(self, messages, context=None):  # noqa: ARG002
            self.calls += 1
            return SimpleNamespace(content=(
                '{"title":"Inspect the source","situation":"A retry '
                'reveals a source issue","steps":["Inspect the source",'
                '"Verify the result"],"gotchas":[]}'
            ))

    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    monkeypatch.setenv("COLONY_SKILLS_DISTILL", "live")
    sm = _self_model()
    skills = SkillStore()
    llm = _LLM()
    governor = WorkerGovernor(
        self_model=sm,
        skill_store=skills,
        llm_router=llm,
    )
    report = {
        "status": "verified",
        "summary": "The root cause was fixed by inspecting the source.",
    }
    for _ in range(2):
        await governor.record_outcome(
            _read_job(),
            report,
            "clean",
            outcome="success",
            attempts=1,
            event_id="distill-once",
            success_attested=True,
        )
    assert llm.calls == 1
    assert skills.count() == 1


@pytest.mark.asyncio
async def test_duplicate_violation_replay_does_not_redeliver(monkeypatch):
    deliveries = []

    async def deliver(payload):
        deliveries.append(payload)
        return True

    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    governor = WorkerGovernor(
        self_model=_self_model(),
        delivery_router=deliver,
    )
    report = {"operations": ["commit"], "commits": 1}
    for _ in range(2):
        await governor.record_outcome(
            _read_job(),
            report,
            "violation",
            outcome="failure",
            event_id="violation-notice-once",
        )
    assert len(deliveries) == 1


@pytest.mark.asyncio
async def test_failed_violation_delivery_retries_before_outbox_ack(monkeypatch):
    calls = 0

    async def deliver(payload):  # noqa: ARG001
        nonlocal calls
        calls += 1
        return calls > 1

    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    governor = WorkerGovernor(
        self_model=_self_model(),
        delivery_router=deliver,
    )
    kwargs = {
        "job": _read_job(),
        "report": {"operations": ["commit"], "commits": 1},
        "verdict": "violation",
        "outcome": "failure",
        "event_id": "retry-violation-notice",
    }
    with pytest.raises(RuntimeError, match="not delivered"):
        await governor.record_outcome(**kwargs)
    replay = await governor.record_outcome(**kwargs)
    assert replay["duplicate"] is True
    await governor.record_outcome(**kwargs)
    assert calls == 2


@pytest.mark.asyncio
async def test_failed_event_insert_rolls_back_competence_aggregate(monkeypatch):
    """A retry after an event-ledger failure must not double competence."""

    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    sm = _self_model()
    gov = WorkerGovernor(self_model=sm)
    sm.store._conn.execute(
        """CREATE TRIGGER reject_worker_event
           BEFORE INSERT ON competence_events
           WHEN NEW.event_key = 'retry-event'
           BEGIN
             SELECT RAISE(ABORT, 'simulated event write failure');
           END"""
    )
    sm.store._conn.commit()
    report = {"status": "verified", "summary": "done"}
    with pytest.raises(RuntimeError, match="not durably recorded"):
        await gov.record_outcome(
            _read_job(),
            report,
            "clean",
            outcome="success",
            event_id="retry-event",
        )
    assert sm.store.get("worker:research") is None

    sm.store._conn.execute("DROP TRIGGER reject_worker_event")
    sm.store._conn.commit()
    await gov.record_outcome(
        _read_job(),
        report,
        "clean",
        outcome="success",
        event_id="retry-event",
    )
    aggregate = sm.store.get("worker:research")
    assert aggregate is not None and aggregate["success"] == 1


@pytest.mark.asyncio
async def test_duplicate_replay_reconciles_trust_before_outbox_ack(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    sm = _self_model()
    gov = WorkerGovernor(self_model=sm)
    original = sm.trust.after_outcome
    calls = 0

    def fail_once(domain):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated trust-store outage")
        return original(domain)

    sm.trust.after_outcome = fail_once
    kwargs = {
        "outcome": "failure",
        "event_id": "trust-retry-event",
    }
    with pytest.raises(RuntimeError, match="simulated trust-store outage"):
        await gov.record_outcome(
            _read_job(), {"status": "failed"}, "clean", **kwargs,
        )
    replay = await gov.record_outcome(
        _read_job(), {"status": "failed"}, "clean", **kwargs,
    )
    assert replay["duplicate"] is True
    assert len(sm.store.events("worker:research", include_shadow=False)) == 1
    assert len(sm.journal.recent(domain="worker:research")) == 1


@pytest.mark.asyncio
async def test_record_outcome_shadow_is_calibration(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "shadow")
    sm = _self_model()
    gov = WorkerGovernor(self_model=sm)
    await gov.record_outcome(
        _read_job(),
        {
            "status": "completed",
            "summary": "ok",
            "action_plane": {"state": "completed"},
        },
        "clean",
    )
    real = sm.store.events("worker:research", include_shadow=False)
    allev = sm.store.events("worker:research", include_shadow=True)
    assert len(real) == 0 and len(allev) == 1  # shadow event only


@pytest.mark.asyncio
async def test_record_outcome_preserves_mode_captured_by_outbox(monkeypatch):
    """A delayed shadow result must not become live evidence after a flip."""

    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    sm = _self_model()
    gov = WorkerGovernor(self_model=sm)
    report = {
        "status": "verified",
        "summary": "ok",
        "action_plane": {"state": "completed"},
    }
    await gov.record_outcome(
        _read_job(),
        report,
        "clean",
        event_id="captured-shadow-event",
        event_mode="shadow",
    )
    assert sm.store.events("worker:research", include_shadow=False) == []
    events = sm.store.events("worker:research", include_shadow=True)
    assert len(events) == 1 and events[0]["shadow"] == 1


@pytest.mark.parametrize(
    ("report", "verdict", "expected"),
    [
        ({"status": "skipped", "action_plane": {"state": "skipped"}},
         "unverified", "neutral"),
        ({"status": "cancelled"}, "clean", "neutral"),
        ({"status": "unknown", "summary": "maybe"}, "clean", "neutral"),
        ({"status": "completed", "summary": "worker says done"},
         "clean", "neutral"),
        ({"summary": "old callback without semantics"}, "clean", "neutral"),
        ({"status": "failed", "summary": "nope"}, "clean", "failure"),
        ({"status": "completed", "action_plane": {"state": "completed"},
          "summary": "verified"}, "clean", "success"),
        ({"status": "verified", "summary": "verified"}, "clean", "success"),
        ({"status": "completed", "action_plane": {"state": "completed"}},
         "violation", "failure"),
    ],
)
def test_completion_outcome_classifier_is_truthful(report, verdict, expected):
    outcome, _reason = WorkerGovernor.classify_completion_outcome(report, verdict)
    assert outcome == expected


class _Feedback:
    def __init__(self):
        self.calls = []

    def record(self, *args):
        self.calls.append(args)


@pytest.mark.asyncio
async def test_neutral_completion_records_no_competence_or_feedback(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    sm = _self_model()
    feedback = _Feedback()
    gov = WorkerGovernor(self_model=sm, feedback_store=feedback)
    await gov.record_outcome(
        _read_job(),
        {"status": "skipped", "action_plane": {"state": "skipped"}},
        "unverified",
    )
    assert sm.store.events("worker:research", include_shadow=True) == []
    assert feedback.calls == []
    journal = sm.journal.recent(domain="worker:research")
    assert journal and journal[0]["outcome"] == "neutral"


@pytest.mark.asyncio
async def test_caller_cannot_assert_success_for_skipped_work(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    sm = _self_model()
    gov = WorkerGovernor(self_model=sm)
    await gov.record_outcome(
        _read_job(),
        {"status": "skipped", "action_plane": {"state": "skipped"}},
        "unverified",
        outcome="success",
    )
    assert sm.store.events("worker:research", include_shadow=True) == []
    journal = sm.journal.recent(domain="worker:research")
    assert "rejected_explicit_success" in journal[0]["reasoning"]


@pytest.mark.asyncio
async def test_explicit_failure_remains_negative_trust_evidence(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    sm = _self_model()
    feedback = _Feedback()
    gov = WorkerGovernor(self_model=sm, feedback_store=feedback)
    job = _read_job()
    await gov.record_outcome(
        job, {"summary": "worker failed"}, "clean", outcome="failure"
    )
    events = sm.store.events("worker:research", include_shadow=False)
    assert len(events) == 1 and events[0]["outcome"] == "failure"
    assert events[0]["source"] == "task_queue.governor"
    assert events[0]["source_ref"] == job.job_id
    assert events[0]["evidence_status"] == "observed"
    assert events[0]["outcome_contract"] == "colony.worker-outcome/v1"
    assert events[0]["evidence"]["classification"] == "explicit_outcome"
    assert feedback.calls == []


@pytest.mark.asyncio
async def test_complete_endpoint_tags_skipped_as_neutral(tmp_path, monkeypatch):
    from colony_sidecar.api.routers import host as host_router
    from colony_sidecar.api.routers import task_queue as queue_router

    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    previous = host_router._worker_governor
    sm = _self_model()
    host_router.set_worker_governor(WorkerGovernor(
        directive_manager=_FakeDirectives(True),
        boundary_required=True,
        self_model=sm,
    ))
    try:
        submitted = await manager.submit(
            task_type="research",
            params={"risk": "read_only", "description": "optional sync"},
        )
        claimed = await manager.queue.claim_job(
            "w1", WorkerCapabilities(node_id="w1")
        )
        assert claimed is not None and claimed.job_id == submitted["id"]
        await manager.queue.start_job(
            submitted["id"], "w1", claimed.claim_attempt_id,
        )
        response = await queue_router.complete_job(
            submitted["id"],
            queue_router.JobCompleteRequest(output={
                "status": "skipped",
                "reason": "not applicable",
                "action_plane": {"state": "skipped"},
            }, claim_attempt_id=claimed.claim_attempt_id),
        )
        assert response["governor_outcome"] == "neutral"
        assert response["job_status"] == "neutral"
        completed = await manager.queue.get_job(submitted["id"])
        assert completed.status is JobStatus.NEUTRAL
        assert completed.tags["governor_outcome"] == "neutral"
        assert sm.store.events("worker:research", include_shadow=True) == []
    finally:
        host_router.set_worker_governor(previous)
        await manager.stop()
        TaskQueueManager._instance = None


@pytest.mark.asyncio
async def test_work_order_queue_completion_is_transport_not_success(tmp_path, monkeypatch):
    from colony_sidecar.api.routers import host as host_router
    from colony_sidecar.api.routers import task_queue as queue_router

    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    TaskQueueManager._instance = None
    manager = await TaskQueueManager.initialize(db_path=tmp_path / "queue.db")
    previous = host_router._worker_governor
    sm = _self_model()
    host_router.set_worker_governor(WorkerGovernor(
        directive_manager=_FakeDirectives(True),
        boundary_required=True,
        self_model=sm,
    ))
    try:
        order = WorkOrderV1.for_project_step(
            SimpleNamespace(
                id="project-transport",
                title="Transport",
                objective="Verify neutral WorkOrder completion",
                source="owner",
                subject_person_id="owner",
                entity_ids=(),
                created_at=1_783_871_200.0,
            ),
            SimpleNamespace(
                id="step-transport",
                ordinal=0,
                description="Perform read-only research",
                action_kind="research",
                boundary_subject="owner",
                work_order_issued_at=1_783_871_200.0,
                created_at=1_783_871_200.0,
            ),
            now=datetime(2026, 7, 12, 12, tzinfo=timezone.utc),
        )
        queued = Job(
            job_id=order.work_order_id,
            job_type=JobType.AGENT_ACTION,
            payload=order.payload(),
        )
        await manager.queue.post(queued)
        submitted = {"id": queued.job_id}
        claimed = await manager.queue.claim_job(
            "w1", WorkerCapabilities(
                node_id="w1",
                capabilities={
                    "work_order:v1", "action_plane:v1",
                    "memory:read", "web:read", "reasoning",
                },
            )
        )
        assert claimed is not None and claimed.job_id == submitted["id"]
        await manager.queue.start_job(
            submitted["id"], "w1", claimed.claim_attempt_id,
        )
        response = await queue_router.complete_job(
            submitted["id"],
            queue_router.JobCompleteRequest(output={
                "status": "completed",
                "summary": "worker claims success",
                "action_plane": {"state": "completed"},
            }, claim_attempt_id=claimed.claim_attempt_id),
        )
        assert response["governor_outcome"] == "neutral"
        assert response["outcome_reason"] == "work_order_transport_only"
        assert response["job_status"] == "neutral"
        completed = await manager.queue.get_job(submitted["id"])
        assert completed.status is JobStatus.NEUTRAL
        assert completed.tags["governor_outcome"] == "neutral"
        assert sm.store.events("worker:agent_action", include_shadow=True) == []
    finally:
        host_router.set_worker_governor(previous)
        await manager.stop()
        TaskQueueManager._instance = None


@pytest.mark.asyncio
async def test_violation_records_and_trips_breaker(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    sm = _self_model()
    # Pre-graduate the domain to act_first so a violation can demote it.
    sm.trust.set_stage("worker:research", "act_first", notify=False)
    gov = WorkerGovernor(self_model=sm)
    job = _read_job()
    await gov.record_outcome(job, {"operations": ["commit"], "commits": 1},
                             "violation")
    ev = sm.store.events("worker:research", include_shadow=False)
    assert ev and ev[0]["violation"] == 1
    assert sm.trust.stage("worker:research") == "ask_first"  # breaker demoted
    refused = gov.evaluate_claim(job, worker_capabilities={"research"})
    assert refused.allowed is False
    assert refused.trust_ok is False
    job.tags["approved_by"] = "owner-principal"
    approved = gov.evaluate_claim(job, worker_capabilities={"research"})
    assert approved.allowed is True
    assert approved.trust_ok is True


# -- status ---------------------------------------------------------------

def test_status_reports_mode_and_worker_domains(monkeypatch):
    monkeypatch.setenv("COLONY_WORKERS_MODE", "live")
    sm = _self_model()
    sm.trust.set_stage("worker:research", "ask_first", notify=False)
    gov = WorkerGovernor(self_model=sm)
    st = gov.status()
    assert st["mode"] == "live"
    assert any(d["domain"] == "worker:research" for d in st["worker_domains"])


# -- worker daemon pure helpers ------------------------------------------

def test_worker_parse_report_enforces_read_only_posture():
    report = cw._parse_report(
        '{"summary": "found it", "operations": ["analyze", "commit", "delete"],'
        ' "commits": 5, "confidence": 1.7}')
    assert report["operations"] == ["analyze"]  # mutate ops dropped
    assert report["files_touched"] == [] and report["commits"] == 0
    assert report["confidence"] == 1.0  # clamped


def test_worker_build_messages_includes_job_fields():
    msgs = cw.build_llm_messages({"payload": {"description": "check the logs",
                                              "domain": "ops"}})
    assert msgs[0]["role"] == "system"
    assert "check the logs" in msgs[1]["content"]


def test_worker_config_defaults(monkeypatch):
    for k in ("COLONY_WORKER_CAPABILITIES", "COLONY_WORKER_JOB_TYPES",
              "COLONY_WORKER_NODE_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("COLONY_AGENT_NAME", "Test Bot")
    cfg = cw.load_config()
    assert cfg["node_id"] == "test-bot-worker"
    assert "research" in cfg["capabilities"]


def test_external_colony_worker_starts_before_llm_and_completion(monkeypatch):
    calls = []

    def fake_post(cfg, url, body, timeout=15):  # noqa: ARG001
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("/complete"):
            return {
                "success": True,
                "transitioned": True,
                "verdict": "clean",
                "job_status": "completed",
                "governor_outcome": "success",
            }
        return {"success": True}

    monkeypatch.setattr(cw, "_post", fake_post)
    monkeypatch.setattr(
        cw,
        "call_llm",
        lambda cfg, messages: calls.append("llm") or {
            "summary": "done", "operations": ["analyze"], "confidence": 1.0,
            "files_touched": [], "commits": 0, "branch": "", "remaining_work": "",
        },
    )
    assert cw.execute_job({"colony_url": "http://colony"}, {"job_id": "job-1"})
    assert calls.index("start") < calls.index("llm") < calls.index("complete")
