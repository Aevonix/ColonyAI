"""P7: owner-visible drives and immutable charter governance.

These are contract tests for a deliberately non-executing ranking layer.  A
drive score may order already-eligible goals; it can never create eligibility
or replace P3's charter, boundary, situation, duplicate, and authority gates.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import stat
import threading

import pytest

from colony_sidecar.api.authority import RequestAuthority
from colony_sidecar.cognition.drive_governance import (
    CharterRevisionV1,
    DriveGovernance,
    DriveGovernanceError,
    DriveGovernanceStore,
    DriveRanker,
    DriveSignalV1,
    DriveV1,
    GoalRankInputV1,
    RankingBudgetV1,
    ScopeV1,
    drive_governance_mode,
)
from colony_sidecar.cognition.goal_spine import (
    CognitionSpineStore,
    PolicyDecisionV1,
)
from colony_sidecar.initiatives.approval_authority import (
    ApprovalAuthorityError,
    ApprovalAuthorityStore,
    ApprovalSubjectBinding,
    legacy_action_binding,
)


NOW = datetime(2026, 7, 12, 14, 0, tzinfo=timezone.utc)
OWNER_SCOPE = ScopeV1(
    subject_person_id="person-owner",
    viewer_scope="owner",
    shareability="owner_private",
)


class BoundaryManager:
    def __init__(self, *, allowed=True, reason="no_active_boundaries"):
        self.allowed = allowed
        self.reason = reason
        self.checked = []

    def check(self, action):
        self.checked.append(action)
        return type("Verdict", (), {
            "allowed": self.allowed,
            "reason": self.reason,
        })()


def owner_authority(*, principal="owner-approval-service", owner=True, scope=True):
    return RequestAuthority(
        principal_id=principal,
        credential_id="owner-key-1",
        scopes=frozenset({"approvals:decide"} if scope else {"approvals:read"}),
        viewer_person_id="person-owner",
        person_ids=frozenset({"person-owner"}),
        audiences=frozenset({"owner"} if owner else {"shared"}),
        authenticated=True,
    )


def charter_decision_authority(
    *, principal="owner-charter-approval-service", credential="charter-key-1",
    owner=True, scope=True,
):
    return RequestAuthority(
        principal_id=principal,
        credential_id=credential,
        scopes=frozenset(
            {"charter:approval-decide"}
            if scope else {"charter:approval-read"}
        ),
        viewer_person_id="person-owner",
        person_ids=frozenset({"person-owner"}),
        audiences=frozenset({"owner"} if owner else {"shared"}),
        authenticated=True,
    )


def drive(
    key="owner_outcomes",
    *,
    state="enabled",
    expires_at=None,
    maximum=0.8,
):
    return DriveV1.create(
        key=key,
        version="v1",
        title=key.replace("_", " ").title(),
        definition_summary=f"Prefer evidence-backed progress for {key}",
        max_abs_contribution=maximum,
        max_signals_per_goal=3,
        state=state,
        scope=OWNER_SCOPE,
        evidence_refs=("directive:owner-charter",),
        created_at=NOW,
        expires_at=expires_at,
    )


def charter(
    drives,
    *,
    charter_key="default",
    parent=None,
    label="owner-charter-v1",
    expires_at=None,
    budget=None,
    scope=OWNER_SCOPE,
):
    weights = {item.drive_id: round(0.8 / len(drives), 4) for item in drives}
    return CharterRevisionV1.create(
        charter_key=charter_key,
        revision_label=label,
        parent_revision_id=parent,
        title="Owner charter",
        purpose_summary="Rank bounded goals toward the owner's stated outcomes",
        principles=(
            "Respect explicit boundaries and consent",
            "Prefer verified, reversible progress",
        ),
        drive_weights=weights,
        ranking_budget=budget or RankingBudgetV1(
            max_goals=20,
            max_signals_per_drive=3,
            max_total_signals=60,
            max_evidence_refs_per_goal=12,
        ),
        scope=scope,
        evidence_refs=("directive:owner-charter",),
        proposed_by="model:charter-drafter",
        proposed_at=NOW,
        expires_at=expires_at or NOW + timedelta(days=90),
    )


def make_governance(tmp_path, *, mode="live"):
    store = DriveGovernanceStore(tmp_path / "drive-governance.db")
    approvals = ApprovalAuthorityStore(tmp_path / "approval-authority.db")
    return DriveGovernance(store, approvals, mode=mode), store, approvals


def register_and_propose(governance, drives, revision):
    for index, item in enumerate(drives):
        result = governance.register_drive(
            item, operation_id=f"drive-operation-{index:02d}"
        )
        assert result["status"] in {"drive_registered", "drive_replayed"}
    result = governance.propose_charter(
        revision, operation_id=f"proposal-{revision.revision_id}"
    )
    assert result["lifecycle_status"] == "proposed"


def approve_transition(
    governance,
    approvals,
    revision,
    *,
    transition="activate",
    authority=None,
    request_now=NOW,
    apply_now=NOW,
    decision_id="decision-charter-0001",
    operation_id="ratify-operation-0001",
):
    authority = authority or owner_authority()
    request = governance.ensure_transition_request(
        revision.revision_id,
        transition=transition,
        ttl_seconds=3600,
        now=request_now,
    )
    approvals.decide(
        request["request_id"],
        decision="approve",
        decision_id=decision_id,
        expected_action_digest=request["action_digest"],
        decided_by=authority.principal_id,
        authority_evidence=(
            f"scoped_principal:{authority.principal_id}:"
            f"{authority.credential_id}"
        ),
        now=request_now,
    )
    return governance.ratify_transition(
        revision.revision_id,
        transition=transition,
        approval_request_id=request["request_id"],
        operation_id=operation_id,
        authority=authority,
        now=apply_now,
    ), request


def active_governance(tmp_path, *, drives=None, revision=None, mode="live"):
    governance, store, approvals = make_governance(tmp_path, mode=mode)
    drives = drives or [drive()]
    revision = revision or charter(drives)
    register_and_propose(governance, drives, revision)
    activated, _ = approve_transition(governance, approvals, revision)
    assert activated["lifecycle_status"] == "active"
    return governance, store, approvals, drives, revision


def policy_rows(proposal_id, *, deny=None):
    rows = {}
    for stage in ("charter", "boundary", "situation", "duplicate", "authority"):
        ref = f"policy-decision:{proposal_id}:{stage}"
        rows[ref] = {
            "decision_ref": ref,
            "proposal_id": proposal_id,
            "stage": stage,
            "allowed": stage != deny,
            "reason": "accepted" if stage != deny else f"{stage}_denied",
            "evidence_refs": [f"gate-evidence:{stage}"],
        }
    return rows


def goal(name, *, proposal=None, scope=OWNER_SCOPE, decision_refs=()):
    proposal = proposal or f"goal-proposal:{name}"
    return GoalRankInputV1(
        goal_id=f"project:{name}",
        proposal_id=proposal,
        goal_fingerprint=f"fingerprint-{name}",
        title=f"Goal {name}",
        objective_summary=f"Produce a verified outcome for {name}",
        rationale_summary=f"Evidence indicates {name} should be evaluated",
        evidence_refs=(f"event:{name}",),
        policy_decision_refs=tuple(decision_refs),
        scope=scope,
    )


def signal(item, candidate, value, *, suffix="1", state="active", scope=None):
    return DriveSignalV1.derive(
        drive=item,
        goal_fingerprint=candidate.goal_fingerprint,
        normalized_value=value,
        confidence=0.9,
        state=state,
        rationale_summary=f"Verified signal {suffix} for {candidate.goal_id}",
        evidence_refs=(f"receipt:{candidate.goal_id}:{suffix}",),
        observed_at=NOW,
        expires_at=NOW + timedelta(hours=6),
        scope=scope or item.scope,
    )


def test_mode_is_default_off_and_invalid_values_fail_off(monkeypatch):
    monkeypatch.delenv("COLONY_DRIVE_GOVERNANCE_MODE", raising=False)
    assert drive_governance_mode() == "off"
    monkeypatch.setenv("COLONY_DRIVE_GOVERNANCE_MODE", "surprise")
    assert drive_governance_mode() == "off"
    monkeypatch.setenv("COLONY_DRIVE_GOVERNANCE_MODE", "shadow")
    assert drive_governance_mode() == "shadow"
    monkeypatch.setenv("COLONY_DRIVE_GOVERNANCE_MODE", "live")
    assert drive_governance_mode() == "live"
    monkeypatch.setenv("COLONY_DRIVE_GOVERNANCE_MODE", "bootstrap")
    assert drive_governance_mode() == "bootstrap"


def test_off_mode_creates_no_governance_state(tmp_path):
    path = tmp_path / "nested" / "drive.db"
    governance = DriveGovernance.lazy(path, mode="off")
    result = governance.register_drive(
        drive(), operation_id="drive-operation-off-01"
    )
    assert result == {"enabled": False, "status": "off"}
    assert not path.exists()


def test_store_is_private_additive_and_passes_sqlite_integrity_check(tmp_path):
    path = tmp_path / "drive.db"
    store = DriveGovernanceStore(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 7
    assert store._conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    tables = {
        row[0] for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "drive_definitions", "drive_signals", "charter_revisions",
        "charter_lifecycle_events", "charter_authority_uses",
    } <= tables


def test_scope_narrowing_never_adds_a_viewer_lane():
    public = ScopeV1("person-a", "public", "public")
    shared = ScopeV1("person-a", "shared", "shared")
    subject = ScopeV1("person-a", "person:person-a", "subject_private")
    owner = ScopeV1("person-a", "owner", "owner_private")
    assert public.permits_child(shared)
    assert public.permits_child(subject)
    assert shared.permits_child(owner)
    assert not shared.permits_child(subject)
    assert subject.permits_child(owner)
    assert not owner.permits_child(subject)


def test_drive_definitions_are_immutable_and_operation_replay_is_fenced(tmp_path):
    governance, store, _ = make_governance(tmp_path, mode="shadow")
    item = drive()
    first = governance.register_drive(item, operation_id="drive-operation-0001")
    replay = governance.register_drive(item, operation_id="drive-operation-0001")
    assert first["drive_id"] == replay["drive_id"] == item.drive_id
    assert replay["status"] == "drive_replayed"

    changed = replace(
        item,
        definition_summary="A mutated definition must not replace the original",
    )
    with pytest.raises(DriveGovernanceError, match="operation replay") as exc_info:
        governance.register_drive(changed, operation_id="drive-operation-0001")
    assert exc_info.value.code == "operation_replay_conflict"
    assert store.get_drive(item.drive_id) == item


def test_models_can_propose_but_cannot_activate_without_bounded_owner_authority(tmp_path):
    governance, store, approvals = make_governance(tmp_path)
    item = drive()
    revision = charter([item])
    register_and_propose(governance, [item], revision)
    assert store.revision_projection(revision.revision_id, now=NOW)[
        "lifecycle_status"
    ] == "proposed"

    with pytest.raises(DriveGovernanceError) as exc_info:
        governance.ratify_transition(
            revision.revision_id,
            transition="activate",
            approval_request_id="apr_missing",
            operation_id="ratify-operation-missing",
            authority=owner_authority(),
            now=NOW,
        )
    assert exc_info.value.code == "approval_request_missing"
    assert approvals.list_requests() == []
    assert store.active_revision("default", now=NOW) is None


def test_charter_transition_request_has_an_immutable_typed_public_binding(tmp_path):
    governance, _store, approvals = make_governance(tmp_path)
    item = drive()
    revision = charter([item])
    register_and_propose(governance, [item], revision)

    request = governance.ensure_transition_request(
        revision.revision_id, transition="activate", now=NOW,
    )

    assert request["subject"] == {
        "kind": "charter_transition",
        "subject_id": revision.revision_id,
        "revision": revision.content_digest,
        "action": "activate",
    }
    assert len(request["subject_digest"]) == 64
    assert request["presentation"]["action_name"] == \
        "charter_revision_activate"
    assert revision.title in request["presentation"]["summary"]
    assert request["request_digest_version"] == 2

    binding = legacy_action_binding(
        "charter_revision_activate",
        operation_id="unrelated-operation-0001",
        job_type="charter_governance",
        risk="authority_mutation",
    )
    with pytest.raises(ValueError) as exc_info:
        approvals.ensure_request(
            job_id=request["job_id"],
            binding=binding,
            subject=ApprovalSubjectBinding(
                kind="charter_transition",
                subject_id=revision.revision_id,
                revision="0" * 64,
                action="activate",
            ),
            now=NOW,
        )
    assert getattr(exc_info.value, "code", "") in {
        "subject_binding_conflict", "job_binding_conflict",
    }


def test_typed_projection_hides_arbitrary_and_orphan_authority_rows(tmp_path):
    governance, _store, approvals = make_governance(tmp_path)
    item = drive()
    revision = charter([item])
    register_and_propose(governance, [item], revision)
    canonical = governance.ensure_transition_request(
        revision.revision_id, transition="activate", now=NOW,
    )

    arbitrary = legacy_action_binding(
        "unrelated_action",
        operation_id="arbitrary-operation-0001",
    )
    approvals.ensure_request(
        job_id="arbitrary-job",
        binding=arbitrary,
        now=NOW,
    )
    orphan = legacy_action_binding(
        "charter_revision_activate",
        operation_id="orphan-operation-0001",
        job_type="charter_governance",
        risk="authority_mutation",
    )
    approvals.ensure_request(
        job_id=f"charter-transition:{orphan.action_digest[:24]}",
        binding=orphan,
        subject=ApprovalSubjectBinding(
            kind="charter_transition",
            subject_id="charter:missing:revision",
            revision="f" * 64,
            action="activate",
        ),
        now=NOW,
    )

    projections = governance.list_transition_approval_projections(now=NOW)
    inventory = governance.transition_approval_inventory(now=NOW)

    assert [item["request_id"] for item in projections] == [
        canonical["request_id"],
    ]
    assert projections[0]["schema"] == \
        "ColonyCharterTransitionApprovalProjectionV1"
    assert "scope" not in projections[0]
    assert projections[0]["authority_evidence"] is None
    assert inventory["invalid_hidden_count"] == 1


def test_typed_decision_approves_and_ratifies_once_without_a_grant(tmp_path):
    governance, store, approvals = make_governance(tmp_path)
    item = drive()
    revision = charter([item])
    register_and_propose(governance, [item], revision)
    request = governance.ensure_transition_request(
        revision.revision_id, transition="activate", now=NOW,
    )
    authority = charter_decision_authority()

    with pytest.raises(DriveGovernanceError) as stale:
        governance.decide_transition_request(
            request["request_id"],
            decision="approve",
            decision_id="decision-charter-stale-0001",
            expected_action_digest=request["action_digest"],
            expected_request_digest="0" * 64,
            authority=authority,
            now=NOW,
        )
    assert stale.value.code == "stale_request_digest"

    first = governance.decide_transition_request(
        request["request_id"],
        decision="approve",
        decision_id="decision-charter-typed-0001",
        expected_action_digest=request["action_digest"],
        expected_request_digest=request["request_digest"],
        authority=authority,
        now=NOW,
    )
    replay = governance.decide_transition_request(
        request["request_id"],
        decision="approve",
        decision_id="decision-charter-typed-0001",
        expected_action_digest=request["action_digest"],
        expected_request_digest=request["request_digest"],
        authority=authority,
        now=NOW + timedelta(hours=2),
    )

    assert first["status"] == "approved_applied"
    assert first["application"]["status"] == "applied"
    assert replay["status"] == "approved_applied"
    assert replay["application"]["event_id"] == \
        first["application"]["event_id"]
    assert store.active_revision("default", now=NOW) == revision
    assert len(store.lifecycle_events("default")) == 1
    assert approvals.list_grants() == []


def test_approved_transition_is_visible_and_recoverable_after_apply_crash(
    tmp_path, monkeypatch,
):
    governance, store, _approvals = make_governance(tmp_path)
    item = drive()
    revision = charter([item])
    register_and_propose(governance, [item], revision)
    request = governance.ensure_transition_request(
        revision.revision_id, transition="activate", now=NOW,
    )
    authority = charter_decision_authority()
    original_ratify = governance.ratify_transition

    def crash_after_authority_commit(*_args, **_kwargs):
        raise RuntimeError("simulated process death after decision commit")

    monkeypatch.setattr(governance, "ratify_transition", crash_after_authority_commit)
    with pytest.raises(RuntimeError, match="simulated process death"):
        governance.decide_transition_request(
            request["request_id"],
            decision="approve",
            decision_id="decision-charter-crash-0001",
            expected_action_digest=request["action_digest"],
            expected_request_digest=request["request_digest"],
            authority=authority,
            now=NOW,
        )

    stranded = governance.transition_approval_projection(
        request["request_id"], now=NOW,
    )
    assert stranded["status"] == "approved_unapplied"
    assert stranded["application"]["status"] == "recovery_required"
    assert store.active_revision("default", now=NOW) is None

    monkeypatch.setattr(governance, "ratify_transition", original_ratify)
    other_authority = charter_decision_authority(
        principal="other-owner-charter-service",
    )
    with pytest.raises(DriveGovernanceError) as actor_conflict:
        governance.decide_transition_request(
            request["request_id"],
            decision="approve",
            decision_id="decision-charter-crash-0001",
            expected_action_digest=request["action_digest"],
            expected_request_digest=request["request_digest"],
            authority=other_authority,
            now=NOW,
        )
    assert actor_conflict.value.code == "owner_authority_mismatch"
    with pytest.raises(ApprovalAuthorityError) as decision_conflict:
        governance.decide_transition_request(
            request["request_id"],
            decision="reject",
            decision_id="decision-charter-conflict-0001",
            expected_action_digest=request["action_digest"],
            expected_request_digest=request["request_digest"],
            authority=authority,
            now=NOW,
        )
    assert decision_conflict.value.code == "decision_already_final"
    assert governance.transition_approval_projection(
        request["request_id"], now=NOW,
    )["status"] == "approved_unapplied"

    rotated_authority = charter_decision_authority(
        credential="charter-key-rotated",
    )
    recovered = governance.decide_transition_request(
        request["request_id"],
        decision="approve",
        decision_id="decision-charter-crash-0001",
        expected_action_digest=request["action_digest"],
        expected_request_digest=request["request_digest"],
        authority=rotated_authority,
        now=NOW + timedelta(hours=2),
    )
    assert recovered["status"] == "approved_applied"
    assert recovered["authority_evidence"] == (
        "scoped_principal:owner-charter-approval-service:charter-key-1"
    )
    assert store.active_revision(
        "default", now=NOW + timedelta(hours=2),
    ) == revision


def test_first_valid_typed_approve_reject_race_wins_exactly_once(tmp_path):
    governance, store, approvals = make_governance(tmp_path)
    item = drive()
    revision = charter([item])
    register_and_propose(governance, [item], revision)
    request = governance.ensure_transition_request(
        revision.revision_id, transition="activate", now=NOW,
    )
    authority = charter_decision_authority()
    barrier = threading.Barrier(2)

    def decide(decision, decision_id):
        barrier.wait(timeout=5)
        try:
            projection = governance.decide_transition_request(
                request["request_id"],
                decision=decision,
                decision_id=decision_id,
                expected_action_digest=request["action_digest"],
                expected_request_digest=request["request_digest"],
                authority=authority,
                now=NOW,
            )
            return ("won", projection["authority_status"])
        except (ApprovalAuthorityError, DriveGovernanceError) as exc:
            return ("lost", exc.code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda item: decide(*item),
            (
                ("approve", "decision-race-approve-0001"),
                ("reject", "decision-race-reject-0001"),
            ),
        ))

    assert sum(result[0] == "won" for result in results) == 1
    assert sum(result == ("lost", "decision_already_final") for result in results) == 1
    final = approvals.get_request(request["request_id"], now=NOW)
    assert final["status"] in {"approved", "rejected"}
    if final["status"] == "approved":
        assert store.active_revision("default", now=NOW) == revision
        assert len(store.lifecycle_events("default")) == 1
    else:
        assert store.active_revision("default", now=NOW) is None
        assert store.lifecycle_events("default") == []
    assert approvals.list_grants() == []


def test_approved_stale_transition_is_terminal_visible_and_can_be_re_requested(
    tmp_path,
):
    governance, store, approvals, drives, first = active_governance(tmp_path)
    authority = charter_decision_authority()
    second = charter(
        drives,
        parent=first.revision_id,
        label="owner-charter-stale-v2",
    )
    governance.propose_charter(
        second, operation_id="proposal-charter-stale-v2",
    )
    old_request = governance.ensure_transition_request(
        second.revision_id, transition="activate", now=NOW,
    )
    approvals.decide(
        old_request["request_id"],
        decision="approve",
        decision_id="decision-stale-approved-0001",
        expected_action_digest=old_request["action_digest"],
        decided_by=authority.principal_id,
        authority_evidence=(
            f"scoped_principal:{authority.principal_id}:"
            f"{authority.credential_id}"
        ),
        now=NOW,
    )
    revoke = governance.ensure_transition_request(
        first.revision_id, transition="revoke", now=NOW,
    )
    governance.decide_transition_request(
        revoke["request_id"],
        decision="approve",
        decision_id="decision-revoke-before-rebind-0001",
        expected_action_digest=revoke["action_digest"],
        expected_request_digest=revoke["request_digest"],
        authority=authority,
        now=NOW,
    )

    stale = governance.transition_approval_projection(
        old_request["request_id"], now=NOW,
    )
    assert stale["status"] == "approved_stale"
    assert stale["application"]["status"] == "new_request_required"
    assert stale["binding_current"] is False
    inventory = governance.transition_approval_inventory(now=NOW)
    assert sum(
        item["status"] == "approved_unapplied"
        for item in inventory["requests"]
    ) == 0

    replacement = governance.ensure_transition_request(
        second.revision_id, transition="activate", now=NOW,
    )
    assert replacement["request_id"] != old_request["request_id"]
    assert replacement["action_digest"] != old_request["action_digest"]
    applied = governance.decide_transition_request(
        replacement["request_id"],
        decision="approve",
        decision_id="decision-rebound-charter-0001",
        expected_action_digest=replacement["action_digest"],
        expected_request_digest=replacement["request_digest"],
        authority=authority,
        now=NOW,
    )
    assert applied["status"] == "approved_applied"
    assert store.active_revision("default", now=NOW) == second
    first_page = governance.transition_approval_inventory(limit=1, now=NOW)
    assert len(first_page["requests"]) == 1
    assert first_page["complete"] is False


def test_charter_cannot_broaden_a_private_drive_scope(tmp_path):
    governance, store, _ = make_governance(tmp_path)
    item = drive()
    public_scope = ScopeV1("person-owner", "public", "public")
    revision = charter([item], label="overbroad-charter", scope=public_scope)
    register_and_propose(governance, [item], revision)
    with pytest.raises(DriveGovernanceError) as exc_info:
        governance.ensure_transition_request(
            revision.revision_id, transition="activate", now=NOW,
        )
    assert exc_info.value.code == "scope_broadening"
    assert store.revision_projection(revision.revision_id, now=NOW)[
        "lifecycle_status"
    ] == "proposed"


@pytest.mark.parametrize(
    "authority",
    [
        owner_authority(owner=False),
        owner_authority(scope=False),
        RequestAuthority(
            principal_id="legacy",
            credential_id="COLONY_API_KEY",
            scopes=frozenset({"*"}),
            viewer_person_id=None,
            person_ids=frozenset(),
            audiences=frozenset({"owner"}),
            authenticated=True,
            legacy=True,
        ),
    ],
)
def test_non_owner_unscoped_and_legacy_authority_cannot_ratify(tmp_path, authority):
    governance, _, approvals = make_governance(tmp_path)
    item = drive()
    revision = charter([item])
    register_and_propose(governance, [item], revision)
    request = governance.ensure_transition_request(
        revision.revision_id, transition="activate", now=NOW
    )
    approvals.decide(
        request["request_id"],
        decision="approve",
        decision_id=f"decision-{authority.principal_id}-0001",
        expected_action_digest=request["action_digest"],
        decided_by=authority.principal_id,
        authority_evidence=(
            f"scoped_principal:{authority.principal_id}:"
            f"{authority.credential_id}"
        ),
        now=NOW,
    )
    with pytest.raises(DriveGovernanceError) as exc_info:
        governance.ratify_transition(
            revision.revision_id,
            transition="activate",
            approval_request_id=request["request_id"],
            operation_id=f"ratify-{authority.principal_id}-0001",
            authority=authority,
            now=NOW,
        )
    assert exc_info.value.code == "owner_authority_required"


def test_ratification_actor_must_match_the_approved_scoped_principal(tmp_path):
    governance, _, approvals = make_governance(tmp_path)
    item = drive()
    revision = charter([item])
    register_and_propose(governance, [item], revision)
    request = governance.ensure_transition_request(
        revision.revision_id, transition="activate", now=NOW,
    )
    approvals.decide(
        request["request_id"], decision="approve",
        decision_id="decision-owner-mismatch",
        expected_action_digest=request["action_digest"],
        decided_by="owner-approval-service",
        authority_evidence="scoped_principal:owner-approval-service:owner-key-1",
        now=NOW,
    )
    with pytest.raises(DriveGovernanceError) as exc_info:
        governance.ratify_transition(
            revision.revision_id, transition="activate",
            approval_request_id=request["request_id"],
            operation_id="ratify-owner-mismatch",
            authority=owner_authority(principal="different-owner-principal"),
            now=NOW,
        )
    assert exc_info.value.code == "owner_authority_mismatch"


def test_activation_replay_supersession_and_revocation_are_append_only(tmp_path):
    governance, store, approvals, drives, first = active_governance(tmp_path)
    history_before = store.lifecycle_events("default")
    replay = governance.ratify_transition(
        first.revision_id,
        transition="activate",
        approval_request_id=history_before[-1]["approval_request_id"],
        operation_id="ratify-operation-0001",
        authority=owner_authority(),
        now=NOW,
    )
    assert replay["replayed"] is True
    assert store.lifecycle_events("default") == history_before

    second = charter(
        drives,
        parent=first.revision_id,
        label="owner-charter-v2",
        expires_at=NOW + timedelta(days=120),
    )
    governance.propose_charter(second, operation_id="proposal-charter-v2")
    activated, activation_request = approve_transition(
        governance,
        approvals,
        second,
        decision_id="decision-charter-0002",
        operation_id="ratify-operation-0002",
    )
    assert activated["lifecycle_status"] == "active"
    assert store.revision_projection(first.revision_id, now=NOW)[
        "lifecycle_status"
    ] == "superseded"
    assert store.active_revision("default", now=NOW) == second

    # One exact approval cannot be replayed under a new operation.
    with pytest.raises(DriveGovernanceError) as exc_info:
        governance.ratify_transition(
            second.revision_id,
            transition="activate",
            approval_request_id=activation_request["request_id"],
            operation_id="ratify-operation-reuse",
            authority=owner_authority(),
            now=NOW,
        )
    assert exc_info.value.code in {"approval_binding_mismatch", "authority_replay"}

    revoked, _ = approve_transition(
        governance,
        approvals,
        second,
        transition="revoke",
        decision_id="decision-charter-0003",
        operation_id="ratify-operation-0003",
    )
    assert revoked["lifecycle_status"] == "revoked"
    assert store.active_revision("default", now=NOW) is None
    assert [event["transition"] for event in store.lifecycle_events("default")] == [
        "activate", "supersede", "activate", "revoke",
    ]


def test_lifecycle_projection_detects_column_or_payload_tampering(tmp_path):
    _, store, _, _, revision = active_governance(tmp_path)
    with store._lock, store._conn:
        store._conn.execute(
            "UPDATE charter_lifecycle_events SET transition='revoke' "
            "WHERE revision_id=?",
            (revision.revision_id,),
        )
    with pytest.raises(DriveGovernanceError) as exc_info:
        store.revision_projection(revision.revision_id, now=NOW)
    assert exc_info.value.code == "lifecycle_integrity_error"


def test_pre_expiry_decision_recovers_after_expiry_but_revision_still_expires(tmp_path):
    governance, store, approvals = make_governance(tmp_path)
    item = drive()
    first = charter([item])
    register_and_propose(governance, [item], first)
    stale_request = governance.ensure_transition_request(
        first.revision_id, transition="activate", now=NOW
    )
    approvals.decide(
        stale_request["request_id"], decision="approve",
        decision_id="decision-stale-0001",
        expected_action_digest=stale_request["action_digest"],
        decided_by="owner-approval-service",
        authority_evidence="scoped_principal:owner-approval-service:owner-key-1",
        now=NOW,
    )

    recovered = governance.ratify_transition(
        first.revision_id, transition="activate",
        approval_request_id=stale_request["request_id"],
        operation_id="ratify-after-request-expiry",
        authority=owner_authority(), now=NOW + timedelta(hours=25),
    )
    assert recovered["lifecycle_status"] == "active"
    assert store.active_revision("default", now=NOW + timedelta(hours=25)) == first

    governance, store, approvals = make_governance(
        tmp_path / "expired-revision",
    )
    register_and_propose(governance, [item], charter(
        [item], label="bootstrap-for-expiry",
    ))
    expired = charter(
        [item], label="already-expired",
        expires_at=NOW + timedelta(hours=1),
    )
    governance.propose_charter(expired, operation_id="proposal-expiring-charter")
    request = governance.ensure_transition_request(
        expired.revision_id, transition="activate", ttl_seconds=4 * 60 * 60,
        now=NOW,
    )
    approvals.decide(
        request["request_id"], decision="approve",
        decision_id="decision-expired-charter",
        expected_action_digest=request["action_digest"],
        decided_by="owner-approval-service",
        authority_evidence="scoped_principal:owner-approval-service:owner-key-1",
        now=NOW,
    )
    with pytest.raises(DriveGovernanceError) as exc_info:
        governance.ratify_transition(
            expired.revision_id, transition="activate",
            approval_request_id=request["request_id"],
            operation_id="ratify-expired-charter",
            authority=owner_authority(), now=NOW + timedelta(hours=2),
        )
    assert exc_info.value.code == "revision_expired"


def test_shadow_mode_records_proposals_but_never_requests_or_changes_authority(tmp_path):
    governance, store, approvals = make_governance(tmp_path, mode="shadow")
    item = drive()
    revision = charter([item])
    register_and_propose(governance, [item], revision)
    result = governance.ensure_transition_request(
        revision.revision_id, transition="activate", now=NOW
    )
    assert result["status"] == "shadow_transition_candidate"
    assert result["effect_executed"] is False
    assert approvals.list_requests() == []
    assert store.active_revision("default", now=NOW) is None


def test_bootstrap_mode_ratifies_only_typed_initial_charter_and_never_applies_ranking(
    tmp_path,
):
    governance, store, approvals = make_governance(tmp_path, mode="bootstrap")
    item = drive()
    revision = charter([item])
    register_and_propose(governance, [item], revision)

    request = governance.ensure_transition_request(
        revision.revision_id, transition="activate", now=NOW,
    )
    projection = governance.decide_transition_request(
        request["request_id"],
        decision="approve",
        decision_id="bootstrap-charter-decision-0001",
        expected_action_digest=request["action_digest"],
        expected_request_digest=request["request_digest"],
        authority=charter_decision_authority(),
        now=NOW,
    )

    assert projection["status"] == "approved_applied"
    assert store.active_revision("default", now=NOW).revision_id == \
        revision.revision_id
    assert approvals.list_grants() == []

    ranked = DriveRanker(
        store,
        policy_decision_resolver=lambda _reference: None,
        directive_manager=BoundaryManager(),
    ).rank([], mode="bootstrap", now=NOW)
    assert ranked.mode == "bootstrap"
    assert ranked.ranking_applied is False
    assert ranked.effective_order == ()
    with pytest.raises(DriveGovernanceError) as signal_held:
        candidate = goal("bootstrap-held")
        governance.record_signal(
            signal(item, candidate, 0.5),
            operation_id="bootstrap-signal-held-0001",
        )
    assert signal_held.value.code == "bootstrap_operation_held"
    with pytest.raises(DriveGovernanceError) as revision_held:
        governance.ensure_transition_request(
            revision.revision_id, transition="revoke", now=NOW,
        )
    assert revision_held.value.code == "bootstrap_transition_held"


def test_bootstrap_allows_only_one_global_root_even_if_two_keys_were_preapproved(
    tmp_path,
):
    governance, store, approvals = make_governance(tmp_path, mode="bootstrap")
    item = drive()
    first = charter([item], charter_key="owner-primary", label="primary-v1")
    second = charter([item], charter_key="owner-secondary", label="secondary-v1")
    governance.register_drive(item, operation_id="bootstrap-global-drive-0001")
    governance.propose_charter(
        first, operation_id="bootstrap-global-primary-proposal-0001",
    )
    governance.propose_charter(
        second, operation_id="bootstrap-global-secondary-proposal-0001",
    )
    first_request = governance.ensure_transition_request(
        first.revision_id, transition="activate", now=NOW,
    )
    second_request = governance.ensure_transition_request(
        second.revision_id, transition="activate", now=NOW,
    )
    authority = charter_decision_authority()
    for index, request in enumerate((first_request, second_request), start=1):
        approvals.decide(
            request["request_id"],
            decision="approve",
            decision_id=f"bootstrap-global-preapproval-000{index}",
            expected_action_digest=request["action_digest"],
            decided_by=authority.principal_id,
            authority_evidence=(
                f"scoped_principal:{authority.principal_id}:"
                f"{authority.credential_id}"
            ),
            now=NOW,
        )

    governance.ratify_transition(
        first.revision_id,
        transition="activate",
        approval_request_id=first_request["request_id"],
        operation_id="bootstrap-global-primary-ratify-0001",
        authority=authority,
        now=NOW,
    )

    assert governance.transition_approval_projection(
        second_request["request_id"], now=NOW,
    )["status"] == "approved_stale"
    with pytest.raises(DriveGovernanceError) as ratify_held:
        governance.ratify_transition(
            second.revision_id,
            transition="activate",
            approval_request_id=second_request["request_id"],
            operation_id="bootstrap-global-secondary-ratify-0001",
            authority=authority,
            now=NOW,
        )
    assert ratify_held.value.code == "bootstrap_transition_held"
    with pytest.raises(DriveGovernanceError) as request_held:
        governance.ensure_transition_request(
            second.revision_id, transition="activate", now=NOW,
        )
    assert request_held.value.code == "bootstrap_transition_held"
    assert store.active_revision("owner-primary", now=NOW) == first
    assert store.active_revision("owner-secondary", now=NOW) is None


def test_signals_require_evidence_are_scope_bounded_and_replay_fenced(tmp_path):
    governance, store, _, drives, _ = active_governance(tmp_path)
    candidate = goal("alpha")
    item = drives[0]
    item_signal = signal(item, candidate, 0.75)
    first = governance.record_signal(
        item_signal, operation_id="signal-operation-0001"
    )
    replay = governance.record_signal(
        item_signal, operation_id="signal-operation-0001"
    )
    assert first["signal_id"] == replay["signal_id"]
    assert replay["status"] == "signal_replayed"

    with pytest.raises(DriveGovernanceError, match="evidence"):
        DriveSignalV1.derive(
            drive=item, goal_fingerprint=candidate.goal_fingerprint,
            normalized_value=0.5, confidence=0.9,
            rationale_summary="Unsupported model opinion",
            evidence_refs=(), observed_at=NOW,
            expires_at=NOW + timedelta(hours=1), scope=item.scope,
        )
    with pytest.raises(DriveGovernanceError, match="broaden"):
        DriveSignalV1.derive(
            drive=item, goal_fingerprint=candidate.goal_fingerprint,
            normalized_value=0.5, confidence=0.9,
            rationale_summary="Attempted public signal",
            evidence_refs=("receipt:scope",), observed_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            scope=ScopeV1("person-owner", "public", "public"),
        )

    changed = replace(item_signal, normalized_value=-0.5)
    with pytest.raises(DriveGovernanceError) as exc_info:
        governance.record_signal(changed, operation_id="signal-operation-0001")
    assert exc_info.value.code == "operation_replay_conflict"
    assert store.signals_for_goal(candidate.goal_fingerprint, now=NOW) == [item_signal]


def test_ranking_is_deterministic_weighted_bounded_and_owner_visible(tmp_path):
    first_drive = drive("owner_outcomes", maximum=0.5)
    second_drive = drive("system_reliability", maximum=0.4)
    budget = RankingBudgetV1(
        max_goals=10, max_signals_per_drive=2,
        max_total_signals=10, max_evidence_refs_per_goal=5,
    )
    revision = charter([first_drive, second_drive], budget=budget)
    governance, store, _, _, _ = active_governance(
        tmp_path, drives=[first_drive, second_drive], revision=revision,
    )

    rows = {}
    alpha_rows = policy_rows("goal-proposal:alpha")
    beta_rows = policy_rows("goal-proposal:beta")
    rows.update(alpha_rows)
    rows.update(beta_rows)
    alpha = goal("alpha", decision_refs=alpha_rows)
    beta = goal("beta", decision_refs=beta_rows)
    for index, item_signal in enumerate((
        signal(first_drive, alpha, 0.9, suffix="a1"),
        signal(first_drive, alpha, 0.8, suffix="a2"),
        # Third signal is fresher only by ID tie-break and is cut by budget=2.
        signal(first_drive, alpha, -1.0, suffix="a3"),
        signal(second_drive, alpha, 0.5, suffix="a4"),
        signal(first_drive, beta, 0.2, suffix="b1"),
        signal(second_drive, beta, -0.5, suffix="b2"),
    )):
        governance.record_signal(
            item_signal, operation_id=f"signal-rank-{index:04d}"
        )

    ranker = DriveRanker(
        store,
        policy_decision_resolver=rows.get,
        directive_manager=BoundaryManager(),
    )
    batch = ranker.rank([beta, alpha], mode="live", now=NOW)
    replay = ranker.rank([beta, alpha], mode="live", now=NOW)
    assert batch == replay
    assert batch.status == "ranked"
    assert batch.ranking_applied is True
    assert batch.suggested_order == (alpha.goal_id, beta.goal_id)
    assert batch.effective_order == batch.suggested_order
    assert all(item.authorization_effect == "none" for item in batch.results)
    alpha_result = next(item for item in batch.results if item.goal_id == alpha.goal_id)
    assert alpha_result.total_score <= 1.0
    assert len(alpha_result.evidence_refs) <= budget.max_evidence_refs_per_goal
    assert all(contribution.abs_budget <= 0.5 for contribution in alpha_result.contributions)
    assert "chain" not in str(alpha_result.payload()).lower()

    owner_view = batch.observer_projection(
        viewer_person_id="person-owner",
        owner_person_id="person-owner",
        audiences={"owner"},
    )
    stranger_view = batch.observer_projection(
        viewer_person_id="person-stranger",
        owner_person_id="person-owner",
        audiences={"shared"},
    )
    assert len(owner_view["results"]) == 2
    assert stranger_view["results"] == []


@pytest.mark.parametrize("denied_stage", [
    "charter", "boundary", "situation", "duplicate", "authority",
])
def test_no_drive_score_can_override_any_p3_policy_gate(tmp_path, denied_stage):
    governance, store, _, drives, _ = active_governance(tmp_path)
    rows = policy_rows("goal-proposal:unsafe", deny=denied_stage)
    candidate = goal("unsafe", decision_refs=rows)
    governance.record_signal(
        signal(drives[0], candidate, 1.0),
        operation_id=f"signal-denied-{denied_stage}",
    )
    batch = DriveRanker(
        store,
        policy_decision_resolver=rows.get,
        directive_manager=BoundaryManager(),
    ).rank([candidate], mode="live", now=NOW)
    result = batch.results[0]
    assert result.eligible is False
    assert result.total_score is None
    assert result.state == f"p3_{denied_stage}_denied"
    assert batch.effective_order == ()
    assert result.authorization_effect == "none"


def test_missing_or_forged_policy_evidence_fails_closed(tmp_path):
    governance, store, _, drives, _ = active_governance(tmp_path)
    rows = policy_rows("goal-proposal:alpha")
    candidate = goal("alpha", decision_refs=rows)
    governance.record_signal(
        signal(drives[0], candidate, 1.0), operation_id="signal-policy-missing"
    )
    missing = dict(rows)
    missing.pop(next(iter(missing)))
    batch = DriveRanker(
        store,
        policy_decision_resolver=missing.get,
        directive_manager=BoundaryManager(),
    ).rank([candidate], mode="live", now=NOW)
    assert batch.results[0].state == "p3_policy_unknown"
    assert batch.results[0].total_score is None

    forged = dict(rows)
    ref = next(iter(forged))
    forged[ref] = {**forged[ref], "proposal_id": "goal-proposal:somebody-else"}
    batch = DriveRanker(
        store,
        policy_decision_resolver=forged.get,
        directive_manager=BoundaryManager(),
    ).rank([candidate], mode="live", now=NOW)
    assert batch.results[0].state == "p3_policy_conflict"
    assert batch.effective_order == ()


def test_ranker_resolves_real_p3_persisted_policy_decisions(tmp_path):
    governance, store, _, _, _ = active_governance(tmp_path)
    cognition = CognitionSpineStore(str(tmp_path / "cognition.db"))
    proposal_id = "goal-proposal:real-p3"
    refs = []
    for stage in ("charter", "boundary", "situation", "duplicate", "authority"):
        decision = PolicyDecisionV1.create(
            proposal_id, stage, True, f"{stage}_accepted",
            (f"gate-evidence:{stage}",),
        )
        cognition.save_policy_decision(decision)
        refs.append(decision.decision_ref)
    candidate = goal(
        "real-p3", proposal=proposal_id, decision_refs=tuple(refs),
    )
    batch = DriveRanker(
        store,
        policy_decision_resolver=cognition.get_policy_decision,
        directive_manager=BoundaryManager(),
    ).rank([candidate], mode="live", now=NOW)
    assert batch.status == "ranked"
    assert batch.results[0].eligible is True
    assert set(refs) == set(batch.results[0].policy_decision_refs)


def test_global_pause_and_late_boundary_changes_override_ranking(tmp_path):
    governance, store, _, drives, _ = active_governance(tmp_path)
    rows = policy_rows("goal-proposal:alpha")
    candidate = goal("alpha", decision_refs=rows)
    governance.record_signal(
        signal(drives[0], candidate, 1.0), operation_id="signal-pause-0001"
    )

    paused = DriveRanker(
        store,
        policy_decision_resolver=rows.get,
        directive_manager=BoundaryManager(
            allowed=False, reason="global_pause_active"
        ),
    ).rank([candidate], mode="live", now=NOW)
    assert paused.status == "global_pause_active"
    assert paused.effective_order == ()
    assert paused.results[0].state == "global_pause_active"

    changed = DriveRanker(
        store,
        policy_decision_resolver=rows.get,
        directive_manager=BoundaryManager(
            allowed=False, reason="owner_boundary_changed"
        ),
    ).rank([candidate], mode="live", now=NOW)
    assert changed.results[0].state == "boundary_recheck_denied"
    assert changed.effective_order == ()


def test_global_pause_wins_even_without_an_active_charter(tmp_path):
    store = DriveGovernanceStore(tmp_path / "empty-drive.db")
    candidate = goal("pause-before-charter")
    batch = DriveRanker(
        store,
        policy_decision_resolver=lambda _ref: None,
        directive_manager=BoundaryManager(
            allowed=False, reason="global_pause_active",
        ),
    ).rank([candidate], mode="live", now=NOW)
    assert batch.status == "global_pause_active"
    assert batch.charter_revision_id is None
    assert batch.effective_order == ()


def test_shadow_ranking_is_observational_and_unknown_states_are_explicit(tmp_path):
    item = drive(state="disabled")
    revision = charter([item])
    governance, store, approvals = make_governance(tmp_path)
    register_and_propose(governance, [item], revision)
    with pytest.raises(DriveGovernanceError) as exc_info:
        approve_transition(governance, approvals, revision)
    assert exc_info.value.code == "drive_disabled"

    enabled = drive()
    revision = charter([enabled])
    governance, store, _, _, _ = active_governance(
        tmp_path / "enabled", drives=[enabled], revision=revision,
    )
    rows = policy_rows("goal-proposal:alpha")
    candidate = goal("alpha", decision_refs=rows)
    ranker = DriveRanker(
        store,
        policy_decision_resolver=rows.get,
        directive_manager=BoundaryManager(),
    )
    shadow = ranker.rank([candidate], mode="shadow", now=NOW)
    assert shadow.status == "shadow_ranked"
    assert shadow.ranking_applied is False
    assert shadow.effective_order == (candidate.goal_id,)
    assert shadow.results[0].drive_states[enabled.drive_id] == "unknown"
    assert shadow.results[0].total_score == 0.0

    no_pause_source = DriveRanker(
        store, policy_decision_resolver=rows.get, directive_manager=None,
    ).rank([candidate], mode="live", now=NOW)
    assert no_pause_source.status == "pause_state_unknown"
    assert no_pause_source.ranking_applied is False
    assert no_pause_source.effective_order == (candidate.goal_id,)


def test_expired_signal_state_is_visible_and_contributes_zero(tmp_path):
    governance, store, _, drives, _ = active_governance(tmp_path)
    rows = policy_rows("goal-proposal:expired-signal")
    candidate = goal("expired-signal", decision_refs=rows)
    expired = DriveSignalV1.derive(
        drive=drives[0], goal_fingerprint=candidate.goal_fingerprint,
        normalized_value=1.0, confidence=1.0,
        rationale_summary="A formerly valid signal",
        evidence_refs=("receipt:expired-signal",),
        observed_at=NOW - timedelta(hours=2),
        expires_at=NOW - timedelta(hours=1), scope=drives[0].scope,
    )
    governance.record_signal(expired, operation_id="signal-expired-visible")
    batch = DriveRanker(
        store,
        policy_decision_resolver=rows.get,
        directive_manager=BoundaryManager(),
    ).rank([candidate], mode="live", now=NOW)
    result = batch.results[0]
    assert result.eligible is True
    assert result.total_score == 0.0
    assert result.drive_states[drives[0].drive_id] == "expired"
    assert result.contributions[0].weighted_value == 0.0


def test_expired_signals_do_not_contribute_and_goal_budget_is_enforced(tmp_path):
    item = drive()
    revision = charter(
        [item],
        budget=RankingBudgetV1(
            max_goals=1, max_signals_per_drive=1,
            max_total_signals=1, max_evidence_refs_per_goal=2,
        ),
    )
    governance, store, _, _, _ = active_governance(
        tmp_path, drives=[item], revision=revision,
    )
    rows = {}
    first_rows = policy_rows("goal-proposal:first")
    second_rows = policy_rows("goal-proposal:second")
    rows.update(first_rows)
    rows.update(second_rows)
    first = goal("first", decision_refs=first_rows)
    second = goal("second", decision_refs=second_rows)
    expired = DriveSignalV1.derive(
        drive=item, goal_fingerprint=first.goal_fingerprint,
        normalized_value=1.0, confidence=1.0,
        rationale_summary="Once valid but now expired",
        evidence_refs=("receipt:expired",), observed_at=NOW - timedelta(hours=2),
        expires_at=NOW - timedelta(hours=1), scope=item.scope,
    )
    governance.record_signal(expired, operation_id="signal-expired-0001")
    batch = DriveRanker(
        store,
        policy_decision_resolver=rows.get,
        directive_manager=BoundaryManager(),
    ).rank([first, second], mode="live", now=NOW)
    assert batch.status == "goal_budget_exceeded"
    assert batch.ranking_applied is False
    assert batch.effective_order == (first.goal_id, second.goal_id)
    assert batch.results == ()


def test_store_observer_projection_preserves_lifecycle_scope_and_evidence(tmp_path):
    _, store, _, drives, revision = active_governance(tmp_path)
    owner = store.observer_projection(
        viewer_person_id="person-owner",
        owner_person_id="person-owner",
        audiences={"owner"},
        now=NOW,
    )
    stranger = store.observer_projection(
        viewer_person_id="person-stranger",
        owner_person_id="person-owner",
        audiences={"shared"},
        now=NOW,
    )
    assert owner["active_charter_revision_id"] == revision.revision_id
    assert owner["charter_revisions"][0]["lifecycle_status"] == "active"
    assert owner["drives"][0]["drive_id"] == drives[0].drive_id
    assert owner["charter_revisions"][0]["evidence_refs"] == [
        "directive:owner-charter"
    ]
    assert stranger["charter_revisions"] == []
    assert stranger["drives"] == []
