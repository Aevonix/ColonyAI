from copy import deepcopy
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import hashlib
import json
import sqlite3
import time

import pytest

from colony_sidecar.cognition.evidence_pipeline import (
    CognitionEvidenceReducer,
    CognitionEvidenceStore,
    project_evidence_event,
)
from colony_sidecar.events.journal import append_event_record, _checksum
from colony_sidecar.execution_results import ExecutionResultV1, bounded_refs
from colony_sidecar.projects.event_outbox import ProjectEventProjector
from colony_sidecar.projects.engine import ProjectEngine
from colony_sidecar.projects.models import Project, Step
from colony_sidecar.projects.store import ProjectStore
from colony_sidecar.self_model.expectations import ExpectationEngine, ExpectationStore
from colony_sidecar.self_model.store import CompetenceStore, SelfModel
from colony_sidecar.work_orders import WorkOrderV1


def _isolate(monkeypatch, tmp_path, *, mode="live"):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("COLONY_EVENT_JOURNAL_DIR", str(tmp_path / "events"))
    monkeypatch.setenv("COLONY_EVENT_JOURNAL_RETENTION", "500")
    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE", mode)
    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE_BOOTSTRAP", "beginning")


def _project_order(
    store: ProjectStore,
    *,
    project_id="proj-cognition-evidence-1",
    step_id="step-cognition-evidence-1",
):
    project = Project(
        id=project_id,
        title="Close the evidence loop",
        objective="Produce a locally verified artifact",
        source="cognition_spine",
        status="active",
        concern_id="concern-cognition-evidence-1",
        goal_proposal_id="goal-proposal:cognition-evidence-1",
        subject_person_id="owner-person-1",
        viewer_scope="owner",
        shareability="owner_private",
    )
    step = Step(
        id=step_id,
        project_id=project.id,
        ordinal=1,
        description="Verify the durable artifact",
        action_kind="research",
        confidence=0.73,
        work_order_issued_at=datetime(
            2026, 7, 13, tzinfo=timezone.utc,
        ).timestamp(),
    )
    store.save_project(project)
    store.save_step(step)
    order = WorkOrderV1.for_project_step(
        project,
        step,
        now=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )
    store.save_work_order(order)
    return project, step, order


def _save_result(
    store: ProjectStore,
    order: WorkOrderV1,
    *,
    terminal="succeeded",
    verification="verified",
    receipts=("artifact:server-verified-result",),
    error="",
    run_id="run-cognition-evidence-1",
):
    ended = datetime.now(timezone.utc)
    started = ended - timedelta(seconds=2)
    result = ExecutionResultV1(
        result_ref=ExecutionResultV1.ref_for(order.work_order_id),
        work_order_id=order.work_order_id,
        work_order_digest=order.work_order_digest,
        work_order_version=order.version,
        run_id=run_id,
        attempt_number=1,
        terminal_outcome=terminal,
        started_at=started.isoformat(),
        ended_at=ended.isoformat(),
        executor_identity="host-action-executor",
        effect_class=order.effect_class,
        receipt_refs=tuple(receipts),
        verification_result=verification,
        verifier_identity=(
            "action-plane-receipt-verifier:v1"
            if verification == "verified" else ""
        ),
        summary="bounded result",
        error=error,
    )
    store.save_execution_result(order, result, transport_status="completed")
    return result


def _runtime(tmp_path, project_store, *, expectations=None, projector=None):
    competence = CompetenceStore(str(tmp_path / "competence.db"))
    self_model = SelfModel(competence)
    evidence_store = CognitionEvidenceStore(str(tmp_path / "evidence.db"))
    reducer = CognitionEvidenceReducer(
        evidence_store,
        project_store=project_store,
        self_model=self_model,
        expectations=expectations,
        project_event_projector=projector,
    )
    return competence, evidence_store, reducer


def _external_journal_record():
    occurred_at = "2026-07-13T00:00:00+00:00"
    scope = {
        "schema": "ExternalCognitionScopeV1",
        "version": 1,
        "subject_person_id": "owner-person-1",
        "viewer_person_id": "owner-person-1",
        "viewer_scope": "owner",
        "shareability": "owner_private",
        "audience_scope": ["owner"],
    }
    scope_digest = hashlib.sha256(json.dumps(
        scope, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")).hexdigest()
    return {
        "seq": 1,
        "ulid": "journal-external-action-0001",
        "type": "cognition.external.action_outcome",
        "recordedAt": "2026-07-13T00:00:01+00:00",
        "occurredAt": occurred_at,
        "data": {
            "schema": "ExternalCognitionJournalProjectionV2",
            "version": 2,
            "external_event_id": "external-action-outcome-0001",
            "external_event_digest": "a" * 64,
            "external_occurred_at": occurred_at,
            "kind": "action_outcome",
            "summary": "producer reports success",
            "attributes": {
                "action_id": "external-action-0001",
                "outcome": "succeeded",
            },
            "producer_principal_id": "external-observer-1",
            "producer_revision": "external-principal:" + "b" * 24,
            "subject_person_id": "owner-person-1",
            "viewer_person_id": "owner-person-1",
            "viewer_scope": "owner",
            "shareability": "owner_private",
            "audience_scope": ["owner"],
            "scope_digest": scope_digest,
            "boundary_attested": False,
            "evidence_status": "reported/unverified",
        },
    }


def test_verified_work_receipt_drives_competence_expectation_and_trace(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path)
    projects = ProjectStore(str(tmp_path / "projects.db"))
    project, step, order = _project_order(projects)
    expectation_store = ExpectationStore(str(tmp_path / "expectations.db"))
    expectations = ExpectationEngine(expectation_store)
    prediction = expectation_store.create_v2(
        subject=f"task:{step.id}",
        domain="task_duration",
        expectation="task completes within its estimate",
        confidence=0.73,
        horizon=time.time() + 3600,
        source="project-planner-v1",
        dedup_key=f"task:{step.id}",
        evidence_refs=(f"work-order:{order.work_order_id}",),
        source_kind="task_receipt",
        subject_person_id=project.subject_person_id,
        viewer_scope=project.viewer_scope,
        shareability=project.shareability,
        cohort="task_duration:project",
    )
    assert prediction is not None
    result = _save_result(projects, order)
    projector = ProjectEventProjector(projects)
    competence, evidence, reducer = _runtime(
        tmp_path,
        projects,
        expectations=expectations,
        projector=projector,
    )

    reduced = reducer.run_once()
    assert reduced["project_outbox"]["projected"] == 1
    assert reduced["dispositions"] == {"verified_execution_success": 1}
    events = competence.events("project")
    attempt_ref = projects.execution_attempts_for(order.work_order_id)[0][
        "attempt_ref"
    ]
    assert len(events) == 1
    assert events[0]["outcome"] == "success"
    assert events[0]["evidence_status"] == "verified"
    assert events[0]["source_ref"] == attempt_ref
    assert events[0]["outcome_contract"] == "ExecutionResultV1:1"
    resolved = expectation_store.get(prediction.prediction_id)
    assert resolved.outcome == "hit"
    trace = evidence.trace(project_id=project.id, viewer_scope="owner")
    assert len(trace) == 1
    assert trace[0]["authority_state"] == "verified"
    assert trace[0]["applied_sinks"] == [
        "competence:project",
        f"expectation:{prediction.prediction_id}",
    ]
    assert trace[0]["projection"]["work_order_id"] == order.work_order_id
    assert attempt_ref in trace[0]["projection"]["evidence_refs"]

    replay = reducer.run_once()
    assert replay["processed"] == 0
    assert len(competence.events("project")) == 1
    assert expectation_store.get(prediction.prediction_id).outcome == "hit"


def test_sink_crash_replay_is_idempotent_and_preserves_complete_sink_receipt(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path)
    projects = ProjectStore(str(tmp_path / "projects.db"))
    project, step, order = _project_order(projects)
    expectation_store = ExpectationStore(str(tmp_path / "expectations.db"))
    expectations = ExpectationEngine(expectation_store)
    prediction = expectation_store.create_v2(
        subject=f"task:{order.work_order_id}",
        domain="task_duration",
        expectation="work order completes",
        confidence=0.7,
        horizon=time.time() + 3600,
        source="project-planner-v1",
        dedup_key=f"task:{order.work_order_id}",
        evidence_refs=(f"work-order:{order.work_order_id}",),
        source_kind="task_receipt",
        subject_person_id=project.subject_person_id,
        viewer_scope=project.viewer_scope,
        shareability=project.shareability,
        cohort="task_duration:work-order",
    )
    _save_result(projects, order)
    ProjectEventProjector(projects).run_once()
    competence, evidence, reducer = _runtime(
        tmp_path, projects, expectations=expectations,
    )
    real_apply = evidence.apply
    crashed = {"once": False}

    def fail_after_sinks(**kwargs):
        if not crashed["once"]:
            crashed["once"] = True
            raise RuntimeError("injected evidence-ledger commit crash")
        return real_apply(**kwargs)

    evidence.apply = fail_after_sinks
    first = reducer.run_once()
    assert first["processed"] == 0
    assert "commit crash" in first["error"]
    assert len(competence.events("project")) == 1
    assert expectation_store.get(prediction.prediction_id).outcome == "hit"
    assert evidence.cursor(reducer.consumer_id) == 0

    evidence.apply = real_apply
    second = reducer.run_once()
    assert second["processed"] == 1
    assert len(competence.events("project")) == 1
    trace = evidence.trace(project_id=project.id, viewer_scope="owner")
    assert trace[0]["applied_sinks"] == [
        "competence:project",
        f"expectation:{prediction.prediction_id}",
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda raw: raw["data"].update(
            producer_principal_id=" external-observer-1",
        ),
        lambda raw: raw.update(seq="1"),
        lambda raw: raw.update(type=" cognition.external.action_outcome"),
        lambda raw: raw.update(ulid=" journal-external-action-0001"),
        lambda raw: raw.update(occurredAt="2026-07-13T00:00:00Z"),
    ],
)
def test_external_evidence_uses_exact_shared_envelope_and_projection_validator(
    tmp_path, monkeypatch, mutation,
):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "owner-person-1")
    raw = _external_journal_record()
    mutation(raw)

    with pytest.raises(ValueError):
        project_evidence_event(raw, ProjectStore(str(tmp_path / "projects.db")))


@pytest.mark.parametrize(
    ("length", "accepted"),
    [(128, True), (129, False), (192, False), (256, False)],
)
def test_external_evidence_v2_envelope_has_exact_host_event_id_bound(
    tmp_path, monkeypatch, length, accepted,
):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "owner-person-1")
    raw = _external_journal_record()
    raw["ulid"] = "j" * length
    projects = ProjectStore(str(tmp_path / "projects.db"))

    if accepted:
        projection, reason, _digest_value = project_evidence_event(raw, projects)
        assert projection is not None
        assert reason == ""
    else:
        with pytest.raises(ValueError, match="journal ID is not canonical"):
            project_evidence_event(raw, projects)


def test_external_evidence_cannot_recompute_an_attacker_into_owner_lane(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "owner-person-1")
    raw = _external_journal_record()
    forged = deepcopy(raw["data"])
    forged.update(
        subject_person_id="person-attacker",
        viewer_person_id="person-attacker",
    )
    scope = {
        "schema": "ExternalCognitionScopeV1",
        "version": 1,
        "subject_person_id": forged["subject_person_id"],
        "viewer_person_id": forged["viewer_person_id"],
        "viewer_scope": forged["viewer_scope"],
        "shareability": forged["shareability"],
        "audience_scope": forged["audience_scope"],
    }
    forged["scope_digest"] = hashlib.sha256(json.dumps(
        scope, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")).hexdigest()
    raw["data"] = forged

    with pytest.raises(ValueError, match="owner lane subject"):
        project_evidence_event(raw, ProjectStore(str(tmp_path / "projects.db")))


def test_external_success_report_remains_unverified_and_cannot_train(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "owner-person-1")
    occurred_at = "2026-07-13T00:00:00+00:00"
    projects = ProjectStore(str(tmp_path / "projects.db"))
    scope = {
        "schema": "ExternalCognitionScopeV1",
        "version": 1,
        "subject_person_id": "owner-person-1",
        "viewer_person_id": "owner-person-1",
        "viewer_scope": "owner",
        "shareability": "owner_private",
        "audience_scope": ["owner"],
    }
    scope_digest = hashlib.sha256(json.dumps(
        scope, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")).hexdigest()
    record = append_event_record(
        "cognition.external.action_outcome",
        {
            "schema": "ExternalCognitionJournalProjectionV2",
            "version": 2,
            "external_event_id": "external-action-outcome-0001",
            "external_event_digest": "a" * 64,
            "external_occurred_at": occurred_at,
            "kind": "action_outcome",
            "summary": "producer reports success",
            "attributes": {
                "action_id": "external-action-0001",
                "outcome": "succeeded",
            },
            "producer_principal_id": "external-observer-1",
            "producer_revision": "external-principal:" + "b" * 24,
            "subject_person_id": "owner-person-1",
            "viewer_person_id": "owner-person-1",
            "viewer_scope": "owner",
            "shareability": "owner_private",
            "audience_scope": ["owner"],
            "scope_digest": scope_digest,
            "boundary_attested": False,
            "evidence_status": "reported/unverified",
        },
        occurred_at=occurred_at,
        event_key="external-cognition:external-action-outcome-0001",
    )
    assert record is not None
    competence, evidence, reducer = _runtime(tmp_path, projects)

    reduced = reducer.run_once()
    assert reduced["dispositions"] == {"reported_external_observation": 1}
    assert competence.events("project") == []
    trace = evidence.trace(viewer_scope="owner")
    assert trace[0]["authority_state"] == "reported_unverified"
    assert trace[0]["applied_sinks"] == []


def test_forged_verified_journal_event_stops_before_learning(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path)
    projects = ProjectStore(str(tmp_path / "projects.db"))
    _project, _step, order = _project_order(projects)
    _save_result(projects, order)
    event = projects.pending_project_events()[0]
    forged = dict(event["payload"])
    forged["result_digest"] = "0" * 64
    record = append_event_record(
        "work_order.result",
        forged,
        occurred_at=event["occurred_at"],
        event_key="forged-project-execution-event-0001",
    )
    assert record is not None
    competence, evidence, reducer = _runtime(tmp_path, projects)

    reduced = reducer.run_once()
    assert reduced["processed"] == 0
    assert "does not bind the event payload" in reduced["error"]
    assert competence.events("project") == []
    assert evidence.cursor(reducer.consumer_id) == 0


def test_copied_valid_payload_cannot_learn_from_a_second_journal_event(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path)
    projects = ProjectStore(str(tmp_path / "projects.db"))
    project, _step, order = _project_order(projects)
    _save_result(projects, order)
    projector = ProjectEventProjector(projects)
    assert projector.run_once()["projected"] == 1
    identity = hashlib.sha256(json.dumps(
        {
            "run_id": "run-cognition-evidence-1",
            "work_order_id": order.work_order_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")).hexdigest()
    outbox = projects.project_event(f"project-execution:{identity}")
    assert outbox is not None
    copied = append_event_record(
        outbox["event_type"],
        dict(outbox["payload"]),
        occurred_at=outbox["occurred_at"],
        event_key="forged-copy-of-valid-project-execution-0001",
    )
    assert copied is not None
    competence, evidence, reducer = _runtime(tmp_path, projects)

    reduced = reducer.run_once()

    assert reduced["processed"] == 1
    assert "differs from its outbox projection" in reduced["error"]
    assert evidence.cursor(reducer.consumer_id) == 1
    assert len(competence.events("project")) == 1
    assert len(evidence.trace(project_id=project.id, viewer_scope="owner")) == 1


@pytest.mark.parametrize("variant", ["noncanonical_type", "typed_payload"])
def test_outbox_receipt_join_requires_exact_raw_event_content(
    tmp_path, monkeypatch, variant,
):
    _isolate(monkeypatch, tmp_path)
    projects = ProjectStore(str(tmp_path / "projects.db"))
    _project, _step, order = _project_order(projects)
    _save_result(projects, order)
    assert ProjectEventProjector(projects).run_once()["projected"] == 1
    identity = hashlib.sha256(json.dumps(
        {
            "run_id": "run-cognition-evidence-1",
            "work_order_id": order.work_order_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")).hexdigest()
    outbox = projects.project_event(f"project-execution:{identity}")
    assert outbox is not None
    payload = dict(outbox["payload"])
    event_type = str(outbox["event_type"])
    if variant == "noncanonical_type":
        event_type = f" {event_type.upper()} "
    else:
        payload["attempt_number"] = True
        payload.pop("evidence_digest")
        payload["evidence_digest"] = hashlib.sha256(json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")).hexdigest()
    raw = {
        "seq": int(outbox["journal_seq"]),
        "ulid": str(outbox["journal_event_id"]),
        "type": event_type,
        "recordedAt": str(outbox["journal_recorded_at"]),
        "occurredAt": str(outbox["occurred_at"]),
        "data": payload,
    }

    def replay(*, after_seq, **_kwargs):
        return {
            "events": [raw] if after_seq < raw["seq"] else [],
            "firstAvailableSeq": 1,
            "journalLastSeq": raw["seq"],
            "hasMore": False,
            "corruptCount": 0,
        }

    competence, evidence, reducer = _runtime(tmp_path, projects)
    reducer._replay = replay
    reducer._current_sequence = lambda: raw["seq"]

    reduced = reducer.run_once()

    assert reduced["processed"] == 0
    assert "differs from its outbox projection" in reduced["error"]
    assert evidence.cursor(reducer.consumer_id) == 0
    assert competence.events("project") == []


def test_shadow_records_verified_projection_without_training(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path, mode="shadow")
    projects = ProjectStore(str(tmp_path / "projects.db"))
    project, _step, order = _project_order(projects)
    _save_result(projects, order)
    competence, evidence, reducer = _runtime(
        tmp_path, projects, projector=ProjectEventProjector(projects),
    )

    reduced = reducer.run_once()
    assert reduced["mode"] == "shadow"
    assert competence.events("project") == []
    trace = evidence.trace(project_id=project.id, viewer_scope="owner")
    assert trace[0]["authority_state"] == "verified"
    assert trace[0]["applied_sinks"] == []


def test_locally_bound_failure_demotes_evidence_without_claiming_success(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path)
    projects = ProjectStore(str(tmp_path / "projects.db"))
    _project, _step, order = _project_order(projects)
    _save_result(
        projects,
        order,
        terminal="failed",
        verification="not_applicable",
        receipts=(),
        error="executor failed before producing the artifact",
    )
    competence, evidence, reducer = _runtime(
        tmp_path, projects, projector=ProjectEventProjector(projects),
    )

    reduced = reducer.run_once()
    assert reduced["dispositions"] == {"verified_execution_failure": 1}
    events = competence.events("project")
    assert len(events) == 1
    assert events[0]["outcome"] == "failure"
    assert events[0]["evidence_status"] == "verified"
    trace = evidence.trace(viewer_scope="owner")
    assert trace[0]["authority_state"] == "verified"
    assert trace[0]["outcome"] == "failure"


def test_later_attempt_skips_an_expectation_already_sealed_by_prior_result(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path)
    projects = ProjectStore(str(tmp_path / "projects.db"))
    project, step, order = _project_order(projects)
    expectation_store = ExpectationStore(str(tmp_path / "expectations.db"))
    expectations = ExpectationEngine(expectation_store)
    prediction = expectation_store.create_v2(
        subject=f"task:{step.id}",
        domain="task_duration",
        expectation="task succeeds",
        confidence=0.7,
        horizon=time.time() + 3600,
        source="project-planner-v1",
        dedup_key=f"task:{step.id}",
        evidence_refs=(f"work-order:{order.work_order_id}",),
        source_kind="task_receipt",
        subject_person_id=project.subject_person_id,
        viewer_scope=project.viewer_scope,
        shareability=project.shareability,
        cohort="task_duration:project",
    )
    first = _save_result(projects, order)
    first_attempt_ref = projects.execution_attempts_for(order.work_order_id)[0][
        "attempt_ref"
    ]
    projector = ProjectEventProjector(projects)
    competence, evidence, reducer = _runtime(
        tmp_path, projects, expectations=expectations, projector=projector,
    )
    assert reducer.run_once()["processed"] == 1
    assert expectation_store.get(prediction.prediction_id).outcome == "hit"
    assert expectation_store.get(
        prediction.prediction_id
    ).outcome_evidence_refs == (first_attempt_ref,)

    ended = datetime.now(timezone.utc) + timedelta(milliseconds=20)
    second = ExecutionResultV1(
        **{
            **first.__dict__,
            "run_id": "run-cognition-evidence-2",
            "attempt_number": 2,
            "started_at": (ended - timedelta(seconds=1)).isoformat(),
            "ended_at": ended.isoformat(),
        }
    )
    projects.save_execution_result(order, second, transport_status="completed")
    reduced = reducer.run_once()

    assert reduced["processed"] == 1
    assert "error" not in reduced
    assert len(competence.events("project")) == 2
    assert expectation_store.get(prediction.prediction_id).outcome == "hit"
    assert expectation_store.get(
        prediction.prediction_id
    ).outcome_evidence_refs == (first_attempt_ref,)
    assert len(evidence.trace(project_id=project.id, viewer_scope="owner")) == 2


def test_replay_does_not_resolve_prediction_created_after_outcome(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path)
    projects = ProjectStore(str(tmp_path / "projects.db"))
    project, step, order = _project_order(projects)
    _save_result(projects, order)
    ProjectEventProjector(projects).run_once()

    expectation_store = ExpectationStore(str(tmp_path / "expectations.db"))
    expectations = ExpectationEngine(expectation_store)
    prediction = expectation_store.create_v2(
        subject=f"task:{step.id}",
        domain="task_duration",
        expectation="future prediction must not consume past evidence",
        confidence=0.6,
        horizon=time.time() + 3600,
        source="late-planner-v1",
        dedup_key=f"late:{step.id}",
        evidence_refs=(f"work-order:{order.work_order_id}",),
        source_kind="task_receipt",
        subject_person_id=project.subject_person_id,
        viewer_scope=project.viewer_scope,
        shareability=project.shareability,
        cohort="task_duration:late",
    )
    _competence, _evidence, reducer = _runtime(
        tmp_path, projects, expectations=expectations,
    )

    assert reducer.run_once()["processed"] == 1
    assert expectation_store.get(prediction.prediction_id).outcome == "pending"


def test_shadow_and_live_disable_the_legacy_competence_writer(
    tmp_path, monkeypatch,
):
    class RecordingModel:
        def __init__(self):
            self.calls = []

        def record(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    model = RecordingModel()
    engine = ProjectEngine(ProjectStore(), self_model=model)
    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE", "live")

    engine._record_outcome("success", 0.2, stated_confidence=0.8)
    assert model.calls == []

    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE", "shadow")
    engine._record_outcome("success", 0.2, stated_confidence=0.8)
    assert model.calls == []

    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE", "off")
    engine._record_outcome("success", 0.2, stated_confidence=0.8)
    assert len(model.calls) == 1


def test_existing_bounded_ids_run_ids_and_url_receipts_project_cleanly(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path)
    projects = ProjectStore(str(tmp_path / "projects.db"))
    project, _step, order = _project_order(
        projects,
        project_id="project id allowed by WorkOrderV1",
        step_id="step id allowed by WorkOrderV1",
    )
    receipt = "https://evidence.example/result?artifact=" + ("a" * 300)
    _save_result(
        projects,
        order,
        receipts=(receipt,),
        run_id="provider run id with spaces",
    )
    competence, evidence, reducer = _runtime(
        tmp_path, projects, projector=ProjectEventProjector(projects),
    )

    reduced = reducer.run_once()

    assert reduced["dispositions"] == {"verified_execution_success": 1}
    assert len(competence.events("project")) == 1
    trace = evidence.trace(project_id=project.id, viewer_scope="owner")
    assert receipt in trace[0]["projection"]["evidence_refs"]
    assert trace[0]["projection"]["local_evidence"]["run_id"] == \
        "provider run id with spaces"


def test_checksum_corruption_stops_before_cursor_or_learning(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    assert append_event_record("conversation.turn", {"summary": "first"})
    first = sorted((tmp_path / "events").glob("[0-9]*.json"))[0]
    damaged = json.loads(first.read_text(encoding="utf-8"))
    damaged["checksum"] = "0" * 64
    first.write_text(json.dumps(damaged), encoding="utf-8")
    assert append_event_record("conversation.turn", {"summary": "second"})
    projects = ProjectStore(str(tmp_path / "projects.db"))
    competence, evidence, reducer = _runtime(tmp_path, projects)

    result = reducer.run_once()

    assert result["error"] == "journal_corruption_detected:1"
    assert result["processed"] == 0
    assert evidence.cursor(reducer.consumer_id) == 0
    assert competence.events("project") == []
    assert reducer.status()["healthy"] is False


def test_journal_rewind_stops_before_cursor_or_learning(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path, mode="shadow")
    projects = ProjectStore(str(tmp_path / "projects.db"))
    evidence = CognitionEvidenceStore(str(tmp_path / "evidence.db"))
    evidence.initialize_cursor(
        "cognition-evidence-v1", 5, bootstrap_mode="beginning",
    )

    def replay(*, after_seq, **_kwargs):
        assert after_seq == 5
        return {
            "events": [], "firstAvailableSeq": 1,
            "journalLastSeq": 3, "corruptCount": 0, "hasMore": False,
        }

    reducer = CognitionEvidenceReducer(
        evidence,
        project_store=projects,
        replay_fn=replay,
        current_sequence_fn=lambda: 3,
    )

    stopped = reducer.run_once()

    assert stopped["error"] == "event_journal_rewind:5:3"
    assert stopped["processed"] == 0
    assert stopped["last_seq"] == 5
    assert evidence.cursor(reducer.consumer_id) == 5
    assert reducer.status()["healthy"] is False


def test_duplicate_sequence_beyond_limit_one_stops_before_learning(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path)
    assert append_event_record("conversation.turn", {"summary": "first"})
    assert append_event_record("conversation.turn", {"summary": "second"})
    first = sorted((tmp_path / "events").glob("[0-9]*.json"))[0]
    duplicate = json.loads(first.read_text(encoding="utf-8"))
    duplicate["ulid"] = "duplicate-sequence-identity"
    unsigned = {
        key: value for key, value in duplicate.items() if key != "checksum"
    }
    duplicate["checksum"] = _checksum(json.dumps(
        unsigned,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n")
    (tmp_path / "events" / "000001.duplicate-sequence-identity.json").write_text(
        json.dumps(duplicate) + "\n",
        encoding="utf-8",
    )
    projects = ProjectStore(str(tmp_path / "projects.db"))
    competence, evidence, reducer = _runtime(tmp_path, projects)

    result = reducer.run_once(limit=1)

    assert result["error"] == "journal_corruption_detected:1"
    assert result["processed"] == 0
    assert evidence.cursor(reducer.consumer_id) == 0
    assert competence.events("project") == []
    assert reducer.status()["healthy"] is False


def test_second_replay_error_after_gap_acknowledgement_stays_unhealthy(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE_GAP_POLICY", "acknowledge")
    projects = ProjectStore(str(tmp_path / "projects.db"))
    evidence = CognitionEvidenceStore(str(tmp_path / "evidence.db"))
    evidence.initialize_cursor(
        "cognition-evidence-v1", 3, bootstrap_mode="beginning",
    )

    def replay(*, after_seq, **_kwargs):
        if after_seq == 3:
            return {
                "events": [], "firstAvailableSeq": 8,
                "journalLastSeq": 8, "corruptCount": 0,
            }
        return {"replayError": "journal_unavailable", "events": []}

    reducer = CognitionEvidenceReducer(
        evidence,
        project_store=projects,
        replay_fn=replay,
        current_sequence_fn=lambda: 8,
    )
    result = reducer.run_once()

    assert result["error"] == "event_journal_unavailable"
    assert evidence.cursor(reducer.consumer_id) == 7
    assert reducer.status()["healthy"] is False
    assert reducer.status()["gaps"]["count"] == 1


def test_internal_sequence_gap_requires_durable_acknowledgement(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path, mode="shadow")
    events = [{
        "seq": sequence,
        "ulid": f"event-{sequence}",
        "type": "conversation.turn",
        "occurredAt": f"2026-07-13T00:00:0{sequence}+00:00",
        "recordedAt": f"2026-07-13T00:00:0{sequence}+00:00",
        "data": {"summary": f"event {sequence}"},
    } for sequence in (1, 3)]

    def replay(*, after_seq, limit, **_kwargs):
        selected = [event for event in events if event["seq"] > after_seq]
        return {
            "events": selected[:limit],
            "firstAvailableSeq": 1,
            "journalLastSeq": 3,
            "hasMore": len(selected) > limit,
            "corruptCount": 0,
        }

    projects = ProjectStore(str(tmp_path / "projects.db"))
    evidence = CognitionEvidenceStore(str(tmp_path / "evidence.db"))
    evidence.initialize_cursor(
        "cognition-evidence-v1", 0, bootstrap_mode="beginning",
    )
    reducer = CognitionEvidenceReducer(
        evidence,
        project_store=projects,
        replay_fn=replay,
        current_sequence_fn=lambda: 3,
    )

    stopped = reducer.run_once()
    assert stopped["error"] == "journal_sequence_gap:1:3"
    assert stopped["processed"] == 1
    assert evidence.cursor(reducer.consumer_id) == 1

    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE_GAP_POLICY", "acknowledge")
    resumed = reducer.run_once()
    assert resumed["processed"] == 1
    assert evidence.cursor(reducer.consumer_id) == 3
    assert reducer.status()["gaps"]["count"] == 1


def test_evidence_ledger_blocks_mutation_and_trace_rechecks_digest(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path, mode="shadow")
    projects = ProjectStore(str(tmp_path / "projects.db"))
    _project, _step, order = _project_order(projects)
    _save_result(projects, order)
    _competence, evidence, reducer = _runtime(
        tmp_path, projects, projector=ProjectEventProjector(projects),
    )
    assert reducer.run_once()["processed"] == 1

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        evidence._conn.execute(
            "UPDATE cognition_evidence_events SET outcome='failure' "
            "WHERE event_seq=1"
        )
    evidence._conn.rollback()
    evidence._conn.execute("DROP TRIGGER cognition_evidence_events_no_update")
    evidence._conn.execute(
        "UPDATE cognition_evidence_events SET applied_sinks_json='[\"forged\"]' "
        "WHERE event_seq=1"
    )
    evidence._conn.commit()

    with pytest.raises(ValueError, match="ledger integrity failure"):
        evidence.trace(viewer_scope="owner")


def test_canonical_hashed_receipt_reference_remains_projectable(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path)
    projects = ProjectStore(str(tmp_path / "projects.db"))
    project, _step, order = _project_order(projects)
    receipt = bounded_refs(({
        "ref": "artifact:verified-output",
        "sha256": "a" * 64,
    },))[0]
    _save_result(projects, order, receipts=(receipt,))
    competence, evidence, reducer = _runtime(
        tmp_path, projects, projector=ProjectEventProjector(projects),
    )

    assert reducer.run_once()["dispositions"] == {
        "verified_execution_success": 1,
    }
    assert len(competence.events("project")) == 1
    assert receipt in evidence.trace(
        project_id=project.id, viewer_scope="owner",
    )[0]["projection"]["evidence_refs"]


def test_off_passthrough_prevents_live_relearning_legacy_outcome(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path, mode="off")
    projects = ProjectStore(str(tmp_path / "projects.db"))
    _project, _step, order = _project_order(projects)
    _save_result(projects, order)
    competence, evidence, reducer = _runtime(
        tmp_path, projects, projector=ProjectEventProjector(projects),
    )
    assert competence.record("project", "success", source="legacy-direct")

    off = reducer.run_once()
    assert off["cursor"] == 1
    assert off["passthrough_events"] == 1
    assert reducer.status()["gaps"]["count"] == 0
    assert reducer.status()["passthrough"]["count"] == 1
    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE", "live")
    live = reducer.run_once()

    assert live["processed"] == 0
    events = competence.events("project")
    assert len(events) == 1
    assert events[0]["source"] == "legacy-direct"
    assert evidence.cursor(reducer.consumer_id) == 1


def test_off_staged_outcome_cannot_train_when_first_consumed_live(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path, mode="off")
    projects = ProjectStore(str(tmp_path / "projects.db"))
    project, step, order = _project_order(projects)
    expectation_store = ExpectationStore(str(tmp_path / "expectations.db"))
    expectations = ExpectationEngine(expectation_store)
    prediction = expectation_store.create_v2(
        subject=f"task:{step.id}",
        domain="task_completion",
        expectation="an off-mode result is not new reducer evidence",
        confidence=0.7,
        horizon=time.time() + 3600,
        source="project-planner-v1",
        dedup_key=f"off-stage:{step.id}",
        evidence_refs=(f"work-order:{order.work_order_id}",),
        source_kind="task_receipt",
        subject_person_id=project.subject_person_id,
        viewer_scope=project.viewer_scope,
        shareability=project.shareability,
        cohort="task_completion:off-stage",
    )
    _save_result(projects, order)
    assert ProjectEventProjector(projects).run_once()["projected"] == 1
    competence, evidence, reducer = _runtime(
        tmp_path, projects, expectations=expectations,
    )
    evidence.initialize_cursor(
        reducer.consumer_id, 0, bootstrap_mode="beginning",
    )

    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE", "live")
    reduced = reducer.run_once()

    assert reduced["processed"] == 1
    assert "error" not in reduced
    assert competence.events("project") == []
    assert expectation_store.get(prediction.prediction_id).outcome == "pending"
    trace = evidence.trace(project_id=project.id, viewer_scope="owner")
    assert trace[0]["projection"]["local_evidence"][
        "cognition_mode_at_stage"
    ] == "off"
    assert trace[0]["applied_sinks"] == []


def test_off_staged_terminal_cannot_settle_project_when_consumed_live(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path, mode="off")
    projects = ProjectStore(str(tmp_path / "projects.db"))
    project, step, order = _project_order(projects)
    result = _save_result(projects, order)
    expectation_store = ExpectationStore(str(tmp_path / "expectations.db"))
    expectations = ExpectationEngine(expectation_store)
    prediction = expectation_store.create_v2(
        subject=f"project:{project.id}",
        domain="task_completion",
        expectation="off-mode project completion stays with the old writer",
        confidence=0.7,
        horizon=time.time() + 3600,
        source="project-planner-v1",
        dedup_key=f"off-terminal:{project.id}",
        evidence_refs=(f"work-order:{order.work_order_id}",),
        source_kind="task_receipt",
        subject_person_id=project.subject_person_id,
        viewer_scope=project.viewer_scope,
        shareability=project.shareability,
        cohort="task_completion:off-terminal",
    )
    step = projects.steps_for(project.id)[0]
    step.status = "done"
    step.result_ref = result.result_ref
    projects.save_step(step)
    project.status = "completed"
    project.outcome = "succeeded"
    project.reason = "all_steps_done"
    projects.save_project(project)
    assert ProjectEventProjector(projects).run_once()["projected"] == 2
    competence, evidence, reducer = _runtime(
        tmp_path, projects, expectations=expectations,
    )
    evidence.initialize_cursor(
        reducer.consumer_id, 0, bootstrap_mode="beginning",
    )

    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE", "live")
    reduced = reducer.run_once()

    assert reduced["processed"] == 2
    assert "error" not in reduced
    assert competence.events("project") == []
    assert expectation_store.get(prediction.prediction_id).outcome == "pending"
    terminal_trace = next(
        row for row in evidence.trace(
            project_id=project.id, viewer_scope="owner",
        )
        if row["event_type"] == "project.completed"
    )
    assert terminal_trace["projection"]["local_evidence"][
        "cognition_mode_at_stage"
    ] == "off"
    assert terminal_trace["applied_sinks"] == []


def test_step_result_does_not_settle_project_expectation_before_terminal(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path)
    projects = ProjectStore(str(tmp_path / "projects.db"))
    project, first_step, first_order = _project_order(projects)
    second_step = Step(
        id="step-cognition-evidence-2",
        project_id=project.id,
        ordinal=2,
        description="Verify the second durable artifact",
        action_kind="research",
        confidence=0.75,
        work_order_issued_at=datetime(
            2026, 7, 13, tzinfo=timezone.utc,
        ).timestamp(),
    )
    projects.save_step(second_step)
    second_order = WorkOrderV1.for_project_step(
        project, second_step, now=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )
    projects.save_work_order(second_order)
    expectation_store = ExpectationStore(str(tmp_path / "expectations.db"))
    expectations = ExpectationEngine(expectation_store)
    prediction = expectation_store.create_v2(
        subject=f"project:{project.id}",
        domain="task_completion",
        expectation="the whole project completes",
        confidence=0.7,
        horizon=time.time() + 3600,
        source="project-planner-v1",
        dedup_key=f"project:{project.id}",
        evidence_refs=(f"work-order:{first_order.work_order_id}",),
        source_kind="task_receipt",
        subject_person_id=project.subject_person_id,
        viewer_scope=project.viewer_scope,
        shareability=project.shareability,
        cohort="task_completion:project",
    )
    first_result = _save_result(projects, first_order)
    competence, _evidence, reducer = _runtime(
        tmp_path,
        projects,
        expectations=expectations,
        projector=ProjectEventProjector(projects),
    )

    assert reducer.run_once()["processed"] == 1
    assert expectation_store.get(prediction.prediction_id).outcome == "pending"

    second_result = _save_result(
        projects, second_order, run_id="run-cognition-evidence-2",
    )
    stored_steps = {item.id: item for item in projects.steps_for(project.id)}
    first_step = stored_steps[first_step.id]
    second_step = stored_steps[second_step.id]
    first_step.status = "done"
    first_step.result_ref = first_result.result_ref
    second_step.status = "done"
    second_step.result_ref = second_result.result_ref
    projects.save_step(first_step)
    projects.save_step(second_step)
    project.status = "completed"
    project.outcome = "succeeded"
    project.reason = "all_steps_done"
    projects.save_project(project)

    reduced = reducer.run_once()
    assert "error" not in reduced, reduced
    assert reduced["processed"] == 2
    assert expectation_store.get(prediction.prediction_id).outcome == "hit"
    assert len(competence.events("project")) == 2


def test_retryable_failure_does_not_seal_task_expectation(
    tmp_path, monkeypatch,
):
    _isolate(monkeypatch, tmp_path)
    projects = ProjectStore(str(tmp_path / "projects.db"))
    project, step, order = _project_order(projects)
    expectation_store = ExpectationStore(str(tmp_path / "expectations.db"))
    expectations = ExpectationEngine(expectation_store)
    prediction = expectation_store.create_v2(
        subject=f"task:{order.work_order_id}",
        domain="task_completion",
        expectation="the logical task eventually succeeds",
        confidence=0.7,
        horizon=time.time() + 3600,
        source="project-planner-v1",
        dedup_key=f"retry:{order.work_order_id}",
        evidence_refs=(f"work-order:{order.work_order_id}",),
        source_kind="task_receipt",
        subject_person_id=project.subject_person_id,
        viewer_scope=project.viewer_scope,
        shareability=project.shareability,
        cohort="task_completion:retry",
    )
    first = _save_result(
        projects,
        order,
        terminal="failed",
        verification="not_applicable",
        receipts=(),
        error="transient failure",
    )
    competence, _evidence, reducer = _runtime(
        tmp_path,
        projects,
        expectations=expectations,
        projector=ProjectEventProjector(projects),
    )
    assert reducer.run_once()["processed"] == 1
    assert expectation_store.get(prediction.prediction_id).outcome == "pending"

    ended = datetime.now(timezone.utc) + timedelta(milliseconds=20)
    second = replace(
        first,
        run_id="run-cognition-evidence-2",
        attempt_number=2,
        terminal_outcome="succeeded",
        started_at=(ended - timedelta(seconds=1)).isoformat(),
        ended_at=ended.isoformat(),
        receipt_refs=("artifact:retry-success",),
        verification_result="verified",
        verifier_identity="action-plane-receipt-verifier:v1",
        error="",
    )
    projects.save_execution_result(order, second, transport_status="completed")

    assert reducer.run_once()["processed"] == 1
    assert expectation_store.get(prediction.prediction_id).outcome == "hit"
    assert [event["outcome"] for event in competence.events("project")] == [
        "success", "failure",
    ]
