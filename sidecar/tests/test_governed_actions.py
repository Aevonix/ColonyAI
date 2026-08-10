"""Exact, durable boundary for owner-authorized Colony actions.

The transport body is intentionally not an identity surface.  These tests
pin the dedicated scoped principal, the immutable execution request, and the
one-mutation-at-most recovery contract before the implementation exists.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from colony_sidecar.api.authority import (
    KeyringError,
    RequestAuthority,
    load_keyring,
    required_scope,
)
from colony_sidecar.api.middleware import ApiKeyMiddleware
from colony_sidecar.api.routers import governed_actions as action_router
from colony_sidecar.governed_actions import (
    ACTION_TOOL_NAMES,
    ColonySubsystemActionExecutor,
    GovernedActionLedger,
    GovernedActionService,
    canonical_json,
    sha256_json,
)
import colony_sidecar.governed_actions as governed_actions_module


NOW = 1_900_000_000.0
OWNER = "person-owner-1"


def _authority(
    *,
    principal: str = "host-action-worker",
    scopes=frozenset({"actions:execute", "actions:verify"}),
    viewer: str | None = OWNER,
    audiences=frozenset({"owner"}),
    allow_unscoped_api: bool = False,
    legacy: bool = False,
) -> RequestAuthority:
    return RequestAuthority(
        principal_id=principal,
        credential_id="current",
        scopes=frozenset(scopes),
        allow_unscoped_api=allow_unscoped_api,
        viewer_person_id=viewer,
        person_ids=frozenset({viewer}) if viewer else frozenset(),
        audiences=frozenset(audiences),
        authenticated=True,
        legacy=legacy,
    )


def _args(tool: str) -> dict:
    return {
        "colony_autonomy_disable": {},
        "colony_autonomy_enable": {},
        "colony_create_commitment": {
            "description": "Send the report",
            "due_at": "2030-03-01T12:00:00+00:00",
            "priority": 80,
        },
        "colony_initiative_feedback": {
            "initiative_id": "initiative-1",
            "action": "actioned",
            "details": {"source": "deck"},
        },
        "colony_record_insight": {
            "content": "Prefers concise status updates",
            "insight_type": "preference",
            "confidence": 0.8,
        },
        "colony_research": {"topic": "bounded agent execution", "depth": "quick"},
        "colony_resolve_commitment": {
            "commitment_id": "commitment-1",
            "outcome": "done",
            "reason": "Delivered",
        },
        "colony_task_complete": {"task_id": "task-1"},
        "colony_task_dismiss": {"task_id": "task-1", "reason": "stale"},
        "colony_task_snooze": {"task_id": "task-1", "hours": 24, "reason": "Later"},
    }[tool]


def _request(
    *,
    tool: str = "colony_task_complete",
    args: dict | None = None,
    action_id: str = "123e4567-e89b-42d3-a456-426614174000",
) -> dict:
    args = _args(tool) if args is None else args
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
        "tool_name": tool,
        "args": args,
        "args_sha256": sha256_json(args),
        "approval": approval,
    }
    return {**unsigned, "execution_digest": sha256_json(unsigned)}


class FakeExecutor:
    def __init__(self) -> None:
        self.prepare_calls: list[tuple[dict, str]] = []
        self.perform_calls: list[tuple[dict, str]] = []
        self.prepare_error: Exception | None = None
        self.error: Exception | None = None

    async def prepare(self, request: dict, owner_person_id: str) -> None:
        self.prepare_calls.append((request, owner_person_id))
        if self.prepare_error is not None:
            raise self.prepare_error

    async def perform(self, request: dict, owner_person_id: str) -> dict:
        self.perform_calls.append((request, owner_person_id))
        if self.error is not None:
            raise self.error
        args = request["args"]
        target = (
            args.get("task_id")
            or args.get("commitment_id")
            or args.get("initiative_id")
            or "autonomy"
        )
        return {
            "schema": "ColonyGovernedActionEffectV1",
            "version": 1,
            "effect_id": target,
            "outcome": "completed",
            "verification": {"status": "completed", "target_id": target},
        }


def _service(tmp_path: Path, executor=None) -> GovernedActionService:
    return GovernedActionService(
        GovernedActionLedger(tmp_path / "governed-actions.db", clock=lambda: NOW),
        executor or FakeExecutor(),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_exact_request_executes_once_and_replay_is_byte_stable(tmp_path):
    executor = FakeExecutor()
    service = _service(tmp_path, executor)
    body = _request()

    first = await service.execute(
        body["action_id"], canonical_json(body).encode(), _authority()
    )
    replay = await service.execute(
        body["action_id"], canonical_json(body).encode(), _authority()
    )
    observed = await service.observe(body["action_id"], _authority())

    assert first == replay == observed
    assert first["schema"] == "ColonyGovernedActionExecutionResultV1"
    assert first["status"] == "completed"
    assert first["effect_state"] == "performed"
    assert first["effect_digest"] == sha256_json(first["effect"])
    assert first["observed_at"] == NOW
    assert len(executor.perform_calls) == 1
    assert executor.perform_calls[0][1] == OWNER
    assert "approval_id" not in canonical_json(first)
    service.close()


@pytest.mark.asyncio
async def test_all_ten_tools_use_exact_arg_schemas_and_owner_from_keyring(tmp_path):
    executor = FakeExecutor()
    service = _service(tmp_path, executor)
    for index, tool in enumerate(sorted(ACTION_TOOL_NAMES), start=1):
        body = _request(
            tool=tool,
            action_id=f"123e4567-e89b-42d3-a456-{index:012d}",
        )
        result = await service.execute(
            body["action_id"], canonical_json(body).encode(), _authority()
        )
        assert result["status"] == "completed"
    assert [call[0]["tool_name"] for call in executor.perform_calls] == sorted(ACTION_TOOL_NAMES)
    assert {call[1] for call in executor.perform_calls} == {OWNER}
    service.close()


@pytest.mark.asyncio
async def test_body_identity_context_and_unknown_args_are_rejected_before_ledger(tmp_path):
    executor = FakeExecutor()
    service = _service(tmp_path, executor)
    for forbidden in ("person_id", "owner_person_id", "context", "principal"):
        body = _request(args={"task_id": "task-1", forbidden: "attacker"})
        with pytest.raises(ValueError):
            await service.execute(
                body["action_id"], canonical_json(body).encode(), _authority()
            )
    assert executor.prepare_calls == []
    assert executor.perform_calls == []
    service.close()


@pytest.mark.asyncio
async def test_request_digest_url_and_duplicate_json_are_exact(tmp_path):
    service = _service(tmp_path)
    body = _request()
    bad = dict(body)
    bad["execution_digest"] = "0" * 64
    with pytest.raises(ValueError):
        await service.execute(body["action_id"], canonical_json(bad).encode(), _authority())
    with pytest.raises(ValueError):
        await service.execute(
            "223e4567-e89b-42d3-a456-426614174000",
            canonical_json(body).encode(),
            _authority(),
        )
    duplicate = canonical_json(body).replace(
        '"version":1', '"version":1,"version":1', 1
    ).encode()
    with pytest.raises(ValueError):
        await service.execute(body["action_id"], duplicate, _authority())
    service.close()


@pytest.mark.asyncio
async def test_same_action_id_with_different_request_never_reexecutes(tmp_path):
    executor = FakeExecutor()
    service = _service(tmp_path, executor)
    body = _request()
    await service.execute(body["action_id"], canonical_json(body).encode(), _authority())
    conflict = _request(args={"task_id": "task-2"})
    with pytest.raises(ValueError):
        await service.execute(
            conflict["action_id"], canonical_json(conflict).encode(), _authority()
        )
    assert len(executor.perform_calls) == 1
    service.close()


@pytest.mark.asyncio
async def test_started_effect_exception_is_ambiguous_and_never_retried(tmp_path):
    executor = FakeExecutor()
    executor.error = RuntimeError("secret backend detail")
    service = _service(tmp_path, executor)
    body = _request()
    first = await service.execute(
        body["action_id"], canonical_json(body).encode(), _authority()
    )
    replay = await service.execute(
        body["action_id"], canonical_json(body).encode(), _authority()
    )
    assert first == replay
    assert first["status"] == "ambiguous"
    assert first["effect_state"] == "uncertain"
    assert "secret" not in canonical_json(first)
    assert len(executor.perform_calls) == 1
    service.close()


@pytest.mark.asyncio
async def test_read_only_prepare_failure_is_failed_and_never_starts_effect(tmp_path):
    executor = FakeExecutor()
    executor.prepare_error = RuntimeError("missing subsystem")
    service = _service(tmp_path, executor)
    body = _request()
    first = await service.execute(
        body["action_id"], canonical_json(body).encode(), _authority()
    )
    replay = await service.execute(
        body["action_id"], canonical_json(body).encode(), _authority()
    )
    assert first == replay
    assert first["status"] == "failed"
    assert first["effect_state"] == "not_performed"
    assert executor.perform_calls == []
    service.close()


@pytest.mark.asyncio
async def test_invalid_effect_projection_is_ambiguous_after_single_dispatch(tmp_path):
    class UnsafeEffect(FakeExecutor):
        async def perform(self, request, owner_person_id):
            self.perform_calls.append((request, owner_person_id))
            return {
                "schema": "ColonyGovernedActionEffectV1",
                "version": 1,
                "effect_id": "task-1",
                "outcome": "completed",
                "verification": {"status": "completed\nsecret"},
            }

    executor = UnsafeEffect()
    service = _service(tmp_path, executor)
    body = _request()
    result = await service.execute(
        body["action_id"], canonical_json(body).encode(), _authority()
    )
    assert result["status"] == "ambiguous"
    assert "secret" not in canonical_json(result)
    assert len(executor.perform_calls) == 1
    service.close()


@pytest.mark.asyncio
async def test_concurrent_exact_requests_have_one_mutation_and_same_result(tmp_path):
    class YieldingExecutor(FakeExecutor):
        async def perform(self, request, owner_person_id):
            await asyncio.sleep(0)
            return await super().perform(request, owner_person_id)

    executor = YieldingExecutor()
    service = _service(tmp_path, executor)
    body = _request()
    encoded = canonical_json(body).encode()
    first, second = await asyncio.gather(
        service.execute(body["action_id"], encoded, _authority()),
        service.execute(body["action_id"], encoded, _authority()),
    )
    assert first == second
    assert len(executor.perform_calls) == 1
    service.close()


@pytest.mark.asyncio
async def test_completed_replay_survives_approval_expiry_but_new_expired_action_fails(tmp_path):
    class Clock:
        now = NOW

        def __call__(self):
            return self.now

    clock = Clock()
    ledger = GovernedActionLedger(tmp_path / "actions.db", clock=clock)
    executor = FakeExecutor()
    service = GovernedActionService(ledger, executor, clock=clock)
    body = _request()
    first = await service.execute(
        body["action_id"], canonical_json(body).encode(), _authority()
    )
    clock.now = NOW + 1000
    replay = await service.execute(
        body["action_id"], canonical_json(body).encode(), _authority()
    )
    assert first == replay

    expired = _request(action_id="223e4567-e89b-42d3-a456-426614174000")
    with pytest.raises(ValueError):
        await service.execute(
            expired["action_id"], canonical_json(expired).encode(), _authority()
        )
    assert len(executor.perform_calls) == 1
    service.close()


@pytest.mark.asyncio
async def test_cancelled_prepare_restart_after_expiry_fails_durably_without_mutation(tmp_path):
    class Clock:
        now = NOW

        def __call__(self):
            return self.now

    class BlockingPrepare(FakeExecutor):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()

        async def prepare(self, request, owner_person_id):
            self.prepare_calls.append((request, owner_person_id))
            self.entered.set()
            await asyncio.Future()

    clock = Clock()
    db = tmp_path / "governed-actions.db"
    first_executor = BlockingPrepare()
    first = GovernedActionService(
        GovernedActionLedger(db, clock=clock), first_executor, clock=clock
    )
    body = _request()
    pending = asyncio.create_task(first.execute(
        body["action_id"], canonical_json(body).encode(), _authority()
    ))
    await first_executor.entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert first.ledger.get(body["action_id"])["state"] == "prepared"
    first.close()

    clock.now = NOW + 1000
    replay_executor = FakeExecutor()
    replay = GovernedActionService(
        GovernedActionLedger(db, clock=clock), replay_executor, clock=clock
    )
    result = await replay.execute(
        body["action_id"], canonical_json(body).encode(), _authority()
    )
    assert result["status"] == "failed"
    assert result["effect_state"] == "not_performed"
    assert replay.ledger.get(body["action_id"])["state"] == "failed"
    assert replay_executor.prepare_calls == []
    assert replay_executor.perform_calls == []
    replay.close()


@pytest.mark.asyncio
async def test_approval_is_rechecked_after_prepare_immediately_before_effect(tmp_path):
    class Clock:
        now = NOW

        def __call__(self):
            return self.now

    class ExpiringPrepare(FakeExecutor):
        async def prepare(self, request, owner_person_id):
            self.prepare_calls.append((request, owner_person_id))
            clock.now = NOW + 1000

    clock = Clock()
    executor = ExpiringPrepare()
    service = GovernedActionService(
        GovernedActionLedger(tmp_path / "actions.db", clock=clock),
        executor,
        clock=clock,
    )
    body = _request()
    result = await service.execute(
        body["action_id"], canonical_json(body).encode(), _authority()
    )
    assert result["status"] == "failed"
    assert executor.perform_calls == []
    service.close()


@pytest.mark.asyncio
async def test_deep_huge_and_nonfinite_documents_fail_before_ledger(tmp_path):
    service = _service(tmp_path)
    body = _request()
    nested = []
    cursor = nested
    for _ in range(30):
        child = []
        cursor.append(child)
        cursor = child
    body["args"] = {"task_id": "task-1", "context": nested}
    raw = canonical_json(body).encode()
    with pytest.raises(ValueError):
        await service.execute(body["action_id"], raw, _authority())
    huge_integer = canonical_json(_request()).replace(
        '"version":1', '"version":1000000000000000000000000000000000000', 1
    ).encode()
    with pytest.raises(ValueError):
        await service.execute(body["action_id"], huge_integer, _authority())
    nonfinite = canonical_json(_request()).replace(
        '"decided_at":1899999995.0', '"decided_at":NaN', 1
    ).encode()
    with pytest.raises(ValueError):
        await service.execute(body["action_id"], nonfinite, _authority())
    service.close()


@pytest.mark.parametrize(
    ("tool", "args"),
    (
        ("colony_task_complete", {"task_id": "task@other"}),
        (
            "colony_resolve_commitment",
            {"commitment_id": "commitment@other"},
        ),
        (
            "colony_initiative_feedback",
            {"initiative_id": "initiative@other", "action": "actioned"},
        ),
        (
            "colony_initiative_feedback",
            {
                "initiative_id": "initiative-1",
                "action": "actioned",
                "details": {"source@other": "deck"},
            },
        ),
    ),
)
@pytest.mark.asyncio
async def test_request_identifiers_match_host_producer_alphabet(tmp_path, tool, args):
    service = _service(tmp_path)
    body = _request(tool=tool, args=args)
    with pytest.raises(ValueError):
        await service.execute(
            body["action_id"], canonical_json(body).encode(), _authority()
        )
    assert service.ledger.get(body["action_id"]) is None
    service.close()


def test_governed_ledger_is_private_and_has_no_persistent_wal_or_shm(tmp_path):
    database = tmp_path / "governed-actions.db"
    ledger = GovernedActionLedger(database, clock=lambda: NOW)
    try:
        assert stat.S_IMODE(database.stat().st_mode) == 0o600
        assert database.stat().st_uid == os.geteuid()
        assert database.stat().st_nlink == 1
        assert not Path(str(database) + "-wal").exists()
        assert not Path(str(database) + "-shm").exists()
    finally:
        ledger.close()


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_governed_ledger_rejects_leaf_alias_without_touching_target(
    tmp_path, alias_kind
):
    victim = tmp_path / "victim.db"
    victim.touch(mode=0o600)
    victim.chmod(0o600)
    before = (victim.read_bytes(), stat.S_IMODE(victim.stat().st_mode))
    database = tmp_path / "governed-actions.db"
    if alias_kind == "symlink":
        database.symlink_to(victim)
    else:
        os.link(victim, database)
    with pytest.raises(OSError):
        GovernedActionLedger(database, clock=lambda: NOW)
    assert (victim.read_bytes(), stat.S_IMODE(victim.stat().st_mode)) == before


def test_governed_ledger_rejects_symlink_or_insecure_private_parent(tmp_path):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(OSError):
        GovernedActionLedger(linked_parent / "actions.db", clock=lambda: NOW)
    assert not (real_parent / "actions.db").exists()

    insecure_parent = tmp_path / "insecure-parent"
    insecure_parent.mkdir(mode=0o755)
    with pytest.raises(OSError):
        GovernedActionLedger(insecure_parent / "actions.db", clock=lambda: NOW)
    assert stat.S_IMODE(insecure_parent.stat().st_mode) == 0o755


def test_governed_ledger_rejects_permissive_or_wrong_owner_existing_leaf(
    tmp_path, monkeypatch
):
    permissive = tmp_path / "permissive.db"
    permissive.touch(mode=0o644)
    permissive.chmod(0o644)
    with pytest.raises(OSError):
        GovernedActionLedger(permissive, clock=lambda: NOW)
    assert stat.S_IMODE(permissive.stat().st_mode) == 0o644

    wrong_owner = tmp_path / "wrong-owner.db"
    wrong_owner.touch(mode=0o600)
    wrong_owner.chmod(0o600)
    monkeypatch.setattr(
        governed_actions_module.os, "geteuid", lambda: os.getuid() + 100_000
    )
    with pytest.raises(OSError):
        GovernedActionLedger(wrong_owner, clock=lambda: NOW)


@pytest.mark.asyncio
async def test_restart_recovers_executing_as_ambiguous_without_effect_retry(tmp_path):
    body = _request()
    db = tmp_path / "governed-actions.db"
    ledger = GovernedActionLedger(db, clock=lambda: NOW)
    parsed, created = ledger.prepare_execution(body, owner_person_id=OWNER)
    ledger.mark_executing(body["action_id"])
    ledger.close()

    executor = FakeExecutor()
    service = GovernedActionService(
        GovernedActionLedger(db, clock=lambda: NOW + 10),
        executor,
        clock=lambda: NOW + 10,
    )
    recovered = await service.observe(body["action_id"], _authority())
    assert created is True
    assert parsed["state"] == "prepared"
    assert recovered["status"] == "ambiguous"
    assert executor.perform_calls == []
    service.close()


@pytest.mark.asyncio
async def test_authority_is_exact_nonlegacy_owner_bound_and_minimally_scoped(tmp_path):
    service = _service(tmp_path)
    body = _request()
    denied = (
        _authority(principal="other-worker"),
        _authority(scopes={"actions:verify"}),
        _authority(viewer=None),
        _authority(audiences=set()),
        _authority(allow_unscoped_api=True),
        _authority(legacy=True),
        _authority(scopes={"actions:execute", "actions:verify", "api:access"}),
    )
    for authority in denied:
        with pytest.raises(PermissionError):
            await service.execute(
                body["action_id"], canonical_json(body).encode(), authority
            )
    service.close()


def test_exact_scope_map_has_no_global_api_fallback():
    path = "/v1/host/actions/123e4567-e89b-42d3-a456-426614174000"
    assert required_scope("PUT", path) == "actions:execute"
    assert required_scope("GET", path) == "actions:verify"
    assert required_scope("POST", path) == "api:access"


@pytest.mark.asyncio
async def test_http_boundary_enforces_dedicated_keyring_role(tmp_path):
    keyring = tmp_path / "keyring.json"
    keyring.write_text(json.dumps({
        "version": 1,
        "principals": [{
            "principal": "host-action-worker",
            "status": "active",
            "allow_unscoped_api": False,
            "scopes": ["actions:execute", "actions:verify"],
            "viewer_person_id": OWNER,
            "person_ids": [OWNER],
            "audiences": ["owner"],
            "credentials": [{
                "id": "current",
                "secret": "dedicated-governed-action-secret",
                "status": "active",
            }],
        }],
    }))
    keyring.chmod(0o600)
    service = _service(tmp_path)
    action_router.set_governed_action_service(service)
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, keyring_path=str(keyring))
    app.include_router(action_router.router)
    body = _request()
    headers = {
        "Authorization": "Bearer dedicated-governed-action-secret",
        "X-Colony-Principal": "host-action-worker",
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/v1/host/actions/" + body["action_id"], json=body, headers=headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "completed"
        verify = await client.get(
            "/v1/host/actions/" + body["action_id"], headers=headers
        )
        assert verify.status_code == 200
        assert verify.content == response.content
        legacy = await client.get(
            "/v1/host/actions/" + body["action_id"],
            headers={"Authorization": "Bearer wrong"},
        )
        assert legacy.status_code == 401
        wrong_type = await client.put(
            "/v1/host/actions/" + body["action_id"],
            content=canonical_json(body),
            headers={**headers, "Content-Type": "text/plain"},
        )
        assert wrong_type.status_code == 422
    action_router.set_governed_action_service(None)
    service.close()


@pytest.mark.asyncio
async def test_http_boundary_rejects_declared_and_chunked_oversize_before_execution(
    tmp_path,
):
    keyring = tmp_path / "keyring.json"
    keyring.write_text(json.dumps({
        "version": 1,
        "principals": [{
            "principal": "host-action-worker",
            "status": "active",
            "allow_unscoped_api": False,
            "scopes": ["actions:execute", "actions:verify"],
            "viewer_person_id": OWNER,
            "person_ids": [OWNER],
            "audiences": ["owner"],
            "credentials": [{
                "id": "current",
                "secret": "dedicated-governed-action-secret",
                "status": "active",
            }],
        }],
    }))
    keyring.chmod(0o600)
    executor = FakeExecutor()
    service = _service(tmp_path, executor)
    action_router.set_governed_action_service(service)
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, keyring_path=str(keyring))
    app.include_router(action_router.router)
    body = _request()
    path = "/v1/host/actions/" + body["action_id"]
    headers = {
        "Authorization": "Bearer dedicated-governed-action-secret",
        "X-Colony-Principal": "host-action-worker",
        "Content-Type": "application/json",
    }

    async def oversized_chunks():
        yield b"x" * (20 * 1024)
        yield b"y" * (20 * 1024)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            declared = await client.put(
                path,
                content=b"{}",
                headers={**headers, "Content-Length": str(32 * 1024 + 1)},
            )
            chunked = await client.put(
                path,
                content=oversized_chunks(),
                headers={**headers, "Transfer-Encoding": "chunked"},
            )
        assert declared.status_code == 413
        assert chunked.status_code == 413
        assert executor.prepare_calls == []
        assert executor.perform_calls == []
        assert service.ledger.get(body["action_id"]) is None
    finally:
        action_router.set_governed_action_service(None)
        service.close()


class _Commitments:
    def __init__(self):
        self.rows = {"commitment-1": {"id": "commitment-1", "person_id": OWNER, "status": "pending"}}

    def create(self, **kwargs):
        assert kwargs["person_id"] == OWNER
        row = {"id": "commitment-new", "person_id": OWNER, "status": "pending"}
        self.rows[row["id"]] = row
        return row

    def get(self, item):
        return self.rows.get(item)

    def resolve(self, item, **kwargs):
        row = self.rows[item]
        row["status"] = "fulfilled"
        return row


class _Goals:
    def __init__(self):
        self.goal = SimpleNamespace(goal_id="task-1", status="active", snoozed_until=None)

    def get_goal(self, item):
        if item != "task-1":
            raise KeyError(item)
        return self.goal

    def complete_task(self, item):
        self.goal.status = "completed"
        return True

    def snooze_task(self, item, hours, reason):
        self.goal.status = "active"
        self.goal.snoozed_until = "future"
        return True

    def dismiss_task(self, item, reason):
        self.goal.status = "abandoned"
        return True


class _Initiatives:
    def __init__(self):
        self.item = SimpleNamespace(id="initiative-1", type="task", status="pending")

    def get(self, item):
        return self.item if item == self.item.id else None

    def update(self, item, **kwargs):
        self.item.status = kwargs["status"]
        return self.item

    def log_history(self, *args, **kwargs):
        return None


class _Graph:
    async def store_memory(self, **kwargs):
        assert kwargs["person_id"] == OWNER
        return "insight-1"


class _Projects:
    def prepare_governed_research(self, project):
        assert project.subject_person_id == OWNER

    def enqueue_governed_research(self, project):
        assert project.subject_person_id == OWNER
        return project


@pytest.mark.asyncio
async def test_generic_subsystem_adapter_maps_every_action_without_context_forwarding():
    running = {"value": True}

    async def enable():
        running["value"] = True

    async def disable():
        running["value"] = False

    executor = ColonySubsystemActionExecutor(
        graph=_Graph(),
        goals=_Goals(),
        commitments=_Commitments(),
        initiatives=_Initiatives(),
        projects=_Projects(),
        autonomy_enable=enable,
        autonomy_disable=disable,
        autonomy_running=lambda: running["value"],
    )
    for tool in sorted(ACTION_TOOL_NAMES):
        args = _args(tool)
        request = _request(tool=tool, args=args)
        await executor.prepare(request, OWNER)
        effect = await executor.perform(request, OWNER)
        assert set(effect) == {"schema", "version", "effect_id", "outcome", "verification"}
        encoded = canonical_json(effect)
        assert "Send the report" not in encoded
        assert "Prefers concise" not in encoded
        assert "bounded agent" not in encoded


@pytest.mark.asyncio
async def test_record_insight_matches_live_colony_graph_contract(monkeypatch):
    import inspect

    from colony_sidecar.intelligence.graph.client import ColonyGraph

    writes = []

    assert {
        "content",
        "memory_type",
        "entities",
        "metadata",
        "importance",
        "person_id",
        "source_type",
        "content_hash",
    } <= set(inspect.signature(ColonyGraph.store_memory).parameters)

    async def store_memory(_self, **kwargs):
        writes.append(kwargs)
        return "insight-live-1"

    monkeypatch.setattr(ColonyGraph, "store_memory", store_memory)
    graph = object.__new__(ColonyGraph)
    executor = ColonySubsystemActionExecutor(graph=graph)
    args = _args("colony_record_insight")
    request = _request(tool="colony_record_insight", args=args)

    await executor.prepare(request, OWNER)
    effect = await executor.perform(request, OWNER)

    assert effect["effect_id"] == "insight-live-1"
    expected_content_hash = sha256_json({
        "schema": "ColonyGovernedInsightIdentityV1",
        "version": 1,
        "person_id": OWNER,
        "insight_type": args["insight_type"],
        "source": "governed_action",
        "content": args["content"],
    })
    assert writes == [{
        "content": args["content"],
        "memory_type": "semantic",
        "entities": [],
        "metadata": {
            "governed": True,
            "insight_type": "preference",
            "source": "governed_action",
        },
        "importance": args["confidence"],
        "person_id": OWNER,
        "source_type": "inference",
        "content_hash": expected_content_hash,
    }]

    other_owner = "person-owner-2"
    await executor.perform(request, other_owner)
    assert writes[-1]["person_id"] == other_owner
    assert writes[-1]["content_hash"] == sha256_json({
        "schema": "ColonyGovernedInsightIdentityV1",
        "version": 1,
        "person_id": other_owner,
        "insight_type": args["insight_type"],
        "source": "governed_action",
        "content": args["content"],
    })
    assert writes[-1]["content_hash"] != expected_content_hash


@pytest.mark.asyncio
async def test_record_insight_contract_drift_fails_during_read_only_prepare():
    executor = ColonySubsystemActionExecutor(graph=object())
    with pytest.raises(RuntimeError, match="insight writer"):
        await executor.prepare(
            _request(tool="colony_record_insight"),
            OWNER,
        )


def test_example_keyring_contains_minimal_governed_action_role():
    document = json.loads(
        (Path(__file__).parents[1] / "api-keyring.example.json").read_text()
    )
    role = next(
        item for item in document["principals"]
        if item["principal"] == "host-action-worker"
    )
    assert role["allow_unscoped_api"] is False
    assert set(role["scopes"]) == {"actions:execute", "actions:verify"}
    assert role["audiences"] == ["owner"]
    assert role["viewer_person_id"] == "replace-with-owner-contact-id"


def test_keyring_rejects_broadened_or_unbound_governed_action_role(tmp_path):
    base = {
        "principal": "host-action-worker",
        "status": "active",
        "allow_unscoped_api": False,
        "scopes": ["actions:execute", "actions:verify"],
        "viewer_person_id": OWNER,
        "person_ids": [OWNER],
        "audiences": ["owner"],
        "credentials": [{
            "id": "current",
            "secret": "dedicated-governed-action-secret",
            "status": "active",
        }],
    }
    for mutation in (
        {"allow_unscoped_api": True},
        {"scopes": ["actions:execute", "actions:verify", "api:access"]},
        {"viewer_person_id": ""},
        {"person_ids": []},
        {"audiences": ["owner", "global"]},
    ):
        principal = {**base, **mutation}
        path = tmp_path / (hashlib.sha256(canonical_json(mutation).encode()).hexdigest() + ".json")
        path.write_text(json.dumps({"version": 1, "principals": [principal]}))
        path.chmod(0o600)
        with pytest.raises(KeyringError):
            load_keyring(path)
