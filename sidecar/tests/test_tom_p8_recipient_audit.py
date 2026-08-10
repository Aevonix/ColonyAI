"""Append-only, reference-only P8 recipient-simulation evidence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3

import pytest

from colony_sidecar.tom import recipient_audit
from colony_sidecar.tom.recipient_audit import (
    RecipientAuditConflictError,
    RecipientSimulationAuditStore,
    evaluation_event_from_result,
    open_recipient_simulation_audit_store,
    sample_event,
)
from colony_sidecar.tom.recipient_simulator import (
    RecipientSimulationRequestV1,
    RecipientSimulationResultV1,
    RepairSuggestionV1,
    SimulationRiskV1,
)
from colony_sidecar.tom.visibility import ViewerContextV1, content_digest


NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


def _viewer(
    person: str,
    *,
    attested: bool = True,
    scope_revision: str = "scope:1",
) -> ViewerContextV1:
    return ViewerContextV1(
        principal_id=f"surface:{person}" if person else "",
        viewer_person_id=person,
        owner_person_id="owner",
        audiences=("viewer",),
        conversation_scope=f"dm:{person}" if person else "",
        scope_revision=scope_revision if attested else "",
        attested=attested,
    )


def _request(
    item: str,
    *,
    person: str = "alice",
    high_salience: bool = True,
    attempt: str = "",
) -> RecipientSimulationRequestV1:
    suffix = f":{attempt}" if attempt else ""
    return RecipientSimulationRequestV1(
        simulation_id=f"simulation:{item}{suffix}",
        draft_text=f"private draft for {item}",
        draft_fact_refs=(f"fact:{item}",),
        recipient=_viewer(person),
        risk_class="medium",
        surface="text",
        high_salience=high_salience,
        created_at=NOW.isoformat(),
    )


def _result(
    request: RecipientSimulationRequestV1,
    *,
    evaluated: bool = True,
) -> RecipientSimulationResultV1:
    related_ref = request.draft_fact_refs[0] if request.draft_fact_refs else ""
    risk = SimulationRiskV1(
        code="fact_ref_not_recipient_authorized",
        severity="critical",
        fact_ref=related_ref,
    )
    repair = RepairSuggestionV1(
        code="add_fact_provenance", priority="medium",
        related_ref=related_ref)
    result = RecipientSimulationResultV1(
        simulation_id=request.simulation_id,
        mode="shadow" if evaluated else "off",
        evaluated=evaluated,
        risk_class=request.risk_class,
        fail_behavior={
            "low": "observe", "medium": "review",
            "high": "hold", "critical": "hold",
        }[request.risk_class],
        recommended_action="observe_only" if evaluated else "no_effect",
        would_recommend="hold" if evaluated else "no_effect",
        risks=(risk,) if evaluated else (),
        repairs=(repair,) if evaluated else (),
        authorized_fact_refs=request.draft_fact_refs if evaluated else (),
        active_arc_refs=("arc:private-host-topology",) if evaluated else (),
        fact_projection_digest="1" * 64 if evaluated else "",
        arc_projection_digest="2" * 64 if evaluated else "",
        evaluation_path="shadow_observation" if evaluated else "disabled",
        external_effect=False,
        authority_granted=False,
        synchronous_gate=False,
        request_digest=request.audit_digest,
        audit_digest="0" * 64,
    )
    payload = result.public()
    payload.pop("audit_digest")
    digest = hashlib.sha256(json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")).hexdigest()
    return replace(result, audit_digest=digest)


def _sample(item: str, *, person: str = "alice", high_salience: bool = True):
    return sample_event(
        event_id=f"audit:sample:{item}",
        idempotency_key=f"sample:{item}",
        outbound_item_ref=f"outbound:{item}",
        recipient=_viewer(person),
        high_salience=high_salience,
        draft_text=f"private draft for {item}",
        sampled_at=NOW,
    )


def _evaluation(
    item: str,
    *,
    person: str = "alice",
    evaluated: bool = True,
    attempt: str = "",
):
    request = _request(item, person=person, attempt=attempt)
    suffix = f":{attempt}" if attempt else ""
    return evaluation_event_from_result(
        event_id=f"audit:evaluation:{item}{suffix}",
        idempotency_key=f"evaluation:{item}{suffix}",
        outbound_item_ref=f"outbound:{item}",
        request=request,
        result=_result(request, evaluated=evaluated),
        evaluated_at=NOW,
    )


def test_off_factory_creates_no_directory_or_database(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "recipient-audit.db"
    monkeypatch.delenv("COLONY_RECIPIENT_SIMULATOR_MODE", raising=False)
    assert open_recipient_simulation_audit_store(path) is None
    assert not path.exists()
    assert not path.parent.exists()
    assert open_recipient_simulation_audit_store(path, mode="invalid") is None
    assert not path.exists()


def test_exact_replay_conflicts_and_append_only_guards(tmp_path):
    path = tmp_path / "audit.db"
    store = RecipientSimulationAuditStore(path)
    event = _sample("one")
    first = store.append(event)
    replay = store.append(event)
    assert first.appended is True and replay.replayed is True

    with pytest.raises(RecipientAuditConflictError, match="immutable"):
        store.append(sample_event(
            event_id=event.event_id,
            idempotency_key=event.idempotency_key,
            outbound_item_ref="outbound:changed",
            recipient=_viewer("alice"),
            high_salience=True,
            draft_text="private draft for changed",
            sampled_at=NOW,
        ))
    with pytest.raises(RecipientAuditConflictError, match="immutable"):
        store.append(sample_event(
            event_id="audit:sample:alias",
            idempotency_key="sample:alias",
            outbound_item_ref=event.outbound_item_ref,
            recipient=_viewer("alice"),
            high_salience=True,
            draft_text="private draft for one",
            sampled_at=NOW,
        ))
    store.close()

    reopened = RecipientSimulationAuditStore(path)
    durable_replay = reopened.append(event)
    assert durable_replay.replayed is True
    reopened.close()

    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("UPDATE recipient_simulation_audit SET evaluated=1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM recipient_simulation_audit")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "INSERT OR REPLACE INTO recipient_simulation_audit "
            "SELECT * FROM recipient_simulation_audit")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "INSERT OR REPLACE INTO recipient_simulation_audit "
            "SELECT seq,event_id||':replacement',"
            "idempotency_key||':replacement',event_kind,"
            "outbound_item_ref||':replacement',recipient_person_id,"
            "scope_revision,high_salience,occurred_at,simulation_ref,"
            "request_digest,result_digest,draft_digest,surface,risk_class,"
            "evaluated,would_action,effective_action,evaluation_path,"
            "risk_codes_json,repair_codes_json,event_digest||'0',payload_json,"
            "stored_at FROM recipient_simulation_audit")
    connection.close()


def test_audit_rows_are_digest_reference_only_without_content_or_topology(tmp_path):
    path = tmp_path / "audit.db"
    store = RecipientSimulationAuditStore(path)
    store.append(_sample("secret"))
    request = _request("secret")
    result = _result(request)
    event = evaluation_event_from_result(
        event_id="audit:evaluation:secret",
        idempotency_key="evaluation:secret",
        outbound_item_ref="outbound:secret",
        request=request,
        result=result,
        evaluated_at=NOW,
    )
    store.append(event)
    store.close()

    raw = repr(sqlite3.connect(path).execute(
        "SELECT * FROM recipient_simulation_audit").fetchall())
    assert request.draft_text not in raw
    assert request.draft_fact_refs[0] not in raw
    assert result.active_arc_refs[0] not in raw
    assert "private-host-topology" not in raw
    assert content_digest(request.draft_text) in raw
    assert result.audit_digest in raw
    assert event.risk_codes == (
        "critical:fact_ref_not_recipient_authorized",)
    assert event.repair_codes == ("medium:add_fact_provenance",)


def test_result_adapter_rejects_mismatched_request_or_authority():
    request = _request("one")
    wrong = _request("two")
    with pytest.raises(ValueError, match="request digest"):
        evaluation_event_from_result(
            event_id="audit:evaluation:one",
            idempotency_key="evaluation:one",
            outbound_item_ref="outbound:one",
            request=request,
            result=_result(wrong),
            evaluated_at=NOW,
        )


def test_result_adapter_recomputes_digest_and_rejects_topology_codes():
    request = _request("one")
    valid = _result(request)
    with pytest.raises(ValueError, match="audit digest"):
        evaluation_event_from_result(
            event_id="audit:evaluation:forged",
            idempotency_key="evaluation:forged",
            outbound_item_ref="outbound:one",
            request=request,
            result=replace(valid, audit_digest="f" * 64),
            evaluated_at=NOW,
        )

    forged = replace(
        valid,
        risks=(SimulationRiskV1(
            code="host:spark8:10.0.0.8", severity="critical"),),
        audit_digest="0" * 64,
    )
    forged_payload = forged.public()
    forged_payload.pop("audit_digest")
    forged = replace(forged, audit_digest=hashlib.sha256(json.dumps(
        forged_payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")).hexdigest())
    with pytest.raises(ValueError, match="risk code"):
        evaluation_event_from_result(
            event_id="audit:evaluation:topology",
            idempotency_key="evaluation:topology",
            outbound_item_ref="outbound:one",
            request=request,
            result=forged,
            evaluated_at=NOW,
        )

    valid_event = evaluation_event_from_result(
        event_id="audit:evaluation:valid",
        idempotency_key="evaluation:valid",
        outbound_item_ref="outbound:one",
        request=request,
        result=valid,
        evaluated_at=NOW,
    )
    with pytest.raises(ValueError, match="risk code"):
        replace(
            valid_event,
            risk_codes=("critical:host:spark8:10.0.0.8",),
        )

    impossible = replace(
        valid,
        mode="off",
        evaluated=True,
        recommended_action="send",
        evaluation_path="pre_send_advisory",
        audit_digest="0" * 64,
    )
    impossible_payload = impossible.public()
    impossible_payload.pop("audit_digest")
    impossible = replace(impossible, audit_digest=hashlib.sha256(json.dumps(
        impossible_payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")).hexdigest())
    with pytest.raises(ValueError, match="off mode|state matrix"):
        evaluation_event_from_result(
            event_id="audit:evaluation:impossible",
            idempotency_key="evaluation:impossible",
            outbound_item_ref="outbound:one",
            request=request,
            result=impossible,
            evaluated_at=NOW,
        )

    topology_request = replace(request, surface="host:spark8")
    with pytest.raises(ValueError, match="surface"):
        evaluation_event_from_result(
            event_id="audit:evaluation:surface",
            idempotency_key="evaluation:surface",
            outbound_item_ref="outbound:one",
            request=topology_request,
            result=_result(topology_request),
            evaluated_at=NOW,
        )

    unattested = RecipientSimulationRequestV1(
        simulation_id="simulation:unknown",
        draft_text="unknown",
        draft_fact_refs=(),
        recipient=_viewer("", attested=False),
        risk_class="high",
        surface="text",
        high_salience=True,
        created_at=NOW.isoformat(),
    )
    with pytest.raises(ValueError, match="attested recipient"):
        evaluation_event_from_result(
            event_id="audit:evaluation:unknown",
            idempotency_key="evaluation:unknown",
            outbound_item_ref="outbound:unknown",
            request=unattested,
            result=_result(unattested),
            evaluated_at=NOW,
        )


def test_coverage_accounts_for_every_sampled_high_salience_item(tmp_path):
    store = RecipientSimulationAuditStore(tmp_path / "audit.db")
    store.append(_sample("evaluated"))
    store.append(_sample("missing"))
    store.append(_sample("low", high_salience=False))
    store.append(_sample("bob", person="bob"))
    store.append(_evaluation("evaluated"))

    alice = store.coverage(_viewer("alice"))
    assert alice.sampled_high_salience == 2
    assert alice.evaluated_high_salience == 1
    assert alice.unevaluated_high_salience == 1
    assert alice.missing_item_refs == ("outbound:missing",)
    assert alice.status == "incomplete"
    assert alice.coverage_complete is False

    store.append(_evaluation("missing"))
    complete = store.coverage(_viewer("alice"))
    assert complete.sampled_high_salience == 2
    assert complete.evaluated_high_salience == 2
    assert complete.unevaluated_high_salience == 0
    assert complete.status == "complete"
    assert complete.coverage_complete is True

    bob = store.coverage(_viewer("bob"))
    assert bob.sampled_high_salience == 1
    assert bob.unevaluated_high_salience == 1
    owner = store.coverage(_viewer("owner"))
    assert owner.sampled_high_salience == 3
    assert owner.evaluated_high_salience == 2

    unknown = store.coverage(_viewer("", attested=False))
    assert unknown.status == "viewer_unattested"
    assert unknown.sampled_high_salience == 0
    assert unknown.coverage_complete is False


def test_unevaluated_result_does_not_claim_coverage(tmp_path):
    store = RecipientSimulationAuditStore(tmp_path / "audit.db")
    store.append(_sample("off"))
    store.append(_evaluation("off", evaluated=False))
    coverage = store.coverage(_viewer("alice"))
    assert coverage.sampled_high_salience == 1
    assert coverage.evaluated_high_salience == 0
    assert coverage.status == "incomplete"
    store.append(_evaluation("off", evaluated=True, attempt="retry"))
    retried = store.coverage(_viewer("alice"))
    assert retried.evaluated_high_salience == 1
    assert retried.status == "complete"


def test_evaluation_requires_sample_first(tmp_path):
    store = RecipientSimulationAuditStore(tmp_path / "audit.db")
    with pytest.raises(RecipientAuditConflictError, match="sample first"):
        store.append(_evaluation("orphan"))


def test_sample_binds_exact_draft_and_simulation_is_globally_single_use(tmp_path):
    store = RecipientSimulationAuditStore(tmp_path / "audit.db")
    store.append(_sample("one"))
    store.append(sample_event(
        event_id="audit:sample:two",
        idempotency_key="sample:two",
        outbound_item_ref="outbound:two",
        recipient=_viewer("alice"),
        high_salience=True,
        draft_text="private draft for one",
        sampled_at=NOW,
    ))
    request = _request("one")
    result = _result(request)
    store.append(evaluation_event_from_result(
        event_id="audit:evaluation:one",
        idempotency_key="evaluation:one",
        outbound_item_ref="outbound:one",
        request=request,
        result=result,
        evaluated_at=NOW,
    ))
    duplicate = evaluation_event_from_result(
        event_id="audit:evaluation:two",
        idempotency_key="evaluation:two",
        outbound_item_ref="outbound:two",
        request=request,
        result=result,
        evaluated_at=NOW,
    )
    with pytest.raises(RecipientAuditConflictError, match="immutable"):
        store.append(duplicate)
    coverage = store.coverage(_viewer("alice"))
    assert coverage.sampled_high_salience == 2
    assert coverage.evaluated_high_salience == 1
    assert coverage.status == "incomplete"

    changed_request = replace(
        request,
        simulation_id="simulation:one:changed",
        draft_text="different private draft",
    )
    changed = evaluation_event_from_result(
        event_id="audit:evaluation:changed",
        idempotency_key="evaluation:changed",
        outbound_item_ref="outbound:one",
        request=changed_request,
        result=_result(changed_request),
        evaluated_at=NOW,
    )
    with pytest.raises(RecipientAuditConflictError, match="sample authority"):
        store.append(changed)


def test_projection_is_bounded_and_fails_closed_across_people(tmp_path):
    store = RecipientSimulationAuditStore(tmp_path / "audit.db")
    for item in ("one", "two", "three"):
        store.append(_sample(item))
    store.append(_sample("bob", person="bob"))

    alice = store.project(_viewer("alice"), max_events=2)
    assert len(alice.events) == 2
    assert alice.truncated is True
    assert "outbound:bob" not in repr(alice.public())
    assert {event.recipient_person_id for event in alice.events} == {"alice"}

    stale_scope = store.project(
        _viewer("alice", scope_revision="scope:2"), max_events=2)
    assert stale_scope.events == ()
    stale_coverage = store.coverage(
        _viewer("alice", scope_revision="scope:2"))
    assert stale_coverage.status == "no_samples"
    assert stale_coverage.sampled_high_salience == 0

    unknown = store.project(_viewer("", attested=False), max_events=2)
    assert unknown.events == ()
    assert unknown.viewer_attested is False
    assert unknown.truncated is False
    with pytest.raises(ValueError, match="bounded projection"):
        store.project(_viewer("alice"), max_events=0)


def test_evaluation_must_match_existing_sample_recipient_and_salience(tmp_path):
    store = RecipientSimulationAuditStore(tmp_path / "audit.db")
    store.append(_sample("one", person="alice"))
    with pytest.raises(RecipientAuditConflictError, match="sample"):
        store.append(_evaluation("one", person="bob"))


def test_evaluation_cannot_predate_its_sample(tmp_path):
    store = RecipientSimulationAuditStore(tmp_path / "audit.db")
    store.append(_sample("one"))
    request = _request("one")
    early = evaluation_event_from_result(
        event_id="audit:evaluation:one",
        idempotency_key="evaluation:one",
        outbound_item_ref="outbound:one",
        request=request,
        result=_result(request),
        evaluated_at=NOW - timedelta(seconds=1),
    )
    with pytest.raises(RecipientAuditConflictError, match="predates"):
        store.append(early)


def test_coverage_never_claims_complete_when_a_row_is_corrupt(tmp_path):
    path = tmp_path / "audit.db"
    store = RecipientSimulationAuditStore(path)
    store.append(_sample("one"))
    store.append(_evaluation("one"))
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER recipient_audit_no_update")
    connection.execute(
        "UPDATE recipient_simulation_audit SET payload_json='{}' "
        "WHERE event_kind='evaluation'")
    connection.commit()
    connection.close()

    coverage = store.coverage(_viewer("alice"))
    assert coverage.status == "indeterminate"
    assert coverage.coverage_complete is False
    assert coverage.corrupt_count == 1


def test_coverage_paginates_full_integrity_scan(
    tmp_path, monkeypatch,
):
    store = RecipientSimulationAuditStore(tmp_path / "audit.db")
    store.append(_sample("one"))
    store.append(_evaluation("one"))
    monkeypatch.setattr(recipient_audit, "COVERAGE_FETCH_SIZE", 1)
    coverage = store.coverage(_viewer("alice"))
    assert coverage.scan_truncated is False
    assert coverage.status == "complete"
    assert coverage.coverage_complete is True


def test_concurrent_exact_sample_commits_once(tmp_path):
    path = tmp_path / "audit.db"
    event = _sample("concurrent")
    stores = [RecipientSimulationAuditStore(path) for _ in range(8)]
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda store: store.append(event), stores))
        assert sum(result.appended for result in results) == 1
        assert sum(result.replayed for result in results) == 7
        projection = stores[0].project(_viewer("alice"))
        assert [row.event_id for row in projection.events] == [event.event_id]
    finally:
        for store in stores:
            store.close()
