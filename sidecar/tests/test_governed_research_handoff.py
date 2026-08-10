"""Durable owner-governed research handoff and authority regression tests."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import time
from types import SimpleNamespace

from fastapi import Request
import pytest

from colony_sidecar.api.authority import RequestAuthority, legacy_authority
from colony_sidecar.api.routers import host
from colony_sidecar.governed_actions import (
    ColonySubsystemActionExecutor,
    GovernedActionLedger,
    GovernedActionService,
    GovernedActionValidationError,
    canonical_json,
    parse_execution_request,
    sha256_json,
)
from colony_sidecar.projects import Project, ProjectEngine, ProjectStore, Step
from colony_sidecar.work_orders import (
    QueueWorkOrderAdapter,
    WorkOrderV1,
    action_authority,
)


NOW = 1_900_000_000.0
OWNER = "person-owner-1"
CAPABILITIES = list(action_authority("research")[1])


def _authority() -> RequestAuthority:
    return RequestAuthority(
        principal_id="host-action-worker",
        credential_id="current",
        scopes=frozenset({"actions:execute", "actions:verify"}),
        allow_unscoped_api=False,
        viewer_person_id=OWNER,
        person_ids=frozenset({OWNER}),
        audiences=frozenset({"owner"}),
        authenticated=True,
        legacy=False,
    )


def _http_request(authority: RequestAuthority) -> Request:
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/v1/host/projects",
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 1),
        "root_path": "",
    })
    request.state.colony_authority = authority
    return request


def _guest_authority() -> RequestAuthority:
    return RequestAuthority(
        principal_id="guest-reader",
        credential_id="guest-key",
        scopes=frozenset({"api:access"}),
        allow_unscoped_api=True,
        viewer_person_id="person-guest",
        person_ids=frozenset({"person-guest"}),
        audiences=frozenset({"viewer"}),
        authenticated=True,
        legacy=False,
    )


def _owner_api_authority() -> RequestAuthority:
    return RequestAuthority(
        principal_id="owner-operator",
        credential_id="owner-key",
        scopes=frozenset({"api:access"}),
        allow_unscoped_api=True,
        viewer_person_id=OWNER,
        person_ids=frozenset({OWNER}),
        audiences=frozenset({"owner"}),
        authenticated=True,
        legacy=False,
    )


def _request(
    *,
    action_id: str = "123e4567-e89b-42d3-a456-426614174000",
    topic: str = "bounded agent execution",
    depth: str = "quick",
    tool_name: str = "colony_research",
) -> dict:
    args = {"topic": topic, "depth": depth} if tool_name == "colony_research" else {}
    approval = {
        "schema": "ColonyOwnerApprovalExecutionBindingV1",
        "version": 1,
        "approval_id": "APR-OWNER0000001",
        "decision_id": "DEC-OWNER-0001",
        "revision": 1,
        "authorization_receipt_sha256": "a" * 64,
        "decided_at": NOW - 5,
        "expires_at": NOW + 120,
    }
    unsigned = {
        "schema": "ColonyGovernedActionExecutionV1",
        "version": 1,
        "action_id": action_id,
        "action_digest": "b" * 64,
        "intent_id": "hti_" + "c" * 32,
        "intent_digest": "d" * 64,
        "tool_name": tool_name,
        "args": args,
        "args_sha256": sha256_json(args),
        "approval": approval,
    }
    return {**unsigned, "execution_digest": sha256_json(unsigned)}


def _host_execution_digest(unsigned: dict) -> str:
    encoded = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    import hashlib

    return hashlib.sha256(encoded).hexdigest()


def _adapter(store: ProjectStore) -> QueueWorkOrderAdapter:
    return QueueWorkOrderAdapter(SimpleNamespace(), project_store=store)


def _engine(path: Path, monkeypatch, *, mode: str = "live") -> ProjectEngine:
    monkeypatch.setenv("COLONY_PROJECTS_MODE", mode)
    store = ProjectStore(str(path))
    return ProjectEngine(store, work_order_adapter=_adapter(store))


def _expected_project_id(request: dict) -> str:
    return "proj-governed-" + _identity_digest(request)[:20]


def _identity_digest(request: dict) -> str:
    return sha256_json({
        "schema": "ColonyGovernedResearchProjectIdentityV1",
        "version": 1,
        "owner_person_id": OWNER,
        "action_id": request["action_id"],
        "action_digest": request["action_digest"],
        "execution_digest": request["execution_digest"],
    })


def _expected_goal_fingerprint(request: dict) -> str:
    return _identity_digest(request)


def _provenance(request: dict) -> set[str]:
    approval = request["approval"]
    return {
        "governed-action:" + request["action_id"],
        "governed-intent:" + request["intent_id"],
        "action-digest:" + request["action_digest"],
        "intent-digest:" + request["intent_digest"],
        "args-digest:" + request["args_sha256"],
        "execution-digest:" + request["execution_digest"],
        "approval:" + approval["approval_id"],
        "decision:" + approval["decision_id"],
        "approval-revision:" + str(approval["revision"]),
        "authorization-receipt-digest:"
        + approval["authorization_receipt_sha256"],
    }


@pytest.mark.asyncio
async def test_service_passes_one_full_immutable_request_to_executor(tmp_path):
    observed = []

    class Executor:
        async def prepare(self, request, owner_person_id):
            observed.append(("prepare", request, owner_person_id))
            with pytest.raises(TypeError):
                request["args"]["topic"] = "widened"
            with pytest.raises(TypeError):
                request["approval"]["decision_id"] = "forged"

        async def perform(self, request, owner_person_id):
            observed.append(("perform", request, owner_person_id))
            return {
                "schema": "ColonyGovernedActionEffectV1",
                "version": 1,
                "effect_id": "project-queued",
                "outcome": "queued",
                "verification": {"project_id": "project-queued"},
            }

    ledger = GovernedActionLedger(tmp_path / "private" / "ledger.db", clock=lambda: NOW)
    service = GovernedActionService(ledger, Executor(), clock=lambda: NOW)
    request = _request()
    result = await service.execute(
        request["action_id"], canonical_json(request).encode(), _authority()
    )

    assert result["status"] == "completed"
    assert [item[0] for item in observed] == ["prepare", "perform"]
    assert observed[0][1] is observed[1][1]
    assert observed[0][2] == observed[1][2] == OWNER
    assert set(observed[0][1]) == set(request)
    service.close()


@pytest.mark.parametrize("topic", ["café research", "memory and 🧠 research"])
def test_execution_digest_matches_host_utf8_envelope_for_non_ascii(topic):
    request = _request(topic=topic)
    unsigned = {
        key: value for key, value in request.items() if key != "execution_digest"
    }
    request["execution_digest"] = _host_execution_digest(unsigned)

    parsed = parse_execution_request(
        canonical_json(request).encode("utf-8"),
        path_action_id=request["action_id"],
    )
    assert parsed["args"]["topic"] == topic
    assert parsed["execution_digest"] == request["execution_digest"]


@pytest.mark.asyncio
async def test_research_topic_bound_is_lossless_at_1400_and_rejects_1401(
    tmp_path, monkeypatch
):
    engine = _engine(tmp_path / "projects.db", monkeypatch)
    service = GovernedActionService(
        GovernedActionLedger(
            tmp_path / "topic-ledger" / "ledger.db", clock=lambda: NOW
        ),
        ColonySubsystemActionExecutor(projects=engine),
        clock=lambda: NOW,
    )
    accepted = _request(topic="x" * 1400)
    accepted_result = await service.execute(
        accepted["action_id"], canonical_json(accepted).encode(), _authority()
    )
    project = engine.store.get_project(accepted_result["effect"]["effect_id"])
    assert accepted_result["status"] == "completed"
    assert project.objective == "Research topic:\n" + "x" * 1400 + "\nDepth: quick"

    rejected = _request(
        action_id="123e4567-e89b-42d3-a456-426614174002",
        topic="x" * 1401,
    )
    with pytest.raises(GovernedActionValidationError, match="topic"):
        await service.execute(
            rejected["action_id"], canonical_json(rejected).encode(), _authority()
        )
    assert engine.store.count() == 1
    assert service.ledger.get(rejected["action_id"]) is None
    service.close()
    engine.store.close()


@pytest.mark.asyncio
async def test_effect_digest_matches_host_utf8_result_contract(tmp_path):
    class Executor:
        async def prepare(self, request, owner_person_id):
            return None

        async def perform(self, request, owner_person_id):
            return {
                "schema": "ColonyGovernedActionEffectV1",
                "version": 1,
                "effect_id": "unicode-effect",
                "outcome": "completed",
                "verification": {"label": "café 🧠"},
            }

    service = GovernedActionService(
        GovernedActionLedger(
            tmp_path / "unicode" / "ledger.db", clock=lambda: NOW
        ),
        Executor(),
        clock=lambda: NOW,
    )
    request = _request()
    result = await service.execute(
        request["action_id"], canonical_json(request).encode(), _authority()
    )

    assert result["status"] == "completed"
    assert result["effect_digest"] == _host_execution_digest(result["effect"])
    assert result["effect_digest"] != sha256_json(result["effect"])
    service.close()


@pytest.mark.asyncio
async def test_governed_research_is_a_fast_durable_project_enqueue(tmp_path, monkeypatch):
    engine = _engine(tmp_path / "projects.db", monkeypatch)
    executor = ColonySubsystemActionExecutor(projects=engine)
    request = _request(topic="token=TOP-SECRET research topic", depth="deep")

    started = time.monotonic()
    await executor.prepare(request, OWNER)
    effect = await executor.perform(request, OWNER)
    elapsed = time.monotonic() - started

    project = engine.store.get_project(effect["effect_id"])
    assert elapsed < 0.5
    assert effect == {
        "schema": "ColonyGovernedActionEffectV1",
        "version": 1,
        "effect_id": _expected_project_id(request),
        "outcome": "queued",
        "verification": {
            "project_id": _expected_project_id(request),
            "status": "planning",
            "depth": "deep",
        },
    }
    assert project is not None
    assert project.source == "governed_action"
    assert project.status == "planning"
    assert project.outcome == "pending"
    assert project.subject_person_id == OWNER
    assert project.viewer_scope == "owner"
    assert project.shareability == "owner_private"
    assert project.capability_allowlist == CAPABILITIES
    assert project.title == "Governed research " + _identity_digest(request)[:8]
    assert "TOP-SECRET" not in project.title
    assert project.goal_fingerprint == _expected_goal_fingerprint(request)
    assert set(project.provenance_refs()) == _provenance(request)
    assert "TOP-SECRET" not in canonical_json(project.provenance_refs())
    engine.store.close()


@pytest.mark.asyncio
async def test_ephemeral_research_pipeline_is_never_called(tmp_path, monkeypatch):
    engine = _engine(tmp_path / "projects.db", monkeypatch)

    class PoisonPipeline:
        async def run(self, **_kwargs):
            await asyncio.sleep(60)
            raise AssertionError("ephemeral ResearchPipeline.run was called")

    executor = ColonySubsystemActionExecutor(projects=engine)
    executor.research = PoisonPipeline()
    request = _request()
    effect = await asyncio.wait_for(executor.perform(request, OWNER), timeout=0.5)

    assert effect["outcome"] == "queued"
    assert engine.store.count() == 1
    engine.store.close()


@pytest.mark.asyncio
async def test_governed_project_replay_restart_mismatch_and_new_action(
    tmp_path, monkeypatch
):
    path = tmp_path / "projects.db"
    engine = _engine(path, monkeypatch)
    executor = ColonySubsystemActionExecutor(projects=engine)
    request = _request()

    first_effect = await executor.perform(request, OWNER)
    first = engine.store.get_project(first_effect["effect_id"])
    original_created_at = first.created_at
    first.status = "active"
    first.replans = 2
    engine.store.save_project(first)
    replay_effect = await executor.perform(request, OWNER)
    replay = engine.store.get_project(replay_effect["effect_id"])
    assert first.id == replay.id == _expected_project_id(request)
    assert replay.created_at == original_created_at
    assert replay.status == "active"
    assert replay.replans == 2
    assert engine.store.count() == 1
    engine.store.close()

    restarted = _engine(path, monkeypatch)
    restarted_executor = ColonySubsystemActionExecutor(projects=restarted)
    after_restart_effect = await restarted_executor.perform(request, OWNER)
    after_restart = restarted.store.get_project(after_restart_effect["effect_id"])
    assert after_restart.id == first.id
    assert after_restart.created_at == original_created_at
    assert after_restart.status == "active"
    assert after_restart.replans == 2
    assert restarted.store.count() == 1

    other = _request(action_id="123e4567-e89b-42d3-a456-426614174001")
    other_effect = await restarted_executor.perform(other, OWNER)
    other_project = restarted.store.get_project(other_effect["effect_id"])
    assert other_project.id != first.id
    assert restarted.store.count() == 2

    with restarted.store._lock:
        restarted.store._conn.execute(
            "UPDATE projects SET objective=? WHERE id=?",
            ("tampered", first.id),
        )
        restarted.store._conn.commit()
    with pytest.raises(ValueError, match="replay mismatch"):
        await restarted_executor.perform(request, OWNER)
    restarted.store.close()


@pytest.mark.parametrize(
    ("field", "mutate"),
    [
        ("owner", lambda project: setattr(project, "subject_person_id", "person-other")),
        ("action_digest", lambda project: project.evidence_refs.__setitem__(0, "action-digest:" + "e" * 64)),
        ("execution_digest", lambda project: project.evidence_refs.__setitem__(-1, "execution-digest:" + "f" * 64)),
        ("depth", lambda project: setattr(project, "objective", project.objective.replace("Depth: quick", "Depth: deep"))),
        ("entity", lambda project: project.entity_ids.append("contact:guest")),
        ("provenance", lambda project: project.policy_decision_refs.append("decision:forged")),
    ],
)
@pytest.mark.asyncio
async def test_exact_insert_refuses_every_same_row_authority_mismatch(
    tmp_path, monkeypatch, field, mutate
):
    engine = _engine(tmp_path / "projects.db", monkeypatch)
    effect = await ColonySubsystemActionExecutor(projects=engine).perform(
        _request(), OWNER
    )
    project = engine.store.get_project(effect["effect_id"])
    candidate = copy.deepcopy(project)
    mutate(candidate)

    with pytest.raises(ValueError, match="replay mismatch"):
        engine.store.insert_authority_bound_project(candidate)
    persisted = engine.store.get_project(project.id)
    assert persisted == project, field
    engine.store.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "active"},
        {"status": "completed", "outcome": "succeeded"},
        {"status": "abandoned", "outcome": "failed"},
        {"reason": "already progressed"},
        {"replans": 1},
        {"next_review_at": 123.0},
    ],
)
@pytest.mark.asyncio
async def test_exact_insert_refuses_noninitial_absent_lifecycle(
    tmp_path, monkeypatch, changes
):
    engine = _engine(tmp_path / "projects.db", monkeypatch)
    effect = await ColonySubsystemActionExecutor(projects=engine).perform(
        _request(), OWNER
    )
    candidate = engine.store.get_project(effect["effect_id"])
    candidate.id += "-new"
    for field, value in changes.items():
        setattr(candidate, field, value)

    with pytest.raises(ValueError, match="initial planning lifecycle"):
        engine.store.insert_authority_bound_project(candidate)
    assert engine.store.get_project(candidate.id) is None
    engine.store.close()


@pytest.mark.asyncio
async def test_governed_project_authority_is_immutable(tmp_path, monkeypatch):
    engine = _engine(tmp_path / "projects.db", monkeypatch)
    effect = await ColonySubsystemActionExecutor(projects=engine).perform(
        _request(), OWNER
    )
    project = engine.store.get_project(effect["effect_id"])

    loaded = engine.store.get_project(project.id)
    loaded.source_event_refs.append("governed-action:forged")
    with pytest.raises(ValueError, match="immutable authority-bound project"):
        engine.store.save_project(loaded)

    loaded = engine.store.get_project(project.id)
    loaded.capability_allowlist.append("messaging:send")
    with pytest.raises(ValueError, match="immutable authority-bound project"):
        engine.store.save_project(loaded)
    engine.store.close()


@pytest.mark.parametrize("mode", ["off", "shadow"])
@pytest.mark.asyncio
async def test_nonlive_projects_mode_does_not_consume_approved_action(
    tmp_path, monkeypatch, mode
):
    engine = _engine(tmp_path / (mode + ".db"), monkeypatch, mode=mode)
    executor = ColonySubsystemActionExecutor(projects=engine)
    ledger = GovernedActionLedger(
        tmp_path / ("ledger-" + mode) / "ledger.db", clock=lambda: NOW
    )
    service = GovernedActionService(ledger, executor, clock=lambda: NOW)
    request = _request()

    result = await service.execute(
        request["action_id"], canonical_json(request).encode(), _authority()
    )
    replay = await service.execute(
        request["action_id"], canonical_json(request).encode(), _authority()
    )
    assert result == replay
    assert result["status"] == "failed"
    assert result["effect_state"] == "not_performed"
    assert ledger.get(request["action_id"])["state"] == "failed"
    assert engine.store.count() == 0
    service.close()
    engine.store.close()


@pytest.mark.asyncio
async def test_missing_canonical_work_order_adapter_does_not_consume_action(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COLONY_PROJECTS_MODE", "live")
    store = ProjectStore(str(tmp_path / "projects.db"))
    engine = ProjectEngine(store, work_order_adapter=object())
    executor = ColonySubsystemActionExecutor(projects=engine)
    ledger = GovernedActionLedger(tmp_path / "ledger" / "ledger.db", clock=lambda: NOW)
    service = GovernedActionService(ledger, executor, clock=lambda: NOW)
    request = _request()

    result = await service.execute(
        request["action_id"], canonical_json(request).encode(), _authority()
    )
    replay = await service.execute(
        request["action_id"], canonical_json(request).encode(), _authority()
    )
    assert result == replay
    assert result["status"] == "failed"
    assert result["effect_state"] == "not_performed"
    assert ledger.get(request["action_id"])["state"] == "failed"
    assert store.count() == 0
    service.close()
    store.close()


@pytest.mark.asyncio
async def test_governed_capabilities_narrow_initial_plan_replan_and_dispatch(
    tmp_path, monkeypatch
):
    engine = _engine(tmp_path / "projects.db", monkeypatch)
    request = _request()
    effect = await ColonySubsystemActionExecutor(projects=engine).perform(
        request, OWNER
    )
    project = engine.store.get_project(effect["effect_id"])

    async def directed_plan(*_args, **kwargs):
        return [Step(
            project_id=kwargs["project_id"], ordinal=1,
            description="write outside the read-only boundary",
            action_kind="directed",
        )]

    monkeypatch.setattr(
        "colony_sidecar.projects.planner.plan_project", directed_plan
    )
    assert await engine._plan_pending("live") == 0
    planned = engine.store.get_project(project.id)
    assert planned.status == "blocked"
    assert "goal_authority_missing" in planned.reason
    assert engine.store.steps_for(project.id) == []

    planned.status = "active"
    planned.reason = ""
    engine.store.save_project(planned)
    failed = Step(
        project_id=project.id, ordinal=1, description="failed read",
        action_kind="research", status="failed",
    )
    engine.store.save_step(failed)
    await engine._replan_remaining(planned, [failed], "live")
    replanned = engine.store.get_project(project.id)
    assert replanned.status == "blocked"
    assert "goal_authority_missing" in replanned.reason
    assert engine.store.steps_for(project.id) == [failed]

    with engine.store._lock:
        engine.store._conn.execute("DELETE FROM steps WHERE project_id=?", (project.id,))
        engine.store._conn.commit()
    replanned.status = "active"
    replanned.reason = ""
    engine.store.save_project(replanned)
    for kind in ("directed", "deliver"):
        with engine.store._lock:
            engine.store._conn.execute(
                "DELETE FROM steps WHERE project_id=?", (project.id,)
            )
            engine.store._conn.commit()
        replanned.status = "active"
        replanned.reason = ""
        engine.store.save_project(replanned)
        forged = Step(
            project_id=project.id, ordinal=1, description="forged mutation",
            action_kind=kind,
        )
        engine.store.save_step(forged)
        called = False

        async def execute(*_args, **_kwargs):
            nonlocal called
            called = True
            return True, "should not dispatch"

        engine._work_orders.execute = execute
        assert await engine._advance_project(replanned, "live") is False
        blocked = engine.store.get_project(project.id)
        assert blocked.status == "blocked"
        assert "goal_authority_missing" in blocked.reason
        assert called is False
        assert engine.store.steps_for(project.id)[0].attempts == 0
    engine.store.close()


@pytest.mark.asyncio
async def test_governed_research_work_order_is_owner_read_only(tmp_path, monkeypatch):
    engine = _engine(tmp_path / "projects.db", monkeypatch)
    effect = await ColonySubsystemActionExecutor(projects=engine).perform(
        _request(), OWNER
    )
    project = engine.store.get_project(effect["effect_id"])
    step = Step(
        project_id=project.id,
        ordinal=1,
        description="gather public sources",
        action_kind="research",
    )
    order = WorkOrderV1.for_project_step(
        project, step, context_refs=project.provenance_refs()
    )

    assert order.source == "governed_action"
    assert order.recipient_scope == "owner"
    assert list(order.capability_allowlist) == CAPABILITIES
    assert set(order.context_refs) == _provenance(_request())
    assert "directed" not in order.action_hint
    assert "deliver" not in order.action_hint
    engine.store.close()


@pytest.mark.asyncio
async def test_autonomy_results_are_prompt_and_match_observed_state():
    running = {"value": True}
    stop_requested = asyncio.Event()

    async def prompt_stop_signal():
        stop_requested.set()

    executor = ColonySubsystemActionExecutor(
        autonomy_enable=lambda: None,
        autonomy_disable=prompt_stop_signal,
        autonomy_running=lambda: running["value"],
    )
    disable_request = _request(tool_name="colony_autonomy_disable")
    started = time.monotonic()
    disable = await asyncio.wait_for(
        executor.perform(disable_request, OWNER), timeout=0.5
    )
    assert time.monotonic() - started < 0.5
    assert stop_requested.is_set()
    assert disable["outcome"] == "stop_requested"
    assert disable["verification"] == {"running": True}

    running["value"] = False
    disabled = await executor.perform(disable_request, OWNER)
    assert disabled["outcome"] == "disabled"
    assert disabled["verification"] == {"running": False}

    enable_request = _request(tool_name="colony_autonomy_enable")
    start_requested = await executor.perform(enable_request, OWNER)
    assert start_requested["outcome"] == "start_requested"
    assert start_requested["verification"] == {"running": False}


@pytest.mark.asyncio
async def test_governed_stop_wrapper_uses_prompt_loop_signal_not_host_join():
    import colony_sidecar.server as server

    class Loop:
        def __init__(self):
            self.requested = False

        async def stop(self):
            self.requested = True

    loop = Loop()
    host_route_called = False

    async def five_second_host_route():
        nonlocal host_route_called
        host_route_called = True
        await asyncio.sleep(5)

    started = time.monotonic()
    await asyncio.wait_for(
        server._governed_autonomy_stop_signal(loop), timeout=0.5
    )
    assert time.monotonic() - started < 0.5
    assert loop.requested is True
    assert host_route_called is False
    assert five_second_host_route is not None


@pytest.mark.asyncio
async def test_insert_then_executor_crash_is_ambiguous_and_never_duplicates(
    tmp_path, monkeypatch
):
    engine = _engine(tmp_path / "projects.db", monkeypatch)
    original = engine.enqueue_governed_research

    def insert_then_crash(project):
        original(project)
        raise RuntimeError("crash after exact project insert")

    engine.enqueue_governed_research = insert_then_crash
    executor = ColonySubsystemActionExecutor(projects=engine)
    ledger_path = tmp_path / "ledger" / "ledger.db"
    ledger = GovernedActionLedger(ledger_path, clock=lambda: NOW)
    service = GovernedActionService(ledger, executor, clock=lambda: NOW)
    request = _request()

    result = await service.execute(
        request["action_id"], canonical_json(request).encode(), _authority()
    )
    assert result["status"] == "ambiguous"
    assert engine.store.count() == 1
    service.close()

    restarted = GovernedActionService(
        GovernedActionLedger(ledger_path, clock=lambda: NOW),
        ColonySubsystemActionExecutor(projects=engine),
        clock=lambda: NOW,
    )
    replay = await restarted.execute(
        request["action_id"], canonical_json(request).encode(), _authority()
    )
    assert replay == result
    assert engine.store.count() == 1
    restarted.close()
    engine.store.close()


@pytest.mark.asyncio
async def test_mode_demotion_holds_governed_project_without_shadow_skip(
    tmp_path, monkeypatch
):
    engine = _engine(tmp_path / "projects.db", monkeypatch)
    effect = await ColonySubsystemActionExecutor(projects=engine).perform(
        _request(), OWNER
    )
    project = engine.store.get_project(effect["effect_id"])
    project.status = "active"
    engine.store.save_project(project)
    step = Step(
        project_id=project.id, ordinal=1, description="read sources",
        action_kind="research",
    )
    engine.store.save_step(step)
    monkeypatch.setenv("COLONY_PROJECTS_MODE", "shadow")

    report = await engine.tick()
    persisted = engine.store.get_project(project.id)
    persisted_step = engine.store.steps_for(project.id)[0]
    assert report["mode"] == "shadow"
    assert report["steps_dispatched"] == 0
    assert persisted.status == "active"
    assert persisted.reason == "governed_action_requires_live_projects_mode"
    assert persisted_step.status == "pending"

    resumed = False

    async def execute(*_args, **_kwargs):
        nonlocal resumed
        resumed = True
        return None, "work_order:queued"

    engine._work_orders.execute = execute
    monkeypatch.setenv("COLONY_PROJECTS_MODE", "live")
    assert await engine._advance_project(persisted, "live") is True
    assert resumed is True
    assert engine.store.get_project(project.id).reason == ""
    assert engine.store.steps_for(project.id)[0].status == "pending"
    engine.store.close()


@pytest.mark.asyncio
async def test_adapter_disappearance_cannot_fall_back_to_local_reasoning(
    tmp_path, monkeypatch
):
    engine = _engine(tmp_path / "projects.db", monkeypatch)
    effect = await ColonySubsystemActionExecutor(projects=engine).perform(
        _request(), OWNER
    )
    project = engine.store.get_project(effect["effect_id"])
    project.status = "active"
    engine.store.save_project(project)
    step = Step(
        project_id=project.id, ordinal=1, description="read sources",
        action_kind="research",
    )
    engine.store.save_step(step)
    reasoning_called = False

    class Reasoning:
        async def run_turn(self, **_kwargs):
            nonlocal reasoning_called
            reasoning_called = True
            raise AssertionError("local fallback must not run")

    engine._reasoning = Reasoning()
    engine._work_orders = None
    assert await engine._advance_project(project, "live") is False
    persisted = engine.store.get_project(project.id)
    assert persisted.status == "active"
    assert persisted.reason == "governed_action_requires_canonical_work_order_adapter"
    assert reasoning_called is False
    assert engine.store.steps_for(project.id)[0].status == "pending"

    resumed = False
    adapter = _adapter(engine.store)

    async def execute(*_args, **_kwargs):
        nonlocal resumed
        resumed = True
        return None, "work_order:queued"

    adapter.execute = execute
    engine._work_orders = adapter
    assert await engine._advance_project(persisted, "live") is True
    assert resumed is True
    assert engine.store.get_project(project.id).reason == ""
    assert reasoning_called is False
    engine.store.close()


@pytest.mark.asyncio
async def test_owner_private_project_routes_filter_guest_without_existence_leak(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", OWNER)
    engine = _engine(tmp_path / "projects.db", monkeypatch)
    effect = await ColonySubsystemActionExecutor(projects=engine).perform(
        _request(topic="TOP-SECRET private research"), OWNER
    )
    governed = engine.store.get_project(effect["effect_id"])
    guest_project = Project(
        title="Guest-visible project",
        objective="guest-scoped objective",
        source="owner",
        subject_person_id="person-guest",
        viewer_scope="person:person-guest",
        shareability="subject_private",
    )
    engine.store.save_project(guest_project)
    monkeypatch.setattr(host, "_project_engine", engine)

    owner_list = await host.list_projects(request=_http_request(_authority()))
    assert {item["id"] for item in owner_list["projects"]} == {
        governed.id, guest_project.id,
    }
    assert any(
        "TOP-SECRET" in item["objective"] for item in owner_list["projects"]
    )
    legacy_list = await host.list_projects(
        request=_http_request(legacy_authority())
    )
    assert governed.id in {item["id"] for item in legacy_list["projects"]}

    guest_authority = _guest_authority()
    guest_request = _http_request(guest_authority)
    guest_list = await host.list_projects(request=guest_request)
    assert [item["id"] for item in guest_list["projects"]] == [guest_project.id]
    assert "TOP-SECRET" not in canonical_json(guest_list)

    hidden = await host.get_project(governed.id, request=guest_request)
    missing = await host.get_project("proj-does-not-exist", request=guest_request)
    assert hidden == missing == {"available": True, "error": "not_found"}
    visible = await host.get_project(guest_project.id, request=guest_request)
    assert visible["project"]["objective"] == "guest-scoped objective"
    engine.store.close()


@pytest.mark.asyncio
async def test_project_abandon_route_is_owner_only_and_hides_existence(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", OWNER)
    engine = _engine(tmp_path / "projects.db", monkeypatch)
    effect = await ColonySubsystemActionExecutor(projects=engine).perform(
        _request(topic="PRIVATE owner research"), OWNER
    )
    governed = engine.store.get_project(effect["effect_id"])
    guest_visible = Project(
        title="Guest-visible project",
        objective="guest-scoped objective",
        source="owner",
        subject_person_id="person-guest",
        viewer_scope="person:person-guest",
        shareability="subject_private",
    )
    engine.store.save_project(guest_visible)
    monkeypatch.setattr(host, "_project_engine", engine)

    guest_request = _http_request(_guest_authority())
    fixed_not_found = {"ok": False, "reason": "not_found", "project": None}
    assert await host.abandon_project(
        governed.id, request=guest_request, body={"reason": "guest_request"},
    ) == fixed_not_found
    assert await host.abandon_project(
        guest_visible.id,
        request=guest_request,
        body={"reason": "guest_request"},
    ) == fixed_not_found
    assert await host.abandon_project(
        "proj-does-not-exist",
        request=guest_request,
        body={"reason": "guest_request"},
    ) == fixed_not_found
    assert await host.abandon_project(
        "proj-does-not-exist",
        request=_http_request(_owner_api_authority()),
        body={"reason": "owner_request"},
    ) == fixed_not_found
    assert engine.store.get_project(governed.id).status == "planning"
    assert engine.store.get_project(guest_visible.id).status == "planning"

    owner_result = await host.abandon_project(
        governed.id,
        request=_http_request(_owner_api_authority()),
        body={"reason": "owner_request"},
    )
    assert owner_result["ok"] is True
    assert owner_result["project"]["status"] == "abandoned"

    legacy_result = await host.abandon_project(
        guest_visible.id,
        request=_http_request(legacy_authority()),
        body={"reason": "legacy_owner_request"},
    )
    assert legacy_result["ok"] is True
    assert legacy_result["project"]["status"] == "abandoned"
    engine.store.close()


@pytest.mark.asyncio
async def test_project_create_route_requires_owner_and_derives_provenance(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COLONY_OWNER_PERSON_ID", OWNER)
    engine = _engine(tmp_path / "projects.db", monkeypatch)
    monkeypatch.setattr(host, "_project_engine", engine)

    refused = await host.create_project(
        request=_http_request(_guest_authority()),
        body={
            "objective": "guest must not create",
            "source": "governed_action",
        },
    )
    assert refused == {
        "ok": False,
        "reason": "owner_authority_required",
        "project": None,
    }
    assert engine.store.list_projects() == []

    scoped_owner = await host.create_project(
        request=_http_request(_owner_api_authority()),
        body={
            "objective": "owner scoped creation",
            "source": "cognition_spine",
        },
    )
    assert scoped_owner["ok"] is True
    assert scoped_owner["project"]["source"] == "owner"

    legacy_owner = await host.create_project(
        request=_http_request(legacy_authority()),
        body={
            "objective": "legacy migration creation",
            "source": "governed_action",
        },
    )
    assert legacy_owner["ok"] is True
    assert legacy_owner["project"]["source"] == "owner"
    engine.store.close()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda project: project.source_event_refs.__setitem__(
            0, "governed-action:not-a-uuid"
        ),
        lambda project: project.source_event_refs.__setitem__(
            1, "governed-intent:not-an-intent"
        ),
        lambda project: project.evidence_refs.__setitem__(
            1, "intent-digest:not-a-digest"
        ),
        lambda project: project.evidence_refs.__setitem__(
            2, "args-digest:" + "A" * 64
        ),
        lambda project: project.policy_decision_refs.__setitem__(
            0, "approval:APR-invalid"
        ),
        lambda project: project.policy_decision_refs.__setitem__(
            1, "decision:contains whitespace"
        ),
        lambda project: project.policy_decision_refs.__setitem__(
            2, "approval-revision:2"
        ),
        lambda project: project.policy_decision_refs.__setitem__(
            3, "authorization-receipt-digest:not-a-digest"
        ),
    ],
)
@pytest.mark.asyncio
async def test_project_engine_rejects_malformed_governed_provenance(
    tmp_path, monkeypatch, mutate
):
    engine = _engine(tmp_path / "projects.db", monkeypatch)
    executor = ColonySubsystemActionExecutor(projects=engine)
    project = executor._governed_research_project(_request(), OWNER)
    mutate(project)

    with pytest.raises((RuntimeError, ValueError), match="governed research"):
        engine.prepare_governed_research(project)
    assert engine.store.count() == 0
    engine.store.close()


@pytest.mark.asyncio
async def test_adapter_loss_after_precheck_does_not_consume_step_attempt(
    tmp_path, monkeypatch
):
    engine = _engine(tmp_path / "projects.db", monkeypatch)
    effect = await ColonySubsystemActionExecutor(projects=engine).perform(
        _request(), OWNER
    )
    project = engine.store.get_project(effect["effect_id"])
    project.status = "active"
    engine.store.save_project(project)
    step = Step(
        project_id=project.id,
        ordinal=1,
        description="read sources",
        action_kind="research",
    )
    engine.store.save_step(step)
    checks = 0

    def adapter_present_then_lost():
        nonlocal checks
        checks += 1
        return checks == 1

    monkeypatch.setattr(
        engine, "_has_canonical_work_order_adapter", adapter_present_then_lost
    )
    assert await engine._advance_project(project, "live") is True
    persisted = engine.store.steps_for(project.id)[0]
    assert persisted.status == "pending"
    assert persisted.attempts == 0
    assert "canonical_work_order_adapter" in persisted.result
    engine.store.close()
