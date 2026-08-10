"""P6/P7 startup, scope, and HTTP attachment regression locks.

The feature modules deliberately shipped without shared server wiring.  These
tests pin the attachment boundary before it is implemented: off creates no
state, P6 shadow remains observer-only, P6 live composes (and never weakens)
P3 capacity, and P7 ranks only durable P3 projects using server authority.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.authority import required_scope
from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import host
from colony_sidecar.cognition.drive_governance import (
    DriveGovernance,
    DriveGovernanceStore,
    DriveRanker,
    DriveV1,
    ScopeV1,
)
from colony_sidecar.initiatives.approval_authority import (
    ApprovalAuthorityStore,
    ApprovalSubjectBinding,
    legacy_action_binding,
)
from colony_sidecar.projects.models import Project
from colony_sidecar.projects.store import ProjectStore
from colony_sidecar.self_model.situation import (
    SituationObservationV1,
    SituationReducer,
    SituationStore,
)
from colony_sidecar.self_model.workspace import ConcernStore
from colony_sidecar.server import (
    _attach_drive_governance,
    _attach_situation_spine,
    _capacity_plus_attachment_failure,
    _compose_p7_charter_admission,
)
from colony_sidecar.cognition.drive_governance import (
    CharterAdmissionConstraintsV1,
    ScopeV1,
)


NOW = datetime(2026, 7, 12, 16, 0, tzinfo=timezone.utc)


class FakeScheduler:
    def __init__(self):
        self.callbacks = {}

    def register(self, name, callback, interval_seconds, metadata=None):
        self.callbacks[name] = {
            "callback": callback,
            "interval_seconds": interval_seconds,
            "metadata": metadata or {},
        }
        return f"schedule:{name}"


class FakeProjectStore:
    def __init__(self, projects=()):
        self.projects = list(projects)

    def list_projects(self, status=None, limit=50):
        items = self.projects
        if status:
            items = [item for item in items if item.status == status]
        return items[:limit]

    def get_project(self, project_id):
        return next(
            (item for item in self.projects if item.id == project_id), None
        )


class FakeCognitionStore:
    def get_policy_decision(self, reference):
        return {"payload": {"decision_ref": reference}}


@pytest.fixture(autouse=True)
def restore_host_wiring():
    names = (
        "_situation_store",
        "_situation_reducer",
        "_drive_governance",
        "_drive_ranker",
        "_drive_project_store",
        "_cognition_spine",
    )
    originals = {name: getattr(host, name, None) for name in names}
    yield
    for name, value in originals.items():
        setattr(host, name, value)


def _p3(*, capacity=None, projects=()):
    capacity = capacity or (
        lambda _proposal, _concern: (True, "capacity_available")
    )
    project_store = FakeProjectStore(projects)
    project_engine = SimpleNamespace(store=project_store)
    return SimpleNamespace(
        _situation=capacity,
        store=FakeCognitionStore(),
        project_engine=project_engine,
    )


def test_default_off_creates_neither_p6_nor_p7_state(tmp_path, monkeypatch):
    monkeypatch.delenv("COLONY_SITUATION_SPINE", raising=False)
    monkeypatch.delenv("COLONY_DRIVE_GOVERNANCE_MODE", raising=False)

    assert _attach_situation_spine(
        state_dir=tmp_path,
        cognition_spine=None,
        scheduler=None,
    ) is None
    assert _attach_drive_governance(
        state_dir=tmp_path,
        cognition_spine=None,
        workspace=None,
        project_store=None,
        directive_manager=None,
        approval_authority=None,
    ) is None
    assert not (tmp_path / "colony-situation.db").exists()
    assert not (tmp_path / "cognition-drive-governance.db").exists()
    assert not (tmp_path / "approval_authority.db").exists()


def test_p6_shadow_is_periodic_observer_and_never_replaces_p3_validator(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_SITUATION_SPINE", "shadow")
    monkeypatch.setattr(
        SituationReducer,
        "run_once",
        lambda self, limit=100: {
            "enabled": True,
            "mode": "shadow",
            "processed": 0,
        },
    )
    capacity = lambda _proposal, _concern: (True, "capacity_available")
    p3 = _p3(capacity=capacity)
    scheduler = FakeScheduler()

    wiring = _attach_situation_spine(
        state_dir=tmp_path,
        cognition_spine=p3,
        scheduler=scheduler,
    )

    assert wiring is not None
    assert p3._situation is capacity
    assert host._situation_store is wiring["store"]
    assert host._situation_reducer is wiring["reducer"]
    assert "situation" in host.supported_capabilities()
    assert "situation_reduce" in scheduler.callbacks
    assert scheduler.callbacks["situation_reduce"]["interval_seconds"] >= 5
    assert scheduler.callbacks["situation_reduce"]["callback"]()["mode"] == \
        "shadow"


@pytest.mark.asyncio
async def test_p6_periodic_reducer_ingests_real_queue_resource_observation(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_SITUATION_SPINE", "shadow")
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    monkeypatch.setattr(
        SituationReducer,
        "run_once",
        lambda self, limit=100: {
            "enabled": True,
            "mode": "shadow",
            "processed": 0,
        },
    )

    class Queue:
        def execution_readiness(self):
            return {
                "ready": True,
                "reason": "scheduler_ready",
                "routing_ready": True,
                "routing_reason": "agent_action_routes_ready",
                "typed_routes": {},
            }

        async def get_queue_stats(self):
            return SimpleNamespace(
                registered_workers=1,
                active_workers=1,
                stale_workers=0,
                available_workers=1,
                worker_heartbeat_ttl_secs=60.0,
            )

    scheduler = FakeScheduler()
    wiring = _attach_situation_spine(
        state_dir=tmp_path,
        cognition_spine=_p3(),
        scheduler=scheduler,
        task_queue=SimpleNamespace(queue=Queue()),
    )

    result = await scheduler.callbacks[
        "situation_reduce"
    ]["callback"]()
    snapshot = wiring["store"].snapshot(
        subject_person_id="person-owner",
        viewer_scope="owner",
    )

    assert result["resource_observation"]["disposition"] == "applied"
    assert result["resource_observation"]["state"] == "available"
    resource = snapshot.active_facts("resource")
    assert len(resource) == 1
    assert resource[0].entity_id == "task-queue-execution"
    assert dict(resource[0].attributes)["capacity_available"] is True


def test_p6_scheduler_registration_failure_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_SITUATION_SPINE", "live")
    monkeypatch.setattr(
        SituationReducer,
        "run_once",
        lambda self, limit=100: {
            "enabled": True, "mode": "live", "processed": 0,
        },
    )
    capacity = lambda _proposal, _concern: (True, "capacity_available")
    p3 = _p3(capacity=capacity)

    class BrokenScheduler:
        def register(self, *_args, **_kwargs):
            raise RuntimeError("schedule store unavailable")

    with pytest.raises(RuntimeError, match="schedule store unavailable"):
        _attach_situation_spine(
            state_dir=tmp_path,
            cognition_spine=p3,
            scheduler=BrokenScheduler(),
        )

    assert p3._situation is capacity
    assert host._situation_store is None
    assert host._situation_reducer is None


def test_p6_live_composes_capacity_and_fails_closed_on_reducer_or_gate_error(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_SITUATION_SPINE", "live")
    monkeypatch.setattr(
        SituationReducer,
        "run_once",
        lambda self, limit=100: {
            "enabled": True,
            "mode": "live",
            "processed": 0,
        },
    )
    calls = []

    def capacity(proposal, concern):
        calls.append((proposal, concern))
        return {"allowed": proposal.objective != "blocked", "reason": "capacity"}

    p3 = _p3(capacity=capacity)
    wiring = _attach_situation_spine(
        state_dir=tmp_path,
        cognition_spine=p3,
        scheduler=None,
    )
    concern = SimpleNamespace(subject_person_id="person-owner")
    blocked = SimpleNamespace(
        objective="blocked",
        subject_person_id="person-owner",
        viewer_scope="owner",
    )
    allowed = SimpleNamespace(
        objective="allowed",
        subject_person_id="person-owner",
        viewer_scope="owner",
    )

    assert p3._situation(blocked, concern) == {
        "allowed": False,
        "reason": "capacity",
    }

    wiring["reducer"].run_once = lambda limit=100: {
        "enabled": True,
        "error": "journal_unavailable",
    }
    reducer_denial = p3._situation(allowed, concern)
    assert reducer_denial["allowed"] is False
    assert reducer_denial["reason"] == "situation_reducer_unhealthy"

    wiring["reducer"].run_once = lambda limit=100: {
        "enabled": True,
        "processed": 0,
    }
    wiring["gate"].for_goal_proposal = lambda *_args, **_kwargs: 1 / 0
    gate_denial = p3._situation(allowed, concern)
    assert gate_denial["allowed"] is False
    assert gate_denial["reason"] == "situation_gate_failed_closed"
    assert len(calls) == 3


def test_p6_attachment_failure_gate_preserves_denial_and_holds_capacity_allow():
    def capacity(proposal, _concern):
        return (
            (False, "capacity_exhausted")
            if proposal.objective == "blocked"
            else (True, "capacity_available")
        )

    validator = _capacity_plus_attachment_failure(
        capacity, "situation_attachment_failed_closed",
    )
    blocked = validator(SimpleNamespace(objective="blocked"), object())
    held = validator(SimpleNamespace(objective="allowed"), object())

    assert blocked == (False, "capacity_exhausted")
    assert held["allowed"] is False
    assert held["reason"] == "situation_attachment_failed_closed"


def test_p6_and_p7_stores_have_explicit_idempotent_close(tmp_path):
    situation = SituationStore(str(tmp_path / "situation.db"))
    drives = DriveGovernanceStore(tmp_path / "drives.db")
    situation.close()
    situation.close()
    drives.close()
    drives.close()


def test_p7_attachment_requires_p3_and_reuses_canonical_approval_store(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_DRIVE_GOVERNANCE_MODE", "shadow")
    shared = ApprovalAuthorityStore(tmp_path / "approval_authority.db")
    cognition = _p3(projects=())
    project_store = cognition.project_engine.store
    workspace = SimpleNamespace()
    directives = object()

    wiring = _attach_drive_governance(
        state_dir=tmp_path,
        cognition_spine=cognition,
        workspace=workspace,
        project_store=project_store,
        directive_manager=directives,
        approval_authority=shared,
    )

    assert wiring["governance"].approval_store is shared
    assert wiring["ranker"]._resolve_policy.__self__ is cognition.store
    assert wiring["ranker"]._directives is directives
    assert workspace.drive_governance is wiring["governance"]
    assert workspace.drive_ranker is wiring["ranker"]
    assert host._drive_project_store is project_store
    assert "drive_governance" in host.supported_capabilities()

    bad_state = tmp_path / "bad"
    with pytest.raises(RuntimeError, match="missing durable dependencies"):
        _attach_drive_governance(
            state_dir=bad_state,
            cognition_spine=None,
            workspace=workspace,
            project_store=project_store,
            directive_manager=directives,
            approval_authority=shared,
        )
    assert not (bad_state / "cognition-drive-governance.db").exists()


def test_p7_bootstrap_attaches_authority_without_replacing_shadow_p3_validator(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_DRIVE_GOVERNANCE_MODE", "bootstrap")
    cognition = _p3(projects=())
    original = cognition._charter = lambda _proposal, _concern: (
        True, "shadow_typed_goal"
    )
    workspace = SimpleNamespace()

    wiring = _attach_drive_governance(
        state_dir=tmp_path,
        cognition_spine=cognition,
        workspace=workspace,
        project_store=cognition.project_engine.store,
        directive_manager=object(),
        approval_authority=None,
    )

    assert wiring["mode"] == "bootstrap"
    assert wiring["governance"].approval_store is not None
    assert wiring["governance"].approval_store.path == \
        tmp_path / "approval_authority.db"
    assert cognition._charter is original


def test_live_p7_charter_narrows_p3_admission_and_cites_ratification():
    owner_scope = ScopeV1("person-owner", "owner", "owner_private")
    public_scope = ScopeV1("person-owner", "public", "public")
    active = SimpleNamespace(
        revision_id="charter:default:ratified",
        scope=owner_scope,
        evidence_refs=("approval:owner-ratification",),
    )
    store = SimpleNamespace(active_revision=lambda _key="default": active)
    base = lambda _proposal, _concern: {
        "allowed": True,
        "reason": "typed_goal_with_source_evidence",
        "evidence_refs": ["event:1"],
    }
    validator = _compose_p7_charter_admission(base, store)

    accepted = validator(SimpleNamespace(
        subject_person_id="person-owner",
        viewer_scope="owner",
        shareability="owner_private",
    ), object())
    denied = validator(SimpleNamespace(
        subject_person_id="person-owner",
        viewer_scope="public",
        shareability="public",
    ), object())

    assert accepted["allowed"] is True
    assert accepted["reason"] == "active_owner_ratified_charter"
    assert accepted["evidence_refs"] == [
        "charter:default:ratified",
        "charter-active:charter:default:ratified",
        "approval:owner-ratification", "event:1",
    ]
    assert denied["allowed"] is False
    assert denied["reason"] == "active_charter_scope_holds_goal"

    store.active_revision = lambda _key="default": None
    missing = validator(SimpleNamespace(
        subject_person_id="person-owner",
        viewer_scope="owner",
        shareability="owner_private",
    ), object())
    assert missing == {
        "allowed": False,
        "reason": "active_owner_ratified_charter_required",
        "evidence_refs": ["event:1"],
    }


def test_ratified_charter_constraints_deny_dangerous_goal_without_explicit_ceiling():
    constraints = CharterAdmissionConstraintsV1(
        objective_allow_terms=("diagnose", "repair"),
        objective_deny_terms=("destroy", "wipe"),
        capability_ceiling=("messaging:send", "reasoning", "root:shell"),
        capability_deny=(),
        required_boundary_refs=("directive:safety",),
        allowed_shareability=("owner_private",),
        allowed_recipient_ids=(),
        allow_destructive=False,
        allow_root_shell=False,
        allow_messaging=False,
    )
    active = SimpleNamespace(
        revision_id="charter:default:constrained",
        scope=ScopeV1("person-owner", "owner", "owner_private"),
        evidence_refs=("approval:owner-ratification",),
        admission_constraints=constraints,
    )
    store = SimpleNamespace(active_revision=lambda _key="default": active)
    directives = SimpleNamespace(active=lambda: [
        SimpleNamespace(id="directive:safety"),
    ])
    validator = _compose_p7_charter_admission(
        lambda *_args: (True, "base_allowed"), store, directives,
    )
    destructive = SimpleNamespace(
        title="Destroy host",
        objective="Destroy the host and wipe its data",
        evidence_refs=("event:1",),
        required_capabilities=("reasoning", "root:shell", "messaging:send"),
        subject_person_id="person-owner",
        viewer_scope="owner",
        shareability="owner_private",
    )
    safe = SimpleNamespace(
        title="Diagnose service",
        objective="Diagnose the service and prepare a reversible repair",
        evidence_refs=("event:1",),
        required_capabilities=("reasoning",),
        subject_person_id="person-owner",
        viewer_scope="owner",
        shareability="owner_private",
    )

    denied = validator(destructive, object())
    accepted = validator(safe, object())
    assert denied["allowed"] is False
    assert denied["reason"] == "charter_objective_explicitly_denied"
    assert accepted["allowed"] is True

    active.admission_constraints = CharterAdmissionConstraintsV1(
        objective_allow_terms=("destroy",),
        objective_deny_terms=(),
        capability_ceiling=("messaging:send", "reasoning", "root:shell"),
        capability_deny=(),
        required_boundary_refs=("directive:safety",),
        allowed_shareability=("owner_private",),
        allowed_recipient_ids=(),
        allow_destructive=True,
        allow_root_shell=True,
        allow_messaging=True,
    )
    still_denied = validator(destructive, object())
    assert still_denied["allowed"] is False
    assert still_denied["reason"] == "charter_recipient_envelope_required"

    directives.active = lambda: []
    missing_boundary = validator(safe, object())
    assert missing_boundary["allowed"] is False
    assert missing_boundary["reason"] == "charter_required_boundary_missing"


@pytest.mark.parametrize(
    ("method", "path", "scope"),
    [
        ("GET", "/v1/host/self/situation", "cognition:read"),
        ("GET", "/v1/host/cognition/drives", "drives:read"),
        ("GET", "/v1/host/cognition/charters", "charter:read"),
        ("GET", "/v1/host/cognition/rankings", "drives:read"),
        ("GET", "/v1/host/cognition/spine", "cognition:read"),
        (
            "POST",
            "/v1/host/cognition/concerns/concern-1/promote",
            "cognition:manage",
        ),
        (
            "POST",
            "/v1/host/cognition/goals/goal-proposal:abc/promote",
            "cognition:manage",
        ),
        ("POST", "/v1/host/cognition/drives", "drives:propose"),
        ("POST", "/v1/host/cognition/drive-signals", "drives:signal"),
        ("POST", "/v1/host/cognition/charters", "charter:propose"),
        (
            "POST",
            "/v1/host/cognition/charters/charter:default:abc/request-activation",
            "charter:request",
        ),
        (
            "POST",
            "/v1/host/cognition/charters/charter:default:abc/request-revocation",
            "charter:request",
        ),
        (
            "POST",
            "/v1/host/cognition/charters/charter:default:abc/ratify",
            "charter:ratify",
        ),
        (
            "GET",
            "/v1/host/cognition/charter-transition-approvals/readiness",
            "charter:approval-read",
        ),
        (
            "GET",
            "/v1/host/cognition/charter-transition-approvals/apr_abc",
            "charter:approval-read",
        ),
        (
            "POST",
            "/v1/host/cognition/charter-transition-approvals/apr_abc/decision",
            "charter:approval-decide",
        ),
        ("GET", "/v1/host/autonomy/posture", "cognition:read"),
        ("GET", "/v1/host/autonomy/schedule", "cognition:read"),
        ("GET", "/v1/host/autonomy/status", "cognition:read"),
    ],
)
def test_exact_p6_p7_route_scopes(method, path, scope):
    assert required_scope(method, path) == scope


def _principal(
    principal,
    secret,
    scopes,
    *,
    audiences=("owner",),
    viewer="person-owner",
    allow_unscoped_api=True,
):
    return {
        "principal": principal,
        "status": "active",
        "scopes": list(scopes),
        "allow_unscoped_api": allow_unscoped_api,
        "viewer_person_id": viewer,
        "person_ids": [viewer],
        "audiences": list(audiences),
        "credentials": [
            {"id": "current", "secret": secret, "status": "active"}
        ],
    }


def _app(tmp_path, principals):
    keyring = tmp_path / "api-keyring.json"
    keyring.write_text(json.dumps({"version": 1, "principals": principals}))
    keyring.chmod(0o600)
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, keyring_path=str(keyring))
    app.include_router(host.router)
    return app


def _headers(secret, principal):
    return {
        "Authorization": f"Bearer {secret}",
        "X-Colony-Principal": principal,
    }


@pytest.mark.asyncio
async def test_restricted_cognition_reader_can_probe_only_exact_autonomy_reads(
    tmp_path,
):
    app = _app(tmp_path, [
        _principal(
            "restricted-cognition-reader",
            "restricted-cognition-key",
            ["api:access", "cognition:read"],
            allow_unscoped_api=False,
        ),
        _principal(
            "missing-cognition-reader",
            "missing-cognition-key",
            ["api:access"],
            allow_unscoped_api=False,
        ),
    ])
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as client:
        exact = [
            await client.get(
                path,
                headers=_headers(
                    "restricted-cognition-key", "restricted-cognition-reader",
                ),
            )
            for path in (
                "/v1/host/autonomy/posture",
                "/v1/host/autonomy/schedule",
                "/v1/host/autonomy/status",
            )
        ]
        missing = await client.get(
            "/v1/host/autonomy/status",
            headers=_headers(
                "missing-cognition-key", "missing-cognition-reader",
            ),
        )
        fallback = await client.get(
            "/v1/host/goals",
            headers=_headers(
                "restricted-cognition-key", "restricted-cognition-reader",
            ),
        )

    assert [response.status_code for response in exact] == [200, 200, 200]
    assert missing.status_code == 403
    assert missing.json()["detail"]["required_scope"] == "cognition:read"
    assert fallback.status_code == 403
    assert fallback.json()["detail"]["code"] == "unscoped_api_denied"


@pytest.mark.asyncio
async def test_cognition_health_uses_credential_viewer_and_promotion_is_owner_only(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    concerns = ConcernStore(str(tmp_path / "workspace.db"))
    concerns.initialize_event_cursor("host-test", 0, bootstrap_mode="replay")
    applied = concerns.apply_event(
        consumer_id="host-test",
        event_seq=1,
        event_id="event-shadow",
        event_type="service.degraded",
        material_digest="material-shadow-v1",
        projection={
            "operation": "upsert",
            "kind": "maintenance",
            "summary": "inspect shadow evidence",
            "salience": 0.8,
            "dedup_key": "service:shadow",
            "sources": ["journal:1:event-shadow"],
            "subject_person_id": "person-owner",
            "viewer_scope": "owner",
            "shareability": "owner_private",
            "occurred_at": NOW.isoformat(),
            "producer_name": "event_concerns",
            "producer_mode": "shadow",
            "producer_revision": "event-reducer:v2",
        },
    )
    calls = []

    def health_snapshot(**kwargs):
        calls.append(kwargs)
        return {
            "available": True,
            "healthy": True,
            "runtime": {"effective_mode": "live"},
            "read_trace": [],
            "routed_outputs": [],
        }

    host.set_cognition_spine(SimpleNamespace(
        concern_store=concerns,
        health_snapshot=health_snapshot,
    ))
    app = _app(tmp_path, [
        _principal(
            "owner-operator", "owner-key",
            ["cognition:read", "cognition:manage"],
        ),
        _principal(
            "guest-reader", "guest-key", ["cognition:read"],
            audiences=("global",), viewer="person-guest",
        ),
    ])

    path = f"/v1/host/cognition/concerns/{applied['concern_id']}/promote"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        health = await client.get(
            "/v1/host/cognition/spine",
            headers=_headers("guest-key", "guest-reader"),
        )
        guest = await client.post(
            path,
            headers=_headers("guest-key", "guest-reader"),
            json={"expected_material_digest": "material-shadow-v1"},
        )
        owner = await client.post(
            path,
            headers=_headers("owner-key", "owner-operator"),
            json={"expected_material_digest": "material-shadow-v1"},
        )
        replay = await client.post(
            path,
            headers=_headers("owner-key", "owner-operator"),
            json={"expected_material_digest": "material-shadow-v1"},
        )

    assert health.status_code == 200
    assert calls[0]["viewer_person_id"] == "person-guest"
    assert calls[0]["audiences"] == frozenset({"global"})
    assert guest.status_code == 403
    assert owner.status_code == replay.status_code == 200
    assert owner.json()["concern"]["promotion_ref"] == replay.json()["concern"][
        "promotion_ref"
    ]
    assert owner.json()["effect_executed"] is False


@pytest.mark.asyncio
async def test_cognition_health_reports_failed_attachment_and_empty_catalog(
    tmp_path,
):
    host.set_cognition_spine(None, attachment_status={
        "configured_mode": "live",
        "state": "failed",
        "reason": "missing durable dependency",
        "configured_handler_catalog": ["thought"],
        "effective_handler_catalog": [],
    })
    app = _app(tmp_path, [
        _principal("owner-reader", "owner-key", ["cognition:read"]),
    ])
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/host/cognition/spine",
            headers=_headers("owner-key", "owner-reader"),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["healthy"] is False
    assert body["attachment"]["configured_handler_catalog"] == ["thought"]
    assert body["attachment"]["effective_handler_catalog"] == []


@pytest.mark.asyncio
async def test_shadow_goal_promotion_derives_exact_owner_operation(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    calls = []

    class Spine:
        async def promote_goal_proposal(self, proposal_id, **kwargs):
            calls.append((proposal_id, kwargs))
            return {
                "status": "project_created",
                "goal_proposal_id": proposal_id,
                "project_id": "proj-promoted",
                "promotion_ref": kwargs["promotion_ref"],
                "effect_executed": False,
            }

    host.set_cognition_spine(Spine())
    app = _app(tmp_path, [
        _principal(
            "owner-operator", "owner-key", ["cognition:manage"],
        ),
        _principal(
            "guest-reader", "guest-key", ["cognition:read"],
            audiences=("global",), viewer="person-guest",
        ),
    ])
    path = "/v1/host/cognition/goals/goal-proposal:abc/promote"
    body = {"expected_thought_result_ref": "thought-result:abc"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        guest = await client.post(
            path, headers=_headers("guest-key", "guest-reader"), json=body,
        )
        owner = await client.post(
            path, headers=_headers("owner-key", "owner-operator"), json=body,
        )
        replay = await client.post(
            path, headers=_headers("owner-key", "owner-operator"), json=body,
        )

    assert guest.status_code == 403
    assert owner.status_code == replay.status_code == 200
    assert calls[0][0] == "goal-proposal:abc"
    assert calls[0][1]["promotion_ref"] == calls[1][1]["promotion_ref"]
    assert calls[0][1]["promotion_ref"].startswith("owner-goal-promotion:")


@pytest.mark.asyncio
async def test_situation_read_is_credential_viewer_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    store = SituationStore(str(tmp_path / "situation.db"))
    store.ingest(SituationObservationV1.create(
        observation_id="obs-owner-service",
        category="service",
        entity_id="gateway",
        state="healthy",
        active=True,
        observed_at=NOW.timestamp(),
        ttl_seconds=300,
        evidence_refs=("health:gateway:1",),
        source_kind="service_probe",
        subject_person_id="person-owner",
        viewer_scope="owner",
        shareability="owner_private",
    ))
    host.set_situation_spine(store, SimpleNamespace(
        status=lambda: {"enabled": True, "mode": "shadow", "healthy": True}
    ))
    app = _app(tmp_path, [
        _principal("situation-reader", "reader-key", ["cognition:read"]),
    ])

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        visible = await client.get(
            "/v1/host/self/situation",
            headers=_headers("reader-key", "situation-reader"),
        )
        broadened = await client.get(
            "/v1/host/self/situation?person_id=person-stranger",
            headers=_headers("reader-key", "situation-reader"),
        )

    assert visible.status_code == 200
    assert visible.json()["snapshot"]["viewer_scope"] == "owner"
    assert visible.json()["snapshot"]["facts"][0]["entity_id"] == "gateway"
    assert broadened.status_code == 403


class RecordingRanker:
    def __init__(self):
        self.goals = None

    def rank(self, goals, *, mode=None, now=None):
        self.goals = tuple(goals)

        class Batch:
            def observer_projection(self, **_viewer):
                return {
                    "schema": "RankingBatchObserverV1",
                    "results": [goal.payload() for goal in goals],
                }

        return Batch()


@pytest.mark.asyncio
async def test_rankings_are_built_only_from_durable_p3_projects(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    p3 = Project(
        id="project-p3",
        title="Verified P3 goal",
        objective="Produce the verified durable result",
        source="cognition_spine",
        status="active",
        goal_proposal_id="goal-proposal:p3",
        goal_fingerprint="fingerprint-p3",
        evidence_refs=["event:p3"],
        policy_decision_refs=[
            f"policy-decision:p3:{stage}" for stage in (
                "charter", "boundary", "situation", "duplicate", "authority"
            )
        ],
        subject_person_id="person-owner",
        viewer_scope="owner",
        shareability="owner_private",
    )
    legacy = Project(
        id="project-owner",
        title="Legacy owner project",
        objective="Must never enter drive ranking",
        source="owner",
        status="active",
    )
    malformed = Project(
        id="project-malformed",
        title="Incomplete P3 row",
        objective="Missing immutable provenance",
        source="cognition_spine",
        status="active",
        subject_person_id="person-owner",
    )
    ranker = RecordingRanker()
    host.set_drive_governance(
        SimpleNamespace(mode="shadow", store=SimpleNamespace()),
        ranker,
        FakeProjectStore([legacy, malformed, p3]),
    )
    app = _app(tmp_path, [
        _principal("drive-reader", "drive-reader-key", ["drives:read"]),
    ])

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/host/cognition/rankings",
            headers=_headers("drive-reader-key", "drive-reader"),
        )

    assert response.status_code == 200
    assert [goal.goal_id for goal in ranker.goals] == ["project-p3"]
    goal = ranker.goals[0]
    assert goal.proposal_id == p3.goal_proposal_id
    assert goal.goal_fingerprint == p3.goal_fingerprint
    assert goal.scope.subject_person_id == "person-owner"


@pytest.mark.asyncio
async def test_drive_proposal_scope_and_actor_are_server_derived(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    governance = DriveGovernance(
        DriveGovernanceStore(tmp_path / "drives.db"),
        ApprovalAuthorityStore(tmp_path / "approval_authority.db"),
        mode="shadow",
    )
    host.set_drive_governance(
        governance,
        DriveRanker(
            governance.store,
            policy_decision_resolver=lambda _reference: None,
            directive_manager=object(),
        ),
        FakeProjectStore(),
    )
    app = _app(tmp_path, [
        _principal("drive-proposer", "proposer-key", ["drives:propose"]),
    ])
    payload = {
        "operation_id": "drive-operation-api-0001",
        "key": "verified_progress",
        "version": "v1",
        "title": "Verified progress",
        "definition_summary": "Prefer reversible evidence-backed progress",
        "max_abs_contribution": 0.5,
        "max_signals_per_goal": 3,
        "state": "enabled",
        "evidence_refs": ["directive:owner-charter"],
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/host/cognition/drives",
            headers=_headers("proposer-key", "drive-proposer"),
            json=payload,
        )
        spoofed = await client.post(
            "/v1/host/cognition/drives",
            headers=_headers("proposer-key", "drive-proposer"),
            json={
                **payload,
                "operation_id": "drive-operation-api-0002",
                "scope": {
                    "subject_person_id": "person-stranger",
                    "viewer_scope": "public",
                    "shareability": "public",
                },
                "proposed_by": "body-owner",
            },
        )

    assert created.status_code == 200
    stored = governance.store.list_drives()[0]
    assert stored.scope.subject_person_id == "person-owner"
    assert stored.scope.viewer_scope == "owner"
    assert stored.scope.shareability == "owner_private"
    assert spoofed.status_code == 422
    assert len(governance.store.list_drives()) == 1


class RecordingGovernance:
    mode = "live"
    store = SimpleNamespace()

    def __init__(self):
        self.calls = []

    def ratify_transition(self, revision_id, **kwargs):
        self.calls.append((revision_id, kwargs))
        return {"status": "charter_activated", "revision_id": revision_id}


@pytest.mark.asyncio
async def test_ratify_requires_both_route_scope_and_owner_decision_authority(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    governance = RecordingGovernance()
    host.set_drive_governance(governance, RecordingRanker(), FakeProjectStore())
    app = _app(tmp_path, [
        _principal(
            "ratifier-limited",
            "ratifier-limited-key",
            ["charter:ratify"],
        ),
        _principal(
            "ratifier-owner",
            "ratifier-owner-key",
            ["charter:ratify", "approvals:decide"],
        ),
    ])
    payload = {
        "transition": "activate",
        "approval_request_id": "approval-request-0001",
        "operation_id": "ratify-operation-api-0001",
    }
    path = "/v1/host/cognition/charters/charter:default:abc/ratify"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.post(
            path,
            headers=_headers("ratifier-limited-key", "ratifier-limited"),
            json=payload,
        )
        allowed = await client.post(
            path,
            headers=_headers("ratifier-owner-key", "ratifier-owner"),
            json=payload,
        )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert len(governance.calls) == 1
    authority = governance.calls[0][1]["authority"]
    assert authority.principal_id == "ratifier-owner"
    assert authority.has_scope("approvals:decide")
    assert "owner" in authority.audiences


class AllowingDirectives:
    def check(self, _action):
        return SimpleNamespace(allowed=True, reason="no_active_boundaries")


@pytest.mark.asyncio
async def test_live_p7_http_flow_is_proposal_approval_and_ranking_only(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    project_store = ProjectStore(str(tmp_path / "projects.db"))
    approval_store = ApprovalAuthorityStore(tmp_path / "approval_authority.db")
    drive_store = DriveGovernanceStore(tmp_path / "drives.db")
    governance = DriveGovernance(drive_store, approval_store, mode="live")
    proposal_id = "goal-proposal:api-flow"
    policy_rows = {}
    refs = []
    for stage in ("charter", "boundary", "situation", "duplicate", "authority"):
        reference = f"policy-decision:api-flow:{stage}"
        refs.append(reference)
        policy_rows[reference] = {
            "payload": {
                "decision_ref": reference,
                "proposal_id": proposal_id,
                "stage": stage,
                "allowed": True,
                "reason": f"{stage}_accepted",
                "evidence_refs": [f"gate-evidence:{stage}"],
            }
        }
    project_store.save_project(Project(
        id="project-api-flow",
        title="API flow goal",
        objective="Produce one verified API integration result",
        source="cognition_spine",
        status="active",
        goal_proposal_id=proposal_id,
        goal_fingerprint="fingerprint-api-flow",
        evidence_refs=["event:api-flow"],
        policy_decision_refs=refs,
        subject_person_id="person-owner",
        viewer_scope="owner",
        shareability="owner_private",
    ))
    ranker = DriveRanker(
        drive_store,
        policy_decision_resolver=policy_rows.get,
        directive_manager=AllowingDirectives(),
    )
    host.set_drive_governance(governance, ranker, project_store)
    scopes = [
        "drives:read", "drives:propose", "drives:signal",
        "charter:read", "charter:propose", "charter:request",
        "charter:ratify", "approvals:decide",
    ]
    app = _app(tmp_path, [
        _principal("governance-owner", "governance-key", scopes),
        _principal(
            "subject-signal",
            "subject-signal-key",
            ["drives:signal"],
            audiences=("viewer",),
            viewer="person-stranger",
        ),
    ])
    headers = _headers("governance-key", "governance-owner")
    drive_payload = {
        "operation_id": "drive-api-flow-0001",
        "key": "verified_outcomes",
        "version": "v1",
        "title": "Verified outcomes",
        "definition_summary": "Prefer verified reversible outcomes",
        "max_abs_contribution": 0.5,
        "max_signals_per_goal": 3,
        "evidence_refs": ["directive:owner-charter"],
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        drive_response = await client.post(
            "/v1/host/cognition/drives", headers=headers, json=drive_payload,
        )
        assert drive_response.status_code == 200
        drive_id = drive_response.json()["drive"]["drive_id"]

        signal_payload = {
            "operation_id": "signal-api-flow-0001",
            "drive_id": drive_id,
            "project_id": "project-api-flow",
            "normalized_value": 0.8,
            "confidence": 0.9,
            "rationale_summary": "Verified receipt supports this project",
            "evidence_refs": ["receipt:api-flow"],
        }
        forged_signal = await client.post(
            "/v1/host/cognition/drive-signals",
            headers=headers,
            json={
                **signal_payload,
                "goal_fingerprint": "fingerprint-body-selected",
                "scope": {"viewer_scope": "public"},
            },
        )
        assert forged_signal.status_code == 422
        signal_response = await client.post(
            "/v1/host/cognition/drive-signals",
            headers=headers,
            json=signal_payload,
        )
        assert signal_response.status_code == 200
        assert signal_response.json()["signal"]["goal_fingerprint"] == \
            "fingerprint-api-flow"
        hidden_project = await client.post(
            "/v1/host/cognition/drive-signals",
            headers=_headers("subject-signal-key", "subject-signal"),
            json={
                **signal_payload,
                "operation_id": "signal-api-flow-hidden",
            },
        )
        assert hidden_project.status_code == 404

        charter_payload = {
            "operation_id": "charter-api-flow-0001",
            "revision_label": "owner-charter-api-v1",
            "title": "Owner charter",
            "purpose_summary": "Rank eligible goals toward verified outcomes",
            "principles": [
                "Respect explicit boundaries",
                "Prefer verified reversible progress",
            ],
            "drive_weights": {drive_id: 0.5},
            "evidence_refs": ["directive:owner-charter"],
        }
        forged_charter = await client.post(
            "/v1/host/cognition/charters",
            headers=headers,
            json={
                **charter_payload,
                "operation_id": "charter-api-flow-forged",
                "scope": {"viewer_scope": "public"},
                "proposed_by": "body-owner",
                "active_charter_revision_id": "body-selected-active-charter",
            },
        )
        assert forged_charter.status_code == 422
        charter_response = await client.post(
            "/v1/host/cognition/charters",
            headers=headers,
            json=charter_payload,
        )
        assert charter_response.status_code == 200
        charter = charter_response.json()["charter"]
        revision_id = charter["revision_id"]
        assert charter["proposed_by"] == "governance-owner"
        assert charter["scope"]["viewer_scope"] == "owner"

        requested = await client.post(
            f"/v1/host/cognition/charters/{revision_id}/request-activation",
            headers=headers,
            json={},
        )
        assert requested.status_code == 200
        approval = requested.json()
        assert approval["status"] == "pending"

        approval_store.decide(
            approval["request_id"],
            decision="approve",
            decision_id="decision-api-flow-0001",
            expected_action_digest=approval["action_digest"],
            decided_by="governance-owner",
            authority_evidence=(
                "scoped_principal:governance-owner:current"
            ),
        )
        ratified = await client.post(
            f"/v1/host/cognition/charters/{revision_id}/ratify",
            headers=headers,
            json={
                "transition": "activate",
                "approval_request_id": approval["request_id"],
                "operation_id": "ratify-api-flow-0001",
            },
        )
        assert ratified.status_code == 200
        assert ratified.json()["lifecycle_status"] == "active"

        drives = await client.get(
            "/v1/host/cognition/drives", headers=headers,
        )
        charters = await client.get(
            "/v1/host/cognition/charters", headers=headers,
        )
        rankings = await client.get(
            "/v1/host/cognition/rankings", headers=headers,
        )

    assert len(drives.json()["drives"]) == 1
    assert len(drives.json()["signals"]) == 1
    assert charters.json()["active_charter_revision_id"] == revision_id
    ranking = rankings.json()["ranking"]
    assert ranking["status"] == "ranked"
    assert ranking["ranking_applied"] is True
    assert ranking["effective_order"] == ["project-api-flow"]
    assert ranking["results"][0]["eligible"] is True
    assert ranking["results"][0]["authorization_effect"] == "none"


@pytest.mark.parametrize("governance_mode", ["bootstrap", "live"])
@pytest.mark.asyncio
async def test_owner_typed_charter_approval_http_contract_is_discoverable_and_atomic(
    tmp_path, monkeypatch, governance_mode,
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", "person-owner")
    monkeypatch.setenv("COLONY_APPROVAL_AUTHORITY_MODE", "enforce")
    approval_store = ApprovalAuthorityStore(tmp_path / "approval_authority.db")
    drive_store = DriveGovernanceStore(tmp_path / "drives.db")
    governance = DriveGovernance(
        drive_store, approval_store, mode=governance_mode,
    )
    host.set_drive_governance(
        governance,
        DriveRanker(
            drive_store,
            policy_decision_resolver=lambda _reference: None,
            directive_manager=AllowingDirectives(),
        ),
        FakeProjectStore(),
    )
    app = _app(tmp_path, [
        _principal(
            "charter-owner",
            "charter-owner-key",
            [
                "api:access", "drives:propose", "charter:propose",
                "charter:request", "charter:approval-read",
                "charter:approval-decide",
            ],
        ),
        _principal(
            "generic-queue-approver",
            "generic-approver-key",
            ["api:access", "approvals:read", "approvals:decide"],
        ),
        _principal(
            "non-owner-charter-reader",
            "shared-reader-key",
            ["api:access", "charter:approval-read"],
            audiences=("shared",),
            viewer="person-shared",
        ),
    ])
    headers = _headers("charter-owner-key", "charter-owner")
    drive = DriveV1.create(
        key="verified_outcomes",
        version="v1",
        title="Verified outcomes",
        definition_summary="Prefer verified reversible outcomes",
        max_abs_contribution=0.5,
        max_signals_per_goal=3,
        state="enabled",
        scope=ScopeV1("person-owner", "owner", "owner_private"),
        evidence_refs=("directive:typed-charter",),
        created_at=NOW,
    )
    governance.register_drive(
        drive, operation_id="typed-drive-operation-0001",
    )
    drive_id = drive.drive_id

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        charter_response = await client.post(
            "/v1/host/cognition/charters",
            headers=headers,
            json={
                "operation_id": "typed-charter-operation-0001",
                "revision_label": "typed-charter-v1",
                "title": "Owner typed charter",
                "purpose_summary": "Admit bounded verified owner goals",
                "principles": ["Prefer verified reversible progress"],
                "drive_weights": {drive_id: 0.5},
                "evidence_refs": ["directive:typed-charter"],
            },
        )
        assert charter_response.status_code == 200
        revision_id = charter_response.json()["charter"]["revision_id"]
        requested = await client.post(
            f"/v1/host/cognition/charters/{revision_id}/request-activation",
            headers=headers,
            json={},
        )
        assert requested.status_code == 200
        approval = requested.json()

        orphan_binding = legacy_action_binding(
            "charter_revision_activate",
            operation_id="orphan-http-charter-operation-0001",
            job_type="charter_governance",
            risk="authority_mutation",
        )
        approval_store.ensure_request(
            job_id=f"charter-transition:{orphan_binding.action_digest[:24]}",
            binding=orphan_binding,
            subject=ApprovalSubjectBinding(
                kind="charter_transition",
                subject_id="charter:missing:http",
                revision="e" * 64,
                action="activate",
            ),
        )

        denied_generic = await client.get(
            "/v1/host/cognition/charter-transition-approvals",
            headers=_headers("generic-approver-key", "generic-queue-approver"),
        )
        denied_audience = await client.get(
            "/v1/host/cognition/charter-transition-approvals",
            headers=_headers("shared-reader-key", "non-owner-charter-reader"),
        )
        readiness = await client.get(
            "/v1/host/cognition/charter-transition-approvals/readiness",
            headers=headers,
        )
        listed = await client.get(
            "/v1/host/cognition/charter-transition-approvals",
            headers=headers,
        )
        decided = await client.post(
            f"/v1/host/cognition/charter-transition-approvals/"
            f"{approval['request_id']}/decision",
            headers=headers,
            json={
                "decision": "approve",
                "decision_id": "decision-http-charter-0001",
                "expected_action_digest": approval["action_digest"],
                "expected_request_digest": approval["request_digest"],
            },
        )
        replay = await client.post(
            f"/v1/host/cognition/charter-transition-approvals/"
            f"{approval['request_id']}/decision",
            headers=headers,
            json={
                "decision": "approve",
                "decision_id": "decision-http-charter-0001",
                "expected_action_digest": approval["action_digest"],
                "expected_request_digest": approval["request_digest"],
            },
        )

    assert denied_generic.status_code == 403
    assert denied_audience.status_code == 403
    assert readiness.status_code == 200
    assert readiness.json()["schema"] == "ColonyCharterApprovalReadinessV1"
    assert readiness.json()["version"] == 1
    assert readiness.json()["status"] == "blocked"
    assert readiness.json()["mode"] == governance_mode
    assert readiness.json()["route_ready"] is True
    assert readiness.json()["pending_count"] == 1
    assert readiness.json()["invalid_hidden_count"] == 1
    assert "invalid_hidden_transition_approval" in readiness.json()["blockers"]
    assert listed.status_code == 200
    assert [item["request_id"] for item in listed.json()["requests"]] == [
        approval["request_id"],
    ]
    assert decided.status_code == 200
    assert decided.json()["status"] == "approved_applied"
    assert decided.json()["authority_evidence"] == \
        "scoped_principal:charter-owner:current"
    assert replay.status_code == 200
    assert replay.json()["application"]["event_id"] == \
        decided.json()["application"]["event_id"]
    assert drive_store.active_revision("default") is not None
    assert approval_store.list_grants() == []
