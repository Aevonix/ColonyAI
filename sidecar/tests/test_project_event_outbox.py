from datetime import datetime, timedelta, timezone
from dataclasses import replace
import sqlite3

import pytest

from colony_sidecar.execution_results import ExecutionResultV1
from colony_sidecar.events.journal import event_record_request_digest
from colony_sidecar.projects.event_outbox import ProjectEventProjector
from colony_sidecar.projects.models import Project, Step
from colony_sidecar.projects.store import ProjectStore
from colony_sidecar.work_orders import WorkOrderV1


def _verified_result(store: ProjectStore):
    project = Project(
        id="proj-evidence-outbox-1",
        title="Verify a durable result",
        objective="Produce receipt-backed evidence",
        source="cognition_spine",
        status="active",
        subject_person_id="owner-person-1",
        viewer_scope="owner",
        shareability="owner_private",
    )
    step = Step(
        id="step-evidence-outbox-1",
        project_id=project.id,
        ordinal=1,
        description="Verify the primary artifact",
        action_kind="research",
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
    result = ExecutionResultV1(
        result_ref=ExecutionResultV1.ref_for(order.work_order_id),
        work_order_id=order.work_order_id,
        work_order_digest=order.work_order_digest,
        work_order_version=order.version,
        run_id="run-evidence-outbox-1",
        attempt_number=1,
        terminal_outcome="succeeded",
        started_at="2026-07-13T00:00:00+00:00",
        ended_at="2026-07-13T00:00:01+00:00",
        executor_identity="host-action-executor",
        effect_class=order.effect_class,
        receipt_refs=("artifact:verified-primary-result",),
        verification_result="verified",
        verifier_identity="action-plane-receipt-verifier:v1",
        summary="verified result",
    )
    store.save_execution_result(order, result, transport_status="completed")
    return project, step, order, result


class KeyedJournal:
    def __init__(self):
        self.records = {}
        self.calls = 0
        self.acknowledged = []
        self.allow_ack = True

    def append(self, event_type, payload, *, occurred_at, event_key):
        self.calls += 1
        request = (event_type, payload, occurred_at)
        existing = self.records.get(event_key)
        if existing is not None:
            assert existing["request"] == request
            return dict(existing["record"])
        record = {
            "seq": len(self.records) + 1,
            "ulid": f"journal-event-{len(self.records) + 1}",
            "recordedAt": "2026-07-13T00:00:02+00:00",
            "retained": True,
        }
        self.records[event_key] = {"request": request, "record": record}
        return dict(record)

    def acknowledge(
        self,
        event_key,
        *,
        expected_seq,
        expected_event_id,
        expected_recorded_at,
        expected_request_digest,
    ):
        if not self.allow_ack:
            return False
        stored = self.records[event_key]
        event_type, payload, occurred_at = stored["request"]
        record = stored["record"]
        assert expected_seq == record["seq"]
        assert expected_event_id == record["ulid"]
        assert expected_recorded_at == record["recordedAt"]
        assert expected_request_digest == event_record_request_digest(
            event_type, payload, occurred_at,
        )
        self.acknowledged.append(event_key)
        return True


def test_execution_result_stages_exact_immutable_event_in_own_transaction(tmp_path):
    store = ProjectStore(str(tmp_path / "projects.db"))
    project, step, order, result = _verified_result(store)

    pending = store.pending_project_events()
    assert len(pending) == 1
    event = pending[0]
    assert event["event_type"] == "work_order.result"
    assert event["payload"]["schema"] == "ProjectExecutionEvidenceV2"
    assert event["payload"]["version"] == 2
    assert event["payload"]["project_id"] == project.id
    assert event["payload"]["step_id"] == step.id
    assert event["payload"]["work_order_id"] == order.work_order_id
    assert event["payload"]["result_ref"] == result.result_ref
    assert event["payload"]["status"] == "verified"
    assert event["payload"]["cognition_mode_at_stage"] in {
        "off", "shadow", "live",
    }
    assert event["payload"]["receipt_refs"] == [
        "artifact:verified-primary-result",
    ]
    assert len(event["payload"]["result_digest"]) == 64
    assert len(event["payload"]["evidence_digest"]) == 64


def test_execution_replay_preserves_first_stage_mode_across_cutover(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE", "live")
    store = ProjectStore(str(tmp_path / "projects.db"))
    _project, _step, order, result = _verified_result(store)

    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE", "off")
    store.save_execution_result(order, result, transport_status="completed")

    pending = store.pending_project_events()
    assert len(pending) == 1
    assert pending[0]["payload"]["cognition_mode_at_stage"] == "live"


def test_projector_is_exactly_once_across_journal_to_outbox_crash(tmp_path):
    store = ProjectStore(str(tmp_path / "projects.db"))
    _verified_result(store)
    journal = KeyedJournal()
    projector = ProjectEventProjector(
        store,
        journal_projector=journal.append,
        journal_acknowledger=journal.acknowledge,
    )
    real_complete = store.complete_project_event
    crashed = {"once": False}

    def crash_after_journal(event_key, record):
        if not crashed["once"]:
            crashed["once"] = True
            raise RuntimeError("injected crash after journal commit")
        return real_complete(event_key, record)

    store.complete_project_event = crash_after_journal
    first = projector.run_once()
    assert first["projected"] == 0
    assert first["failed"] == 1
    assert len(journal.records) == 1
    assert store.pending_project_events()

    store.complete_project_event = real_complete
    second = projector.run_once()
    assert second["projected"] == 1
    assert second["failed"] == 0
    assert len(journal.records) == 1
    assert journal.calls == 2
    assert not store.pending_project_events()
    event = next(iter(journal.records))
    assert journal.acknowledged == [event]
    assert store.project_event(event)["journal_acknowledged"] == 1
    assert len(store.project_event(event)["projection_receipt_digest"]) == 64


def test_acknowledgement_failure_never_reprojects_or_duplicates_event(tmp_path):
    store = ProjectStore(str(tmp_path / "projects.db"))
    _verified_result(store)
    journal = KeyedJournal()
    journal.allow_ack = False
    projector = ProjectEventProjector(
        store,
        journal_projector=journal.append,
        journal_acknowledger=journal.acknowledge,
    )

    first = projector.run_once()
    assert first["projected"] == 1
    assert first["failed"] == 0
    assert first["acknowledgement_failures"] == 1
    assert journal.calls == 1
    assert len(store.unacknowledged_project_events()) == 1

    journal.allow_ack = True
    restarted = ProjectEventProjector(
        store,
        journal_projector=journal.append,
        journal_acknowledger=journal.acknowledge,
    )
    second = restarted.run_once()
    assert second["projected"] == 0
    assert second["acknowledged"] == 1
    assert journal.calls == 1
    assert not store.unacknowledged_project_events()


def test_terminal_project_event_replay_is_stable_and_receipt_bound(tmp_path):
    store = ProjectStore(str(tmp_path / "projects.db"))
    project, step, _order, result = _verified_result(store)
    step.status = "done"
    step.result_ref = result.result_ref
    store.save_step(step)
    project.status = "completed"
    project.outcome = "succeeded"
    project.reason = "all_steps_done"

    store.save_project(project)
    store.save_project(project)
    pending = store.pending_project_events()
    terminal = [row for row in pending if row["event_type"] == "project.completed"]
    assert len(terminal) == 1
    assert terminal[0]["payload"]["schema"] == "ProjectTerminalEvidenceV2"
    assert terminal[0]["payload"]["version"] == 2
    assert terminal[0]["payload"]["evidence_status"] == "verified"
    assert terminal[0]["payload"]["cognition_mode_at_stage"] in {
        "off", "shadow", "live",
    }
    assert terminal[0]["payload"]["result_refs"] == [{
        "step_id": step.id,
        "result_ref": result.result_ref,
        "result_digest": terminal[0]["payload"]["result_refs"][0][
            "result_digest"
        ],
    }]
    assert len(terminal[0]["payload"]["project_digest"]) == 64


def test_terminal_replay_preserves_first_stage_mode_across_cutover(
    tmp_path, monkeypatch,
):
    store = ProjectStore(str(tmp_path / "projects.db"))
    project, step, _order, result = _verified_result(store)
    step.status = "done"
    step.result_ref = result.result_ref
    store.save_step(step)
    project.status = "completed"
    project.outcome = "succeeded"
    project.reason = "all_steps_done"

    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE", "live")
    store.save_project(project)
    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE", "shadow")
    store.save_project(project)

    terminal = [
        row for row in store.pending_project_events()
        if row["event_type"] == "project.completed"
    ]
    assert len(terminal) == 1
    assert terminal[0]["payload"]["cognition_mode_at_stage"] == "live"


def test_new_attempt_cannot_change_result_head_after_project_terminal(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_COGNITION_EVIDENCE", "live")
    store = ProjectStore(str(tmp_path / "projects.db"))
    project, step, order, result = _verified_result(store)
    step.status = "done"
    step.result_ref = result.result_ref
    store.save_step(step)
    project.status = "completed"
    project.outcome = "succeeded"
    project.reason = "all_steps_done"
    store.save_project(project)
    terminal_before = next(
        row for row in store.pending_project_events()
        if row["event_type"] == "project.completed"
    )["payload"]

    # Exact redelivery remains idempotent after terminalization.
    assert store.save_execution_result(
        order, result, transport_status="completed",
    ) == result.result_ref
    ended = datetime.fromisoformat(result.ended_at) + timedelta(seconds=1)
    late = replace(
        result,
        run_id="run-evidence-outbox-late-2",
        attempt_number=2,
        started_at=(ended - timedelta(milliseconds=500)).isoformat(),
        ended_at=ended.isoformat(),
    )

    with pytest.raises(ValueError, match="cannot mutate a terminal"):
        store.save_execution_result(order, late, transport_status="completed")

    assert len(store.execution_attempts_for(order.work_order_id)) == 1
    logical = store.get_execution_result(result.result_ref)
    assert logical["run_id"] == result.run_id
    terminal_after = next(
        row for row in store.pending_project_events()
        if row["event_type"] == "project.completed"
    )["payload"]
    assert terminal_after == terminal_before


def test_tampered_outbox_payload_is_not_published(tmp_path):
    store = ProjectStore(str(tmp_path / "projects.db"))
    _verified_result(store)
    event = store.pending_project_events()[0]
    store._conn.execute(
        "UPDATE project_event_outbox SET payload_json=? WHERE event_key=?",
        ('{"status":"verified"}', event["event_key"]),
    )
    store._conn.commit()
    journal = KeyedJournal()
    result = ProjectEventProjector(
        store,
        journal_projector=journal.append,
        journal_acknowledger=journal.acknowledge,
    ).run_once()

    assert result["projected"] == 0
    assert result["failed"] == 1
    assert journal.calls == 0
    assert store.pending_project_events()[0]["last_error"].endswith(
        "digest mismatch"
    )


def test_invalid_json_records_error_and_does_not_poison_later_drain(tmp_path):
    store = ProjectStore(str(tmp_path / "projects.db"))
    _verified_result(store)
    event = store.pending_project_events()[0]
    store._conn.execute(
        "UPDATE project_event_outbox SET payload_json=? WHERE event_key=?",
        ("{not-json", event["event_key"]),
    )
    store._conn.commit()
    journal = KeyedJournal()

    result = ProjectEventProjector(
        store,
        journal_projector=journal.append,
        journal_acknowledger=journal.acknowledge,
    ).run_once()

    assert result["projected"] == 0
    assert result["failed"] == 1
    assert journal.calls == 0
    assert "invalid project outbox JSON" in result["outbox"]["last_error"]


@pytest.mark.parametrize("field,mutate", [
    ("journal_seq", lambda row: int(row["journal_seq"]) + 1),
    ("journal_event_id", lambda row: f"{row['journal_event_id']}-tampered"),
    ("journal_recorded_at", lambda _row: "2026-07-13T00:00:03+00:00"),
    ("projection_receipt_digest", lambda _row: "0" * 64),
    ("projection_receipt_digest", lambda _row: None),
])
def test_corrupted_projection_receipt_is_not_acknowledged_or_released(
    tmp_path, monkeypatch, field, mutate,
):
    monkeypatch.setenv("COLONY_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("COLONY_EVENT_JOURNAL_DIR", str(tmp_path / "events"))
    monkeypatch.setenv("COLONY_EVENT_JOURNAL_RETENTION", "500")
    store = ProjectStore(str(tmp_path / "projects.db"))
    _verified_result(store)

    first = ProjectEventProjector(
        store,
        journal_acknowledger=lambda _key, **_receipt: False,
    ).run_once()
    assert first["projected"] == 1
    assert first["acknowledgement_failures"] == 1
    row = store.unacknowledged_project_events()[0]
    marker_dir = tmp_path / "events" / ".event-keys"
    markers_before = sorted(path.name for path in marker_dir.iterdir())
    assert len(markers_before) == 1

    store._conn.execute(
        f"UPDATE project_event_outbox SET {field}=? WHERE event_key=?",
        (mutate(row), row["event_key"]),
    )
    store._conn.commit()

    second = ProjectEventProjector(store).run_once()

    assert second["acknowledged"] == 0
    assert second["acknowledgement_failures"] == 1
    retained = store.project_event(row["event_key"])
    assert retained["journal_acknowledged"] == 0
    assert "projection receipt" in retained["last_error"]
    assert sorted(path.name for path in marker_dir.iterdir()) == markers_before
    assert second["outbox"]["acknowledgement_pending"] == 1
    assert second["outbox"]["last_error"]
    with pytest.raises(ValueError, match="projection receipt"):
        store.acknowledge_project_event(row["event_key"])


@pytest.mark.parametrize("field,value", [
    ("payload_json", '{"status":"verified"}'),
    ("event_type", "project.abandoned"),
    ("event_digest", "0" * 64),
])
def test_direct_acknowledgement_revalidates_the_staged_envelope(
    tmp_path, field, value,
):
    store = ProjectStore(str(tmp_path / "projects.db"))
    _verified_result(store)
    journal = KeyedJournal()
    journal.allow_ack = False
    first = ProjectEventProjector(
        store,
        journal_projector=journal.append,
        journal_acknowledger=journal.acknowledge,
    ).run_once()
    assert first["projected"] == 1
    row = store.unacknowledged_project_events()[0]
    store._conn.execute(
        f"UPDATE project_event_outbox SET {field}=? WHERE event_key=?",
        (value, row["event_key"]),
    )
    store._conn.commit()

    with pytest.raises(ValueError, match="outbox (payload|envelope)"):
        store.acknowledge_project_event(row["event_key"])
    assert store.project_event(row["event_key"])["journal_acknowledged"] == 0


def test_projector_rejects_legacy_key_only_acknowledger(tmp_path):
    store = ProjectStore(str(tmp_path / "projects.db"))

    with pytest.raises(TypeError, match="exact projection receipt fields"):
        ProjectEventProjector(
            store,
            journal_acknowledger=lambda _key: True,
        )


@pytest.mark.parametrize("field,value", [
    ("event_type", "project.abandoned"),
    ("occurred_at", "2030-01-01T00:00:00+00:00"),
    ("event_key", "changed-project-event-key"),
])
def test_outbox_envelope_tamper_is_not_published(tmp_path, field, value):
    store = ProjectStore(str(tmp_path / "projects.db"))
    _verified_result(store)
    event = store.pending_project_events()[0]
    store._conn.execute(
        f"UPDATE project_event_outbox SET {field}=? WHERE event_key=?",
        (value, event["event_key"]),
    )
    store._conn.commit()
    journal = KeyedJournal()

    result = ProjectEventProjector(
        store,
        journal_projector=journal.append,
        journal_acknowledger=journal.acknowledge,
    ).run_once()

    assert result["projected"] == 0
    assert result["failed"] == 1
    assert journal.calls == 0
    assert result["outbox"]["last_error"].endswith(
        "envelope digest mismatch"
    )


def test_existing_outbox_schema_adds_ack_column_without_losing_rows(tmp_path):
    path = tmp_path / "projects.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE project_event_outbox (
               event_key TEXT PRIMARY KEY,event_type TEXT NOT NULL,
               event_digest TEXT NOT NULL,payload_json TEXT NOT NULL,
               occurred_at TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
               journal_seq INTEGER,journal_event_id TEXT,
               journal_recorded_at TEXT,projection_attempts INTEGER NOT NULL DEFAULT 0,
               last_error TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL
           )"""
    )
    conn.execute(
        """INSERT INTO project_event_outbox
           (event_key,event_type,event_digest,payload_json,occurred_at,state,
            created_at,updated_at) VALUES (?,?,?,?,?,'pending',1,1)""",
        (
            "legacy-event", "work_order.result", "a" * 64, "{}",
            "2026-07-13T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    store = ProjectStore(str(path))
    columns = {
        row[1] for row in store._conn.execute(
            "PRAGMA table_info(project_event_outbox)"
        ).fetchall()
    }
    assert "journal_acknowledged" in columns
    assert "projection_receipt_digest" in columns
    assert store.project_event("legacy-event")["journal_acknowledged"] == 0


def test_status_counts_more_than_500_pending_acknowledgements(tmp_path):
    store = ProjectStore(str(tmp_path / "projects.db"))
    rows = [
        (
            f"event-{index}", "work_order.result", "a" * 64, "{}",
            "2026-07-13T00:00:00+00:00", "projected", index + 1,
            f"journal-{index}", "2026-07-13T00:00:01+00:00", 0, 1.0, 1.0,
        )
        for index in range(725)
    ]
    store._conn.executemany(
        """INSERT INTO project_event_outbox
           (event_key,event_type,event_digest,payload_json,occurred_at,state,
            journal_seq,journal_event_id,journal_recorded_at,
            journal_acknowledged,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    store._conn.commit()

    assert store.project_event_outbox_status()["acknowledgement_pending"] == 725


def test_project_cannot_claim_another_projects_verified_result(tmp_path):
    store = ProjectStore(str(tmp_path / "projects.db"))
    _first, _first_step, _order, result = _verified_result(store)
    second = Project(
        id="proj-evidence-outbox-2",
        title="Separate project",
        objective="Must own its own result",
        source="cognition_spine",
        status="active",
        subject_person_id="owner-person-1",
        viewer_scope="owner",
        shareability="owner_private",
    )
    second_step = Step(
        id="step-evidence-outbox-2",
        project_id=second.id,
        ordinal=1,
        description="Do separate work",
        action_kind="research",
        status="done",
        result_ref=result.result_ref,
    )
    store.save_project(second)
    store.save_step(second_step)
    second.status = "completed"
    second.outcome = "succeeded"
    second.reason = "all_steps_done"

    store.save_project(second)

    terminal = next(
        row for row in store.pending_project_events()
        if row["event_type"] == "project.completed"
        and row["payload"]["project_id"] == second.id
    )
    assert terminal["payload"]["evidence_status"] == "unverified"
    assert terminal["payload"]["status"] == "unverified"


def test_result_store_revalidates_order_and_independent_receipt_claims(tmp_path):
    store = ProjectStore(str(tmp_path / "projects.db"))
    project, _step, order, result = _verified_result(store)
    second_store = ProjectStore(str(tmp_path / "separate-projects.db"))
    second_store.save_project(project)
    second_store.save_step(_step)
    second_store.save_work_order(order)

    with pytest.raises(ValueError, match="independent receipt evidence"):
        second_store.save_execution_result(
            order,
            replace(result, receipt_refs=()),
            transport_status="completed",
        )
    with pytest.raises(ValueError, match="independent receipt evidence"):
        second_store.save_execution_result(
            order,
            replace(
                result,
                verifier_identity=result.executor_identity,
            ),
            transport_status="completed",
        )
    with pytest.raises(Exception, match="authority digest mismatch"):
        second_store.save_execution_result(
            replace(order, project_id="different-project"),
            result,
            transport_status="completed",
        )

    assert second_store.get_execution_result(result.result_ref) is None
    assert second_store.pending_project_events() == []
