"""P6 regressions for receipt-bound ExpectationV2 and calibration cohorts."""

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import time

import pytest

from colony_sidecar.self_model.expectations import (
    ExpectationEngine,
    ExpectationStore,
    OutcomeObservationV1,
)


def store(tmp_path):
    return ExpectationStore(str(tmp_path / "expectations.db"))


def create_v2(s, *, key="one", horizon=None, subject="owner", viewer="owner"):
    return s.create_v2(
        subject=f"task:{key}",
        domain="task_duration",
        expectation="task completes in its estimate",
        confidence=0.8,
        horizon=horizon or time.time() + 60,
        source="task-adapter-v2",
        dedup_key=f"task:{key}",
        evidence_refs=(f"task:{key}",),
        source_kind="task_receipt",
        subject_person_id=subject,
        viewer_scope=viewer,
        shareability=(
            "subject_private" if viewer.startswith("person:")
            else "owner_private"
        ),
        cohort="task_duration:task",
        detail={"task_id": key},
    )


def outcome(
    prediction,
    *,
    oid="eo-1",
    value=True,
    observed=None,
    refs=("receipt:task-1",),
    subject=None,
    viewer=None,
):
    return OutcomeObservationV1.create(
        observation_id=oid,
        prediction_id=prediction.prediction_id,
        value=value,
        observed_at=observed or time.time(),
        evidence_refs=refs,
        source_kind="task_receipt",
        subject_person_id=subject or prediction.subject_person_id,
        viewer_scope=viewer or prediction.viewer_scope,
        shareability=prediction.shareability,
    )


def test_additive_migration_reads_pre_v2_database(tmp_path):
    path = tmp_path / "expectations.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE predictions (
            prediction_id TEXT PRIMARY KEY, subject TEXT NOT NULL,
            domain TEXT NOT NULL, expectation TEXT NOT NULL,
            confidence REAL NOT NULL, horizon REAL NOT NULL, source TEXT,
            outcome TEXT DEFAULT 'pending', resolved_at REAL, detail TEXT,
            dedup_key TEXT, created_at REAL NOT NULL
        );
        INSERT INTO predictions VALUES
            ('p-old','x:1','legacy','old prediction',0.6,2000000000,'old',
             'pending',NULL,'{}','old-key',1700000000);
        """
    )
    conn.commit()
    conn.close()

    migrated = ExpectationStore(str(path))
    row = migrated.pending()[0]
    assert row.prediction_id == "p-old"
    assert row.schema_version == 1
    assert row.viewer_scope == "owner"
    columns = {
        item["name"] for item in migrated._conn.execute(
            "PRAGMA table_info(predictions)",
        ).fetchall()
    }
    assert {"evidence_refs", "resolution_digest", "cohort"} <= columns


def test_v2_requires_evidence_and_exact_scope(tmp_path):
    s = store(tmp_path)
    with pytest.raises(ValueError, match="durable evidence"):
        s.create_v2(
            subject="task:one", domain="task_duration", expectation="done",
            confidence=0.5, horizon=time.time() + 60, source="test",
            dedup_key="one", evidence_refs=(), source_kind="task_receipt",
            subject_person_id="owner", viewer_scope="owner",
            shareability="owner_private", cohort="task",
        )
    with pytest.raises(ValueError, match="exact subject viewer"):
        s.create_v2(
            subject="task:private", domain="task_duration", expectation="done",
            confidence=0.5, horizon=time.time() + 60, source="test",
            dedup_key="private", evidence_refs=("task:private",),
            source_kind="task_receipt", subject_person_id="person-a",
            viewer_scope="owner", shareability="subject_private", cohort="task",
        )


def test_prediction_creation_dedups_across_store_connections(tmp_path):
    path = tmp_path / "expectations.db"
    first = ExpectationStore(str(path))
    second = ExpectationStore(str(path))
    horizon = time.time() + 60
    with ThreadPoolExecutor(max_workers=2) as pool:
        created = list(pool.map(
            lambda target: create_v2(target, horizon=horizon),
            (first, second),
        ))
    assert sum(item is not None for item in created) == 1
    assert len(first.pending()) == 1


def test_outcome_observation_has_no_prose_resolution_channel():
    with pytest.raises(TypeError):
        OutcomeObservationV1.create(
            observation_id="eo-1", prediction_id="p-1", value=True,
            observed_at=time.time(), evidence_refs=("receipt:r1",),
            source_kind="task_receipt", subject_person_id="owner",
            viewer_scope="owner", shareability="owner_private",
            narrative="the model says it worked",
        )


def test_on_time_receipt_hits_and_is_idempotent(tmp_path):
    s = store(tmp_path)
    p = create_v2(s, horizon=time.time() + 60)
    receipt = outcome(p, observed=p.horizon - 1)
    result = s.resolve_v2(receipt)
    duplicate = s.resolve_v2(receipt)
    assert result["outcome"] == "hit" and result["late"] is False
    assert duplicate["disposition"] == "duplicate"
    resolved = s.resolved_since(0)[0]
    assert resolved.outcome_observation_id == "eo-1"
    assert resolved.outcome_evidence_refs == ("receipt:task-1",)
    assert resolved.resolution_digest


def test_outcome_idempotency_holds_across_store_connections(tmp_path):
    path = tmp_path / "expectations.db"
    first = ExpectationStore(str(path))
    second = ExpectationStore(str(path))
    p = create_v2(first, horizon=time.time() + 60)
    receipt = outcome(p, observed=p.horizon - 1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda target: target.resolve_v2(receipt), (first, second),
        ))
    assert {item["disposition"] for item in results} == {"resolved", "duplicate"}


def test_positive_receipt_after_horizon_is_a_miss(tmp_path):
    s = store(tmp_path)
    horizon = time.time() - 30
    p = create_v2(s, horizon=horizon)
    result = s.resolve_v2(outcome(p, observed=horizon + 10))
    assert result["outcome"] == "miss"
    assert result["late"] is True


def test_v2_cannot_be_resolved_without_an_outcome_receipt(tmp_path):
    s = store(tmp_path)
    p = create_v2(s)
    with pytest.raises(ValueError, match="outcome observation"):
        s.resolve(p.prediction_id, "hit")


def test_first_valid_outcome_is_sealed(tmp_path):
    s = store(tmp_path)
    p = create_v2(s)
    s.resolve_v2(outcome(p, oid="eo-first", value=False))
    with pytest.raises(ValueError, match="already sealed"):
        s.resolve_v2(outcome(p, oid="eo-second", value=True))


def test_outcome_replay_conflict_and_scope_mismatch_fail(tmp_path):
    s = store(tmp_path)
    p = create_v2(s)
    first = outcome(p)
    s.resolve_v2(first)
    conflict = outcome(p, value=False)
    with pytest.raises(ValueError, match="replay conflict"):
        s.resolve_v2(conflict)

    other = create_v2(s, key="two")
    with pytest.raises(ValueError, match="scope"):
        s.resolve_v2(outcome(
            other, oid="eo-other", subject="person-a", viewer="owner",
        ))


def test_bare_boolean_resolver_cannot_settle_v2(tmp_path):
    s = store(tmp_path)
    p = create_v2(s, horizon=time.time() - 1)
    engine = ExpectationEngine(s)
    engine.register_resolver("task:", lambda _prediction: True)
    assert engine.check() == {"hit": 0, "miss": 0, "unresolved": 0}
    assert s.due()[0].prediction_id == p.prediction_id


class Commitments:
    def __init__(self, item):
        self.item = item

    def get(self, _cid):
        return self.item

    def list(self, status=None, limit=100, **_kwargs):
        return {"commitments": [], "total": 0}


def test_commitment_resolver_compares_fulfillment_event_time_to_horizon(tmp_path):
    s = store(tmp_path)
    horizon = time.time() - 30
    p = s.create_v2(
        subject="commitment:c1", domain="commitment",
        expectation="fulfilled by due date", confidence=0.8,
        horizon=horizon, source="cadence-model", dedup_key="commitment:c1",
        evidence_refs=("commitment:c1",), source_kind="commitment_receipt",
        subject_person_id="owner", viewer_scope="owner",
        shareability="owner_private", cohort="commitment:due-date",
        detail={"commitment_id": "c1"},
    )
    fulfilled = datetime.fromtimestamp(
        horizon + 5, tz=timezone.utc,
    ).isoformat()
    engine = ExpectationEngine(s)
    engine._commitments = lambda: Commitments({
        "id": "c1", "status": "fulfilled", "fulfilled_at": fulfilled,
    })
    counts = engine.check()
    assert counts["miss"] == 1
    assert s.resolved_since(0)[0].outcome_observed_at == pytest.approx(horizon + 5)


def structured_record(kind, native_id, horizon, *, viewer="owner", refs=True):
    fields = {
        "contact": ("contact_id", "next_contact_due_at"),
        "task": ("task_id", "expected_complete_at"),
        "service": ("service_id", "expected_recovery_at"),
        "relationship": ("relationship_id", "followup_due_at"),
    }
    id_field, horizon_field = fields[kind]
    return {
        id_field: native_id,
        horizon_field: horizon,
        "label": native_id,
        "confidence": 0.7,
        "evidence_refs": (f"{kind}:{native_id}",) if refs else (),
        "subject_person_id": "owner",
        "viewer_scope": viewer,
        "shareability": "owner_private",
    }


def test_bounded_generators_cover_non_commitment_domains(tmp_path):
    engine = ExpectationEngine(store(tmp_path))
    future = time.time() + 3600
    assert engine.generate_contact_cadence([
        structured_record("contact", "c1", future),
    ]) == 1
    assert engine.generate_task_duration([
        structured_record("task", "t1", future),
    ]) == 1
    assert engine.generate_service_recovery([
        structured_record("service", "s1", future),
    ]) == 1
    assert engine.generate_relationship_followup([
        structured_record("relationship", "r1", future),
    ]) == 1
    assert {item.domain for item in engine.store.pending()} == {
        "contact_cadence", "task_duration", "service_recovery",
        "relationship_followup",
    }


def test_generator_rejects_missing_evidence_past_horizon_and_obeys_bound(tmp_path):
    engine = ExpectationEngine(store(tmp_path))
    future = time.time() + 3600
    records = [
        structured_record("task", f"t{i}", future) for i in range(5)
    ]
    assert engine.generate_task_duration(records, maximum=2) == 2
    assert engine.generate_task_duration([
        structured_record("task", "bad", future, refs=False),
        structured_record("task", "past", time.time() - 1),
    ]) == 0


def test_calibration_report_is_transparent_and_cohorted(tmp_path):
    s = store(tmp_path)
    engine = ExpectationEngine(s)
    for index, value in enumerate((True, False)):
        p = create_v2(s, key=str(index), horizon=time.time() + 60)
        s.resolve_v2(outcome(
            p, oid=f"eo-{index}", value=value,
            observed=p.horizon - 1, refs=(f"receipt:r-{index}",),
        ))
    report = engine.calibration_report()
    assert report["proper"] is True
    assert report["scoring_rule"] == "brier"
    assert report["resolved_n"] == 2
    assert report["cohorts"][0]["cohort"] == "task_duration:task"
    assert report["domains"]["task_duration"]["formula"].startswith("mean")


def test_observer_projection_is_subject_and_viewer_scoped(tmp_path):
    s = store(tmp_path)
    engine = ExpectationEngine(s)
    create_v2(s, key="owner")
    private = s.create_v2(
        subject="task:private", domain="task_duration", expectation="done",
        confidence=0.6, horizon=time.time() + 60, source="task-adapter-v2",
        dedup_key="task:private", evidence_refs=("task:private",),
        source_kind="task_receipt", subject_person_id="person-a",
        viewer_scope="person:person-a", shareability="subject_private",
        cohort="task_duration:task",
    )
    s.resolve_v2(OutcomeObservationV1.create(
        observation_id="eo-private",
        prediction_id=private.prediction_id,
        value=True,
        observed_at=private.horizon - 1,
        evidence_refs=("receipt:private",),
        source_kind="task_receipt",
        subject_person_id="person-a",
        viewer_scope="person:person-a",
        shareability="subject_private",
    ))
    owner = engine.observer_projection(
        subject_person_id="owner", viewer_scope="owner",
    )
    person = engine.observer_projection(
        subject_person_id="person-a", viewer_scope="person:person-a",
    )
    wrong = engine.observer_projection(
        subject_person_id="person-a", viewer_scope="owner",
    )
    assert [item["subject"] for item in owner["pending"]] == ["task:owner"]
    assert [item["subject"] for item in person["resolved"]] == ["task:private"]
    assert wrong["pending"] == []
    assert owner["calibration"]["resolved_n"] == 0
    assert person["calibration"]["resolved_n"] == 1


def test_legacy_owner_snapshot_keeps_owner_private_cross_subject_visibility(tmp_path):
    s = store(tmp_path)
    engine = ExpectationEngine(s)
    create_v2(s, key="for-contact", subject="person-a", viewer="owner")
    s.create_v2(
        subject="task:subject-only", domain="task_duration", expectation="done",
        confidence=0.6, horizon=time.time() + 60, source="task-adapter-v2",
        dedup_key="task:subject-only", evidence_refs=("task:subject-only",),
        source_kind="task_receipt", subject_person_id="person-a",
        viewer_scope="person:person-a", shareability="subject_private",
        cohort="task_duration:task",
    )
    subjects = {row["subject"] for row in engine.snapshot()["pending"]}
    assert "task:for-contact" in subjects
    assert "task:subject-only" not in subjects


def test_legacy_create_and_boolean_resolution_remain_compatible(tmp_path):
    s = store(tmp_path)
    prediction = s.create(
        subject="legacy:one", domain="legacy", expectation="works",
        confidence=0.5, horizon=time.time() - 1, source="legacy",
        dedup_key="legacy:one",
    )
    engine = ExpectationEngine(s)
    engine.register_resolver("legacy:", lambda _prediction: True)
    assert engine.check()["hit"] == 1
    assert s.resolved_since(0)[0].schema_version == 1
    assert prediction.schema_version == 1
