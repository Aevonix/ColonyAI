"""Phase B runtime, provenance, and proposal-routing contracts for P3."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from colony_sidecar.cognition.goal_spine import (
    CognitionSpine,
    CognitionSpineStore,
    ThoughtOutputV1,
    ThoughtProposalPresentationSink,
)
from colony_sidecar.proposals import ProposalStore
from colony_sidecar.cognition.runtime import CognitionRuntimeContractV1
from colony_sidecar.self_model.workspace import ConcernStore
from colony_sidecar.task_queue.handlers.inference import (
    InferenceHandler,
    ThoughtOnlyInferenceHandler,
)
from colony_sidecar.task_queue.handlers.registry import build_default_handlers
from colony_sidecar.task_queue.models import Job, JobType
from colony_sidecar.task_queue.worker import JobHandler, WorkerNode
from colony_sidecar.server import (
    _cognition_owner_spec,
    _cognition_worker_profile,
)
from colony_sidecar.autonomy.loop import _record_p3_thinker_candidates


NOW = datetime(2026, 7, 12, 18, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _phase_b_env(monkeypatch):
    monkeypatch.setenv("COLONY_COGNITION_SPINE", "live")
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")


def _runtime(**updates):
    values = {
        "requested_mode": "live",
        "workspace_mode": "live",
        "event_concern_mode": "shadow",
        "drive_governance_mode": "shadow",
    }
    values.update(updates)
    return CognitionRuntimeContractV1.compose(**values)


def _event_concern(
    store: ConcernStore,
    *,
    event_id: str,
    producer_mode: str,
    shareability: str = "owner_private",
):
    cursor = store.event_cursor("phase-b")
    if cursor is None:
        cursor = store.initialize_event_cursor(
            "phase-b", 0, bootstrap_mode="replay",
        )
    seq = int(cursor) + 1
    subject = "person-owner"
    viewer_scope = {
        "owner_private": "owner",
        "public": "public",
    }[shareability]
    applied = store.apply_event(
        consumer_id="phase-b",
        event_seq=seq,
        event_id=event_id,
        event_type="service.degraded",
        material_digest=f"material:{event_id}",
        projection={
            "operation": "upsert",
            "kind": "maintenance",
            "summary": f"inspect {event_id}",
            "salience": 0.8,
            "dedup_key": f"service:{event_id}",
            "sources": [f"journal:{seq}:{event_id}"],
            "subject_person_id": subject,
            "viewer_scope": viewer_scope,
            "shareability": shareability,
            "occurred_at": NOW.isoformat(),
            "producer_name": "event_concerns",
            "producer_mode": producer_mode,
            "producer_revision": "event-concern-reducer:test",
        },
    )
    return store.get(applied["concern_id"])


class _Queue:
    def __init__(self):
        self.posts = []
        self.jobs = {}

    async def get_job(self, job_id):
        return self.jobs.get(job_id)

    async def post(self, job):
        self.posts.append(job)
        self.jobs[job.job_id] = job


class _ThoughtAdapter:
    def __init__(self):
        self.jobs = []

    async def ensure_posted(self, thought):
        self.jobs.append(thought)
        return thought.thought_job_id


def _spine(tmp_path, concerns, *, runtime):
    return CognitionSpine(
        concern_store=concerns,
        cognition_store=CognitionSpineStore(str(tmp_path / "cognition.db")),
        project_engine=SimpleNamespace(store=SimpleNamespace()),
        thought_queue=_ThoughtAdapter(),
        directive_manager=None,
        charter_validator=lambda *_args: (True, "test"),
        situation_validator=lambda *_args: (True, "test"),
        available_capabilities={"reasoning"},
        enforce_runtime_contract=True,
        runtime_contract_provider=lambda: runtime,
        revision_provider=lambda: {
            "policy_revision": "policy:test:v1",
            "situation_revision": "situation:test:v1",
        },
    )


def test_mode_lattice_holds_only_authority_widening_combinations():
    assert _runtime().effective_mode == "live"
    assert _runtime(workspace_mode="shadow").effective_mode == "held"
    assert _runtime(
        drive_governance_mode="live",
        charter_store_attached=True,
        charter_revision_id=None,
    ).effective_mode == "held"
    live = _runtime(
        drive_governance_mode="live",
        charter_store_attached=True,
        charter_revision_id="charter:default:owner-ratified",
    )
    assert live.effective_mode == "live"
    # Shadow P7 and shadow event reduction remain observational. Actual
    # concern provenance is checked per record instead of stopping daily work.
    assert "shadow_drive_governance_cannot_constrain_live_p3" not in live.blockers


@pytest.mark.asyncio
async def test_live_p3_holds_shadow_concern_until_exact_material_promotion(
    tmp_path,
):
    concerns = ConcernStore(str(tmp_path / "workspace.db"))
    item = _event_concern(
        concerns, event_id="event-shadow", producer_mode="shadow",
    )
    spine = _spine(tmp_path, concerns, runtime=_runtime())

    held = await spine.process_concern(item.concern_id, now=NOW)
    again = await spine.process_concern(
        item.concern_id, now=NOW + timedelta(seconds=1),
    )
    assert held["status"] == "cognition_held"
    assert held["reason"] == "concern_provenance_requires_promotion"
    assert again["status"] == "cognition_backoff"
    assert spine.thought_queue.jobs == []

    promoted = concerns.promote_concern(
        item.concern_id,
        expected_material_digest=item.last_material_digest,
        promotion_ref="owner-promotion:material:event-shadow",
        now=NOW + timedelta(seconds=2),
    )
    assert promoted.producer_mode == "shadow"
    assert promoted.promotion_ref.startswith("owner-promotion:")

    queued = await spine.process_concern(
        item.concern_id, now=NOW + timedelta(seconds=2),
    )
    assert queued["status"] == "thought_queued"
    assert len(spine.thought_queue.jobs) == 1


def test_admission_backoff_is_bounded_and_revision_keyed_across_restart(
    tmp_path,
):
    path = tmp_path / "cognition.db"
    store = CognitionSpineStore(str(path))
    context = {
        "concern_id": "concern-1",
        "material_digest": "material-1",
        "runtime_revision": "runtime-1",
        "policy_revision": "policy-1",
        "situation_revision": "situation-1",
        "charter_revision_id": "charter-1",
        "producer_revision": "producer-1",
        "promotion_ref": "",
    }
    first = store.record_admission(
        **context, state="held", reason="dependency_held", now=NOW,
    )
    early = store.record_admission(
        **context,
        state="held",
        reason="dependency_held",
        now=NOW + timedelta(seconds=1),
    )
    assert first["attempt_count"] == early["attempt_count"] == 1
    assert early["state"] == "backoff"

    reopened = CognitionSpineStore(str(path))
    retry = reopened.record_admission(
        **context,
        state="held",
        reason="dependency_held",
        now=NOW + timedelta(seconds=6),
    )
    changed = reopened.record_admission(
        **{**context, "situation_revision": "situation-2"},
        state="held",
        reason="dependency_held",
        now=NOW + timedelta(seconds=6),
    )
    assert retry["attempt_count"] == 2
    assert changed["attempt_count"] == 1
    assert retry["retry_delay_seconds"] <= 300
    assert retry["admission_ref"] != changed["admission_ref"]


def test_non_action_outputs_route_durably_and_never_execute(tmp_path):
    path = tmp_path / "cognition.db"
    store = CognitionSpineStore(str(path))
    for kind, payload in (
        ("Note", {"note": "observe one more sample"}),
        ("MemoryWriteProposal", {"content": "candidate fact"}),
        (
            "ExperimentProposal",
            {"hypothesis": "probe improves signal", "metric": "health", "variant": "v2"},
        ),
    ):
        output = ThoughtOutputV1(
            kind=kind,
            thought_job_id=f"thought-{kind}",
            thought_job_digest=f"digest-{kind}",
            evidence_refs=("event:test",),
            confidence=0.8,
            payload={"kind": kind, **payload},
            result_ref=f"thought-result:{kind}",
        )
        scope = {
            "concern_id": "concern-1",
            "subject_person_id": "person-owner",
            "viewer_scope": "owner",
            "shareability": "owner_private",
        }
        first = store.route_output(output, **scope)
        replay = store.route_output(output, **scope)
        assert first == replay
        assert first["effect_executed"] is False
        assert first["state"] in {"delivered", "pending"}

    reopened = CognitionSpineStore(str(path))
    rows = reopened.routed_outputs(limit=10)
    assert {row["kind"] for row in rows} == {
        "Note", "MemoryWriteProposal", "ExperimentProposal",
    }
    assert all(row["effect_executed"] is False for row in rows)


def test_proposal_sink_is_scoped_shadow_only_and_idempotent(tmp_path):
    cognition = CognitionSpineStore(str(tmp_path / "cognition.db"))
    proposals = ProposalStore(str(tmp_path / "proposals.db"))
    output = ThoughtOutputV1(
        kind="MemoryWriteProposal",
        thought_job_id="thought-memory-proposal",
        thought_job_digest="digest-memory-proposal",
        evidence_refs=("event:memory",),
        confidence=0.8,
        payload={
            "kind": "MemoryWriteProposal",
            "content": "candidate fact; no graph mutation",
            "confidence": 0.8,
        },
        result_ref="thought-result:memory-proposal",
    )
    route = cognition.route_output(
        output,
        concern_id="concern-memory",
        subject_person_id="person-owner",
        viewer_scope="owner",
        shareability="owner_private",
    )
    sink = ThoughtProposalPresentationSink(proposals)

    first = sink.put_if_absent(route)
    replay = sink.put_if_absent(route)
    mirrored = proposals.get(first)

    assert first == replay
    assert proposals.count() == 1
    assert mirrored.status == "shadow"
    assert mirrored.route_ref == route["route_ref"]
    assert mirrored.result_ref == output.result_ref
    assert mirrored.subject_person_id == "person-owner"
    assert mirrored.shareability == "owner_private"
    assert route["state"] == "pending"
    assert route["effect_executed"] is False


class _NonstrictThoughtHandler(JobHandler):
    async def execute(self, job):
        return {"job_id": job.job_id}


def test_registry_and_worker_require_the_strict_thought_handler():
    router = SimpleNamespace()
    handlers = build_default_handlers(router=router)
    assert isinstance(handlers[JobType.INFERENCE], InferenceHandler)
    assert isinstance(handlers[JobType.THOUGHT], ThoughtOnlyInferenceHandler)
    assert handlers[JobType.THOUGHT] is not handlers[JobType.INFERENCE]

    with pytest.raises(ValueError, match="strict thought-only handler"):
        WorkerNode(
            node_id="thought-node",
            queue=SimpleNamespace(),
            handlers={JobType.THOUGHT: _NonstrictThoughtHandler()},
        )


def test_server_cognition_owner_has_only_the_private_thought_lane():
    handlers, capabilities = _cognition_owner_spec(
        router=SimpleNamespace(), node_id="thought-owner",
    )
    assert set(handlers) == {JobType.THOUGHT}
    assert isinstance(handlers[JobType.THOUGHT], ThoughtOnlyInferenceHandler)
    assert capabilities.job_types == {JobType.THOUGHT}
    assert capabilities.capabilities == {
        "cognition_scoped", "thought_engine:v1",
    }
    assert not {
        JobType.CUSTOM,
        JobType.INFERENCE,
        JobType.MONITORING,
        JobType.SYSTEM_MAINTENANCE,
    }.intersection(handlers)


def test_configured_cognition_attachment_failure_never_selects_generic_worker():
    assert _cognition_worker_profile(
        configured_mode="live", attached=False,
    ) == "held"
    assert _cognition_worker_profile(
        configured_mode="shadow", attached=False,
    ) == "held"
    assert _cognition_worker_profile(
        configured_mode="live", attached=True,
    ) == "thought_only"
    assert _cognition_worker_profile(
        configured_mode="off", attached=False,
    ) == "generic"


@pytest.mark.asyncio
async def test_self_directed_shadow_provenance_is_not_laundered_by_live_workspace(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_WORKSPACE", "live")
    concerns = ConcernStore(str(tmp_path / "workspace.db"))
    workspace = SimpleNamespace()
    from colony_sidecar.self_model.workspace import WorkspaceEngine

    workspace = WorkspaceEngine(concerns)
    initiative = SimpleNamespace(
        dedup_key="thinking:test",
        description="Consider a bounded investigation",
        priority=0.7,
    )
    assert _record_p3_thinker_candidates(workspace, [initiative]) == 1
    item = concerns.active(limit=1)[0]
    assert item.producer_name == "self_directed_thinker"
    assert item.producer_mode == "shadow"
    assert item.producer_revision == "self-directed-thinker:v1"

    spine = _spine(tmp_path, concerns, runtime=_runtime())
    held = await spine.process_concern(item.concern_id, now=NOW)
    assert held["status"] == "cognition_held"
    assert held["reason"] == "concern_provenance_requires_promotion"


@pytest.mark.asyncio
async def test_thought_only_handler_refuses_generic_inference():
    handler = ThoughtOnlyInferenceHandler(SimpleNamespace())
    with pytest.raises(ValueError, match="refuses non-thought"):
        await handler.execute(Job(
            job_id="inference-1",
            job_type=JobType.INFERENCE,
            payload={"prompt": "do not run"},
        ))


def test_health_read_trace_filters_owner_private_rows_from_guest(tmp_path):
    concerns = ConcernStore(str(tmp_path / "workspace.db"))
    private = _event_concern(
        concerns, event_id="private", producer_mode="shadow",
    )
    public = _event_concern(
        concerns,
        event_id="public",
        producer_mode="shadow",
        shareability="public",
    )
    spine = _spine(tmp_path, concerns, runtime=_runtime())
    for item in (private, public):
        spine.store.record_admission(
            concern_id=item.concern_id,
            material_digest=item.last_material_digest,
            runtime_revision="runtime-1",
            policy_revision="policy-1",
            situation_revision="situation-1",
            charter_revision_id="",
            producer_revision=item.producer_revision,
            promotion_ref="",
            subject_person_id=item.subject_person_id,
            shareability=item.shareability,
            audience_scope=(
                ("global",) if item.shareability == "public"
                else ("owner",)
            ),
            state="held",
            reason="concern_provenance_requires_promotion",
            now=NOW,
        )

    owner = spine.health_snapshot(
        viewer_person_id="person-owner",
        owner_person_id="person-owner",
        audiences={"owner"},
    )
    guest = spine.health_snapshot(
        viewer_person_id="person-guest",
        owner_person_id="person-owner",
        audiences={"global"},
    )
    assert {row["concern_id"] for row in owner["read_trace"]} == {
        private.concern_id, public.concern_id,
    }
    assert {row["concern_id"] for row in guest["read_trace"]} == {
        public.concern_id,
    }


def test_live_health_requires_the_exact_thought_route_to_be_ready(tmp_path):
    concerns = ConcernStore(str(tmp_path / "workspace.db"))
    spine = _spine(tmp_path, concerns, runtime=_runtime())
    route = {"ready": False, "reason": "thought_handler_not_registered"}
    spine._worker_health_provider = lambda: {
        "ready": True,
        "reason": "ready",
        "typed_routes": {"thought": dict(route)},
    }

    held = spine.health_snapshot(
        viewer_person_id="person-owner",
        owner_person_id="person-owner",
        audiences={"owner"},
    )
    route.update({"ready": True, "node_id": "thought-node", "reason": "ready"})
    healthy = spine.health_snapshot(
        viewer_person_id="person-owner",
        owner_person_id="person-owner",
        audiences={"owner"},
    )

    assert held["healthy"] is False
    assert held["worker"]["reason"] == "thought_handler_not_registered"
    assert healthy["healthy"] is True
    assert healthy["worker"]["node_id"] == "thought-node"


def test_routed_output_visibility_uses_immutable_scope_not_mutated_concern(
    tmp_path,
):
    concerns = ConcernStore(str(tmp_path / "workspace.db"))
    private = _event_concern(
        concerns, event_id="private-route", producer_mode="live",
    )
    spine = _spine(tmp_path, concerns, runtime=_runtime())
    output = ThoughtOutputV1(
        kind="MemoryWriteProposal",
        thought_job_id="thought-private-route",
        thought_job_digest="digest-private-route",
        evidence_refs=("event:private-route",),
        confidence=0.8,
        payload={"kind": "MemoryWriteProposal", "content": "owner secret"},
        result_ref="thought-result:private-route",
    )
    spine.store.route_output(
        output,
        concern_id=private.concern_id,
        subject_person_id="person-owner",
        viewer_scope="owner",
        shareability="owner_private",
    )
    with concerns._lock:
        concerns._conn.execute(
            "UPDATE concerns SET viewer_scope='public',shareability='public' "
            "WHERE concern_id=?", (private.concern_id,),
        )
        concerns._conn.commit()

    guest = spine.health_snapshot(
        viewer_person_id="person-guest",
        owner_person_id="person-owner",
        audiences={"global"},
    )
    owner = spine.health_snapshot(
        viewer_person_id="person-owner",
        owner_person_id="person-owner",
        audiences={"owner"},
    )
    assert guest["routed_outputs"] == []
    assert owner["routed_outputs"][0]["payload"]["content"] == "owner secret"


def test_admission_visibility_uses_immutable_scope_not_mutated_concern(
    tmp_path,
):
    concerns = ConcernStore(str(tmp_path / "workspace.db"))
    private = _event_concern(
        concerns, event_id="private-admission", producer_mode="live",
    )
    spine = _spine(tmp_path, concerns, runtime=_runtime())
    spine._runtime_admission(
        private,
        material_digest=private.last_material_digest,
        now=NOW,
    )
    with concerns._lock:
        concerns._conn.execute(
            "UPDATE concerns SET viewer_scope='public',shareability='public' "
            "WHERE concern_id=?", (private.concern_id,),
        )
        concerns._conn.commit()

    guest = spine.health_snapshot(
        viewer_person_id="person-guest",
        owner_person_id="person-owner",
        audiences={"global"},
    )
    owner = spine.health_snapshot(
        viewer_person_id="person-owner",
        owner_person_id="person-owner",
        audiences={"owner"},
    )

    assert guest["read_trace"] == []
    assert len(owner["read_trace"]) == 1
    admission = owner["read_trace"][0]
    assert admission["subject_person_id"] == "person-owner"
    assert admission["viewer_person_id"] == "person-owner"
    assert admission["shareability"] == "owner_private"
    assert admission["scope_digest"]
    assert admission["scope_integrity"] == "verified"


def test_legacy_admission_scope_migration_is_fail_private(tmp_path):
    import sqlite3

    path = tmp_path / "legacy-cognition.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE cognition_admission_trace (
            admission_ref TEXT PRIMARY KEY,
            concern_id TEXT NOT NULL,
            material_digest TEXT NOT NULL,
            runtime_revision TEXT NOT NULL,
            policy_revision TEXT NOT NULL,
            situation_revision TEXT NOT NULL,
            charter_revision_id TEXT NOT NULL,
            producer_revision TEXT NOT NULL,
            promotion_ref TEXT NOT NULL,
            state TEXT NOT NULL,
            reason TEXT NOT NULL,
            attempt_count INTEGER NOT NULL,
            retry_not_before REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        INSERT INTO cognition_admission_trace VALUES (
            'legacy-admission', 'legacy-concern', 'material', 'runtime',
            'policy', 'situation', '', 'producer', '', 'held', 'legacy',
            1, 0, 1, 1
        );
        """
    )
    connection.commit()
    connection.close()

    store = CognitionSpineStore(str(path))
    row = store.admission_trace(limit=1)[0]
    assert row["shareability"] == "owner_private"
    assert row["audience_scope"] == ["owner"]
    assert row["scope_digest"]
    assert row["scope_integrity"] == "verified"

    concerns = ConcernStore(str(tmp_path / "legacy-workspace.db"))
    spine = _spine(tmp_path, concerns, runtime=_runtime())
    spine.store = store
    guest = spine.health_snapshot(
        viewer_person_id="person-guest",
        owner_person_id="person-owner",
        audiences={"global"},
    )
    owner = spine.health_snapshot(
        viewer_person_id="person-owner",
        owner_person_id="person-owner",
        audiences={"owner"},
    )
    assert guest["read_trace"] == []
    assert owner["read_trace"][0]["admission_ref"] == "legacy-admission"
