from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from colony_sidecar.api.routers import host
from colony_sidecar.api.authority import required_scope
from colony_sidecar.cognition.evidence_pipeline import CognitionEvidenceStore
from colony_sidecar.execution_results import ExecutionResultV1
from colony_sidecar.projects.models import Project, Step
from colony_sidecar.projects.store import ProjectStore
from colony_sidecar.self_model.expectations import ExpectationEngine, ExpectationStore
from colony_sidecar.self_model.store import CompetenceStore, SelfModel
from colony_sidecar.server import _attach_cognition_evidence
from colony_sidecar.work_orders import WorkOrderV1


class FakeScheduler:
    def __init__(self, *, fail=False):
        self.callbacks = {}
        self.fail = fail

    def register(self, name, callback, interval_seconds, metadata=None):
        if self.fail:
            raise RuntimeError("scheduler registration failed")
        self.callbacks[name] = {
            "callback": callback,
            "interval_seconds": interval_seconds,
            "metadata": metadata or {},
        }
        return f"schedule:{name}"


@pytest.fixture(autouse=True)
def isolate_host(monkeypatch, tmp_path):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("COLONY_EVENT_JOURNAL_DIR", str(tmp_path / "events"))
    monkeypatch.setenv("COLONY_EVENT_JOURNAL_RETENTION", "500")
    originals = {
        name: getattr(host, name)
        for name in (
            "_cognition_evidence_store",
            "_cognition_evidence_reducer",
            "_project_event_projector",
            "_cognition_evidence_attachment_status",
        )
    }
    yield
    for name, value in originals.items():
        setattr(host, name, value)


def test_default_off_keeps_only_passthrough_cursor_and_drains_project_outbox(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv("COLONY_COGNITION_EVIDENCE", raising=False)
    projects = ProjectStore(str(tmp_path / "projects.db"))
    scheduler = FakeScheduler()

    wiring = _attach_cognition_evidence(
        state_dir=tmp_path,
        project_store=projects,
        scheduler=scheduler,
    )

    assert wiring["mode"] == "off"
    assert wiring["store"] is host._cognition_evidence_store
    assert wiring["reducer"] is host._cognition_evidence_reducer
    assert wiring["initial_status"]["enabled"] is False
    assert wiring["projector"] is host._project_event_projector
    assert "cognition_evidence_reduce" in scheduler.callbacks
    assert "project_event_outbox" not in scheduler.callbacks
    assert (tmp_path / "colony-cognition-evidence.db").exists()
    assert "project_event_outbox" in host.supported_capabilities()
    assert "cognition_evidence" not in host.supported_capabilities()
    assert required_scope(
        "GET", "/v1/host/cognition/evidence"
    ) == "cognition:read"
    wiring["store"].close()


def test_shadow_attaches_observer_without_competence_authority(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE", "shadow")
    projects = ProjectStore(str(tmp_path / "projects.db"))
    scheduler = FakeScheduler()

    wiring = _attach_cognition_evidence(
        state_dir=tmp_path,
        project_store=projects,
        self_model=None,
        scheduler=scheduler,
    )

    assert wiring["mode"] == "shadow"
    assert wiring["store"] is host._cognition_evidence_store
    assert wiring["reducer"] is host._cognition_evidence_reducer
    assert wiring["initial_status"]["enabled"] is True
    assert "cognition_evidence_reduce" in scheduler.callbacks
    assert "project_event_outbox" not in scheduler.callbacks
    assert (tmp_path / "colony-cognition-evidence.db").exists()
    assert "cognition_evidence" in host.supported_capabilities()
    wiring["store"].close()


def test_live_rejects_missing_canonical_self_model_before_db_creation(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE", "live")
    projects = ProjectStore(str(tmp_path / "projects.db"))

    with pytest.raises(RuntimeError, match="canonical SelfModel"):
        _attach_cognition_evidence(
            state_dir=tmp_path,
            project_store=projects,
            self_model=None,
            scheduler=FakeScheduler(),
        )

    assert host._cognition_evidence_store is None
    assert host._cognition_evidence_reducer is None
    assert host._project_event_projector is None
    assert not (tmp_path / "colony-cognition-evidence.db").exists()


def test_live_rejects_missing_periodic_scheduler(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE", "live")
    projects = ProjectStore(str(tmp_path / "projects.db"))
    competence = CompetenceStore(str(tmp_path / "competence.db"))

    with pytest.raises(RuntimeError, match="autonomy scheduler"):
        _attach_cognition_evidence(
            state_dir=tmp_path,
            project_store=projects,
            self_model=SelfModel(competence),
            scheduler=None,
        )

    assert host._project_event_projector is None
    assert not (tmp_path / "colony-cognition-evidence.db").exists()


def test_live_attaches_one_reducer_and_scheduler_failure_is_atomic(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE", "live")
    projects = ProjectStore(str(tmp_path / "projects.db"))
    competence = CompetenceStore(str(tmp_path / "competence.db"))
    model = SelfModel(competence)
    project = Project(
        id="project-atomic-attachment",
        title="Atomic attachment",
        objective="Do not learn before scheduler ownership exists",
        source="cognition_spine",
        status="active",
        subject_person_id="owner",
        viewer_scope="owner",
        shareability="owner_private",
    )
    step = Step(
        id="step-atomic-attachment",
        project_id=project.id,
        ordinal=1,
        description="Produce one verified receipt",
        action_kind="research",
        work_order_issued_at=datetime(
            2026, 7, 13, tzinfo=timezone.utc,
        ).timestamp(),
    )
    projects.save_project(project)
    projects.save_step(step)
    order = WorkOrderV1.for_project_step(
        project, step, now=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )
    projects.save_work_order(order)
    result = ExecutionResultV1(
        result_ref=ExecutionResultV1.ref_for(order.work_order_id),
        work_order_id=order.work_order_id,
        work_order_digest=order.work_order_digest,
        work_order_version=order.version,
        run_id="run-atomic-attachment",
        attempt_number=1,
        terminal_outcome="succeeded",
        started_at="2026-07-13T00:00:00+00:00",
        ended_at="2026-07-13T00:00:01+00:00",
        executor_identity="host-action-executor",
        effect_class=order.effect_class,
        receipt_refs=("artifact:atomic-attachment",),
        verification_result="verified",
        verifier_identity="action-plane-receipt-verifier:v1",
    )
    projects.save_execution_result(order, result, transport_status="completed")
    expectation_store = ExpectationStore(str(tmp_path / "expectations.db"))
    expectations = ExpectationEngine(expectation_store)
    prediction = expectation_store.create_v2(
        subject=f"task:{order.work_order_id}",
        domain="task_completion",
        expectation="task completes",
        confidence=0.7,
        horizon=datetime.now(timezone.utc).timestamp() + 3600,
        source="project-planner-v1",
        dedup_key=f"task:{order.work_order_id}",
        evidence_refs=(f"work-order:{order.work_order_id}",),
        source_kind="task_receipt",
        subject_person_id="owner",
        viewer_scope="owner",
        shareability="owner_private",
        cohort="task_completion:atomic",
    )

    with pytest.raises(RuntimeError, match="scheduler registration failed"):
        _attach_cognition_evidence(
            state_dir=tmp_path,
            project_store=projects,
            self_model=model,
            expectations=expectations,
            scheduler=FakeScheduler(fail=True),
        )

    assert host._cognition_evidence_store is None
    assert host._cognition_evidence_reducer is None
    assert host._project_event_projector is None
    assert host._cognition_evidence_attachment_status["state"] == "failed"
    assert competence.events("project") == []
    assert expectation_store.get(prediction.prediction_id).outcome == "pending"
    reopened = CognitionEvidenceStore(
        str(tmp_path / "colony-cognition-evidence.db")
    )
    assert reopened.cursor("cognition-evidence-v1") is None
    reopened.close()


@pytest.mark.asyncio
async def test_http_evidence_trace_uses_server_derived_viewer_scope(monkeypatch):
    calls = []

    class TraceStore:
        def trace(self, **kwargs):
            calls.append(kwargs)
            return [{"event_seq": 7, "viewer_scope": kwargs["viewer_scope"]}]

    class Reducer:
        def status(self):
            return {"mode": "shadow", "healthy": True}

    host.set_cognition_evidence(
        TraceStore(), Reducer(), SimpleNamespace(status=lambda: {}),
        {"configured_mode": "shadow", "state": "attached"},
    )
    authority = SimpleNamespace(
        legacy=False,
        viewer_person_id="person-7",
        audiences=frozenset(),
    )
    monkeypatch.setattr(host, "request_authority", lambda _request: authority)
    monkeypatch.setattr(
        host,
        "resolve_request_person",
        lambda _request, claimed_person_id=None: claimed_person_id or "person-7",
    )

    result = await host.get_cognition_evidence(
        SimpleNamespace(), person_id="person-7", project_id="project-7", limit=25,
    )

    assert result["available"] is True
    assert result["learning_available"] is True
    assert result["status"]["healthy"] is True
    assert calls == [{
        "project_id": "project-7",
        "subject_person_id": "person-7",
        "viewer_scope": "person:person-7",
        "limit": 25,
    }]
    assert result["trace"][0]["viewer_scope"] == "person:person-7"


@pytest.mark.asyncio
async def test_http_never_returns_unverifiable_evidence_trace(monkeypatch):
    class CorruptTraceStore:
        def trace(self, **_kwargs):
            raise ValueError("cognition evidence ledger integrity failure at 7")

    class Reducer:
        mode = "shadow"

        def status(self):
            return {"mode": "shadow", "healthy": True}

    host.set_cognition_evidence(
        CorruptTraceStore(), Reducer(), SimpleNamespace(status=lambda: {}),
        {"configured_mode": "shadow", "state": "attached"},
    )
    authority = SimpleNamespace(
        legacy=False,
        viewer_person_id="owner",
        audiences=frozenset(),
    )
    monkeypatch.setattr(host, "request_authority", lambda _request: authority)
    monkeypatch.setattr(
        host,
        "resolve_request_person",
        lambda _request, claimed_person_id=None: claimed_person_id or "owner",
    )

    result = await host.get_cognition_evidence(SimpleNamespace(), limit=25)

    assert result["trace"] == []
    assert result["status"]["healthy"] is False
    assert result["status"]["last_error"] == \
        "evidence_ledger_integrity_failed"
    assert "integrity failure" in result["trace_error"]
