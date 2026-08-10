"""Runtime tool provenance, collision, and boundary regressions.

Dynamic Toolsmith handlers are capabilities, not aliases for shipped tools.
Their provider provenance must be resolved before caller authority or standing
boundaries are evaluated, and a shipped/dynamic name collision is never a
valid model surface.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from colony_sidecar.directives import (
    Action,
    DirectiveManager,
    DirectiveStore,
    DirectiveStoreUnavailable,
)
from colony_sidecar.reasoning import ReasoningLoop, ToolExecutor
from colony_sidecar.reasoning.tool_policy import ToolActorPolicy


def _definition(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"dynamic test tool named {name}",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _dynamic_provider(name: str, handler):
    return lambda: {name: (_definition(name), handler)}


def _actor_for_collision(name: str) -> ToolActorPolicy:
    return ToolActorPolicy(
        principal_id="collision-probe",
        viewer_person_id="owner",
        allow_private_read=name != "calculate",
        allow_mutation=name == "write_file",
    )


def _owner_mutation_actor() -> ToolActorPolicy:
    return ToolActorPolicy(
        principal_id="owner-mutator",
        viewer_person_id="owner",
        allow_private_read=True,
        allow_mutation=True,
    )


def _closed_global_pause_manager(tmp_path) -> DirectiveManager:
    """Persist a real global pause, then reproduce a SQLite store outage."""

    store = DirectiveStore(str(tmp_path / "directives.db"))
    manager = DirectiveManager(store)
    captured = manager.capture_from_message("stop acting")
    assert captured.captured
    healthy = manager.check(Action(
        kind="execute_tool",
        text="pre-outage global-pause probe",
        high_risk=True,
    ))
    assert healthy.allowed is False
    assert healthy.reason == "global_pause_active"
    store._conn.close()
    return manager


class _CallingModel:
    def __init__(self, name: str):
        self.name = name
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if len(self.calls) == 1:
            function = SimpleNamespace(name=self.name, arguments="{}")
            raw = SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(tool_calls=[SimpleNamespace(
                    id="model-tool-call",
                    function=function,
                )]),
            )])
            return SimpleNamespace(raw=raw, content="", usage={})
        raw = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(tool_calls=[]),
        )])
        return SimpleNamespace(raw=raw, content="done", usage={})


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["calculate", "read_file", "write_file"])
async def test_dynamic_first_party_collision_fails_closed_direct(name):
    calls = []

    async def dynamic_handler(arguments):
        calls.append(arguments)
        return "DYNAMIC EXECUTED"

    executor = ToolExecutor()
    executor.set_dynamic_provider(_dynamic_provider(name, dynamic_handler))
    result = await executor.execute_batch(
        [{"id": "direct-collision", "name": name, "arguments": {}}],
        allowed_tools=frozenset({name}),
        actor_policy=_actor_for_collision(name),
    )

    assert calls == []
    assert result[0]["executed"] is False
    assert result[0]["error"] == "tool_name_collision"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["calculate", "read_file", "write_file"])
async def test_dynamic_first_party_collision_fails_before_model_call(name):
    calls = []

    async def dynamic_handler(arguments):
        calls.append(arguments)
        return "DYNAMIC EXECUTED"

    executor = ToolExecutor()
    executor.set_dynamic_provider(_dynamic_provider(name, dynamic_handler))
    model = _CallingModel(name)
    result = await ReasoningLoop(model=model, tools=executor).run_turn(
        session_id="collision-model",
        messages=[{"role": "user", "content": "use the tool"}],
        available_tools=[name],
        actor_policy=_actor_for_collision(name),
    )

    assert result.status == "error"
    assert result.error is not None and "tool_name_collision" in result.error
    assert model.calls == []
    assert calls == []


def _crashing_provider():
    raise RuntimeError("registry unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize(("provider", "error"), [
    (lambda: [], "tool_registry_malformed"),
    (_crashing_provider, "tool_registry_unavailable"),
    (lambda: {
        "fresh_dynamic_action": (
            _definition("different_dynamic_action"),
            lambda _arguments: None,
        ),
    }, "tool_registry_malformed"),
])
async def test_malformed_dynamic_registry_fails_closed(provider, error):
    executor = ToolExecutor()
    executor.set_dynamic_provider(provider)
    result = await executor.execute_batch([{
        "id": "malformed-registry",
        "name": "fresh_dynamic_action",
        "arguments": {},
    }])

    assert result[0]["executed"] is False
    assert result[0]["error"] == error


@pytest.mark.asyncio
async def test_synchronous_dynamic_handler_cannot_run_before_await_validation():
    calls = []

    def unsafe_sync_handler(arguments):
        calls.append(arguments)
        return "MUTATED BEFORE AWAIT"

    name = "fresh_dynamic_action"
    executor = ToolExecutor()
    executor.set_dynamic_provider(
        _dynamic_provider(name, unsafe_sync_handler))
    result = await executor.execute_batch([{
        "id": "sync-handler",
        "name": name,
        "arguments": {"value": 1},
    }])

    assert calls == []
    assert result[0]["executed"] is False
    assert result[0]["error"] == "tool_registry_malformed"


class _ExplodingGuard:
    def check(self, _action):
        raise RuntimeError("directive store unavailable")


class _MalformedGuard:
    def check(self, _action):
        return SimpleNamespace(allowed="yes", reason="not a boolean")


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", [None, _ExplodingGuard(), _MalformedGuard()])
async def test_dynamic_mutation_fails_closed_for_unusable_directive_guard(guard):
    calls = []

    async def dynamic_handler(arguments):
        calls.append(arguments)
        return "DYNAMIC EXECUTED"

    name = "fresh_dynamic_action"
    executor = ToolExecutor()
    executor.set_dynamic_provider(_dynamic_provider(name, dynamic_handler))
    executor.configure_execution_policy(
        directive_manager=guard,
        boundary_required=True,
    )
    result = await executor.execute_batch(
        [{"id": "guard-failure", "name": name, "arguments": {}}],
        allowed_tools=frozenset({name}),
        actor_policy=ToolActorPolicy(
            principal_id="owner-mutator",
            viewer_person_id="owner",
            allow_private_read=True,
            allow_mutation=True,
        ),
    )

    assert calls == []
    assert result[0]["executed"] is False
    assert result[0]["error"] == "tool_boundary_unavailable"


@pytest.mark.asyncio
async def test_real_directive_store_outage_blocks_direct_dynamic_mutation(
    tmp_path,
):
    calls = []

    async def dynamic_handler(arguments):
        calls.append(arguments)
        return "DYNAMIC EXECUTED"

    name = "fresh_dynamic_action"
    executor = ToolExecutor()
    executor.set_dynamic_provider(_dynamic_provider(name, dynamic_handler))
    executor.configure_execution_policy(
        directive_manager=_closed_global_pause_manager(tmp_path),
        boundary_required=True,
    )

    result = await executor.execute_batch(
        [{
            "id": "sqlite-outage-direct",
            "name": name,
            "arguments": {"target": "owner-state"},
        }],
        allowed_tools=frozenset({name}),
        actor_policy=_owner_mutation_actor(),
    )

    assert calls == []
    assert result[0]["executed"] is False
    assert result[0]["error"] == "tool_boundary_unavailable"


@pytest.mark.asyncio
async def test_real_directive_store_outage_blocks_model_dynamic_mutation(
    tmp_path,
):
    calls = []

    async def dynamic_handler(arguments):
        calls.append(arguments)
        return "DYNAMIC EXECUTED"

    name = "fresh_dynamic_action"
    executor = ToolExecutor()
    executor.set_dynamic_provider(_dynamic_provider(name, dynamic_handler))
    executor.configure_execution_policy(
        directive_manager=_closed_global_pause_manager(tmp_path),
        boundary_required=True,
    )
    model = _CallingModel(name)

    result = await ReasoningLoop(model=model, tools=executor).run_turn(
        session_id="sqlite-outage-model",
        messages=[{"role": "user", "content": "use the tool"}],
        available_tools=[name],
        actor_policy=_owner_mutation_actor(),
    )

    assert result.status == "completed"
    assert len(model.calls) == 2
    assert "tool_boundary_unavailable" in json.dumps(model.calls[1][0])
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["write_file", "read_file"])
async def test_real_directive_store_outage_blocks_static_private_or_mutating_tool(
    tmp_path, name,
):
    calls = []

    async def handler(arguments):
        calls.append(arguments)
        return "STATIC EXECUTED"

    executor = ToolExecutor(handlers={name: handler})
    executor.configure_execution_policy(
        directive_manager=_closed_global_pause_manager(tmp_path),
        boundary_required=True,
    )
    result = await executor.execute_batch(
        [{
            "id": f"sqlite-outage-{name}",
            "name": name,
            "arguments": {"path": "owner.txt", "content": "updated"},
        }],
        allowed_tools=frozenset({name}),
        actor_policy=_owner_mutation_actor(),
    )

    assert calls == []
    assert result[0]["executed"] is False
    assert result[0]["error"] == "tool_boundary_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["calculate", "web_search"])
async def test_real_directive_store_outage_keeps_public_read_carveout(
    tmp_path, name,
):
    calls = []

    async def handler(arguments):
        calls.append(arguments)
        return "PUBLIC"

    executor = ToolExecutor(handlers={name: handler})
    executor.configure_execution_policy(
        directive_manager=_closed_global_pause_manager(tmp_path),
        boundary_required=True,
    )
    arguments = (
        {"expression": "2+3"}
        if name == "calculate"
        else {"query": "general information"}
    )
    result = await executor.execute_batch(
        [{
            "id": f"sqlite-outage-{name}",
            "name": name,
            "arguments": arguments,
        }],
        allowed_tools=frozenset({name}),
        actor_policy=ToolActorPolicy(
            principal_id="guest",
            viewer_person_id="guest",
        ),
    )

    assert result[0]["executed"] is True
    assert result[0]["content"] == "PUBLIC"
    assert calls == [arguments]


class _MalformedActiveStore:
    def __init__(self, active):
        self._active = active

    def active(self, *, polarity=None):  # noqa: ARG002
        return self._active


@pytest.mark.parametrize(
    "active",
    [None, {}, set(), "not-a-boundary-list", [object()]],
    ids=["none", "mapping", "set", "string", "invalid-item"],
)
def test_directive_guard_rejects_malformed_active_boundary_sets(active):
    manager = DirectiveManager(_MalformedActiveStore(active))

    with pytest.raises(
        DirectiveStoreUnavailable,
        match="malformed active-boundary set",
    ):
        manager.check(Action(
            kind="execute_tool",
            text="must not infer an empty healthy store",
            high_risk=True,
        ))


@pytest.mark.asyncio
async def test_healthy_empty_store_and_global_pause_behavior_are_unchanged(
    tmp_path,
):
    calls = []

    async def dynamic_handler(arguments):
        calls.append(arguments)
        return "DYNAMIC EXECUTED"

    name = "fresh_dynamic_action"
    manager = DirectiveManager(DirectiveStore(str(tmp_path / "directives.db")))
    executor = ToolExecutor()
    executor.set_dynamic_provider(_dynamic_provider(name, dynamic_handler))
    executor.configure_execution_policy(
        directive_manager=manager,
        boundary_required=True,
    )
    call = [{"id": "healthy", "name": name, "arguments": {}}]

    healthy_empty = await executor.execute_batch(
        call,
        allowed_tools=frozenset({name}),
        actor_policy=_owner_mutation_actor(),
    )
    assert healthy_empty[0]["executed"] is True

    manager.capture_from_message("stop acting")
    healthy_pause = await executor.execute_batch(
        call,
        allowed_tools=frozenset({name}),
        actor_policy=_owner_mutation_actor(),
    )
    assert healthy_pause[0]["executed"] is False
    assert healthy_pause[0]["error"] == "tool_boundary_denied"
    assert "global_pause_active" in healthy_pause[0]["content"]
    assert calls == [{}]


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", [None, _ExplodingGuard(), _MalformedGuard()])
@pytest.mark.parametrize("name", ["calculate", "web_search"])
async def test_static_public_reads_survive_unusable_directive_guard(guard, name):
    calls = []

    async def public_read(arguments):
        calls.append(arguments)
        return "PUBLIC"

    executor = ToolExecutor(handlers={name: public_read})
    executor.configure_execution_policy(
        directive_manager=guard,
        boundary_required=True,
    )
    result = await executor.execute_batch(
        [{
            "id": "public-calculate",
            "name": name,
            "arguments": {"expression": "2+3"},
        }],
        actor_policy=ToolActorPolicy(
            principal_id="guest",
            viewer_person_id="guest",
        ),
    )

    assert result[0]["executed"] is True
    assert result[0]["content"] == "PUBLIC"
    assert calls == [{"expression": "2+3"}]


@pytest.mark.asyncio
async def test_owner_private_static_read_and_scoped_static_mutation_remain_usable():
    guard = DirectiveManager(DirectiveStore())
    calls = []

    async def handler(arguments):
        calls.append(arguments)
        return "OK"

    executor = ToolExecutor(handlers={
        "read_file": handler,
        "write_file": handler,
    })
    executor.configure_execution_policy(
        directive_manager=guard,
        boundary_required=True,
    )
    actor = ToolActorPolicy(
        principal_id="owner-mutator",
        viewer_person_id="owner",
        allow_private_read=True,
        allow_mutation=True,
    )
    result = await executor.execute_batch([
        {
            "id": "private-read",
            "name": "read_file",
            "arguments": {"path": "owner.txt"},
        },
        {
            "id": "scoped-write",
            "name": "write_file",
            "arguments": {"path": "owner.txt", "content": "updated"},
        },
    ], actor_policy=actor)

    assert [item["executed"] for item in result] == [True, True]
    assert calls == [
        {"path": "owner.txt"},
        {"path": "owner.txt", "content": "updated"},
    ]


@pytest.mark.asyncio
async def test_global_act_pause_blocks_dynamic_mutation_direct_and_model():
    guard = DirectiveManager(DirectiveStore())
    guard.capture_from_message("stop acting")
    calls = []

    async def dynamic_handler(arguments):
        calls.append(arguments)
        return "DYNAMIC EXECUTED"

    name = "fresh_dynamic_action"
    executor = ToolExecutor()
    executor.set_dynamic_provider(_dynamic_provider(name, dynamic_handler))
    executor.configure_execution_policy(
        directive_manager=guard,
        boundary_required=True,
    )
    actor = ToolActorPolicy(
        principal_id="owner-mutator",
        viewer_person_id="owner",
        allow_private_read=True,
        allow_mutation=True,
    )

    direct = await executor.execute_batch(
        [{"id": "paused-direct", "name": name, "arguments": {}}],
        allowed_tools=frozenset({name}),
        actor_policy=actor,
    )
    assert direct[0]["error"] == "tool_boundary_denied"
    assert "global_pause_active" in direct[0]["content"]

    model = _CallingModel(name)
    modeled = await ReasoningLoop(model=model, tools=executor).run_turn(
        session_id="paused-model",
        messages=[{"role": "user", "content": "use the tool"}],
        available_tools=[name],
        actor_policy=actor,
    )
    assert modeled.status == "completed"
    assert len(model.calls) == 2
    assert "tool_boundary_denied" in json.dumps(model.calls[1][0])
    assert calls == []


@pytest.mark.asyncio
async def test_p8_off_dynamic_tool_compatibility_and_unique_definitions():
    calls = []

    async def static_calculate(arguments):
        return str(arguments.get("expression", ""))

    async def dynamic_handler(arguments):
        calls.append(arguments)
        return "DYNAMIC EXECUTED"

    name = "fresh_dynamic_action"
    executor = ToolExecutor(handlers={"calculate": static_calculate})
    executor.set_dynamic_provider(_dynamic_provider(name, dynamic_handler))
    definitions = executor.get_definitions(["calculate", name, name])
    definition_names = [item["function"]["name"] for item in definitions]
    assert definition_names == ["calculate", name]

    result = await executor.execute_batch([{
        "id": "legacy-dynamic",
        "name": name,
        "arguments": {"value": 1},
    }])
    assert result[0]["executed"] is True
    assert calls == [{"value": 1}]
