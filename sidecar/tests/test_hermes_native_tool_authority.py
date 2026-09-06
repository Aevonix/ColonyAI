"""Native authority follows exact transport turns, not model arguments."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from test_hermes_general_governance import runtime, _pre


NATIVE_TOOLS = ("terminal", "read_file", "write_file", "patch", "web_search",
                "web_extract", "execute_code", "delegate_task", "cronjob",
                "session_search", "memory", "skill_manage", "send_message",
                "camera_capture", "mcp_device_unlock", "colony_unknown")


def call(context, name, *, session="s", task="t", turn="u", args=None, dispatch=None):
    return context.middleware["tool_execution"](
        tool_name=name, args=args or {}, session_id=session, task_id=task,
        turn_id=turn, tool_call_id="call", api_request_id="api",
        next_call=dispatch or (lambda args: "executed"))


@pytest.mark.parametrize("name", NATIVE_TOOLS)
def test_guest_never_reaches_native_or_unknown_handler(runtime, name):
    _, ctx, _, _ = runtime
    _pre(ctx, session="s", task="t", turn="u", platform="sms", sender="+15550002")
    invoked = []
    result = json.loads(call(ctx, name, args={"authority_lane": "owner", "contact_id": "cid-owner"},
                             dispatch=lambda args: invoked.append(args)))
    assert result["status"] == "requires_authorization"
    assert result["approval_created"] is False and result["effect_performed"] is False
    assert invoked == []


@pytest.mark.parametrize("sender,platform", [("+15550001", "sms"), ("", "cli")])
def test_owner_and_attested_system_keep_native_tools(runtime, sender, platform):
    _, ctx, _, _ = runtime
    _pre(ctx, session="s", task="t", turn="u", platform=platform, sender=sender)
    for name in NATIVE_TOOLS:
        assert call(ctx, name) == "executed"


@pytest.mark.parametrize("overrides", [{"session": "other"}, {"task": "other"},
                                     {"turn": "other"}, {"task": ""}, {"turn": ""}])
def test_scope_cannot_fall_back_to_latest_owner_turn(runtime, overrides):
    _, ctx, _, _ = runtime
    _pre(ctx, session="s", task="t", turn="u", platform="cli", sender="")
    assert json.loads(call(ctx, "terminal", **overrides))["status"] == "unavailable"


def test_native_scope_failure_returns_error_without_dispatch(runtime, monkeypatch):
    module, ctx, _, _ = runtime
    monkeypatch.setattr(module._TRANSPORT_SCOPES, "for_execution",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("lookup failed")))
    assert json.loads(call(ctx, "terminal"))["status"] == "unavailable"


def test_concurrent_owner_guest_and_children_keep_parent_limits(runtime):
    module, ctx, _, _ = runtime
    for role, sender in [("owner", "+15550001"), ("guest", "+15550002")]:
        _pre(ctx, session=role, task=role, turn=role, platform="sms", sender=sender)
        ctx.hooks["subagent_start"](parent_session_id=role, parent_turn_id=role,
                                    child_session_id=role + "-child")
        # A child cannot gain system authority by presenting the local CLI
        # platform in a subsequent native callback.
        ctx.hooks["pre_llm_call"](session_id=role + "-child", task_id=role, turn_id=role,
            parent_session_id=role, platform="cli", sender_id="", user_message="delegated")
    def invoke(role):
        return call(ctx, "terminal", session=role + "-child", task=role, turn=role)
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner, guest = list(pool.map(invoke, ["owner", "guest"]))
    assert owner == "executed"
    assert json.loads(guest)["status"] == "requires_authorization"
    assert module._TOOL_EXECUTION_CONTEXT.get() is None


def test_missing_or_conflicting_child_binding_cannot_become_owner(runtime):
    _, ctx, _, _ = runtime
    _pre(ctx, session="s", task="t", turn="u", platform="cli", sender="")
    ctx.hooks["pre_llm_call"](session_id="child", task_id="child", turn_id="child",
        parent_session_id="s", platform="cli", sender_id="")
    assert json.loads(call(ctx, "terminal", session="child", task="child", turn="child"))["status"] == "unavailable"
    ctx.hooks["subagent_start"](parent_session_id="s", parent_turn_id="u", child_session_id="child-2")
    ctx.hooks["subagent_start"](parent_session_id="s", parent_turn_id="wrong", child_session_id="child-2")
    ctx.hooks["pre_llm_call"](session_id="child-2", task_id="c", turn_id="c",
        parent_session_id="s", platform="cli", sender_id="")
    assert json.loads(call(ctx, "terminal", session="child-2", task="c", turn="c"))["status"] == "unavailable"


def test_native_rotation_keeps_exact_owner_scope(runtime):
    _, ctx, _, _ = runtime
    _pre(ctx, session="s", task="t", turn="u", platform="cli", sender="")
    assert json.loads(call(ctx, "terminal", session="rotated"))["status"] == "unavailable"
    ctx.hooks["pre_api_request"](session_id="rotated", task_id="t", turn_id="u")
    assert call(ctx, "terminal", session="rotated") == "executed"


def test_nested_code_dispatch_inherits_only_exact_ambient_scope(runtime):
    module, ctx, _, _ = runtime
    _pre(ctx, session="s", task="t", turn="u", platform="cli", sender="")
    def code(args):
        assert call(ctx, "read_file", session="", turn="") == "executed"
        assert json.loads(call(ctx, "read_file", session="", turn="", task="wrong"))["status"] == "unavailable"
        assert json.loads(call(ctx, "read_file", session="guest", turn=""))["status"] == "unavailable"
        return "executed"
    assert call(ctx, "execute_code", dispatch=code) == "executed"
    assert module._TOOL_EXECUTION_CONTEXT.get() is None
    assert json.loads(call(ctx, "read_file", session="", turn=""))["status"] == "unavailable"


def test_downstream_tool_error_is_not_retried_or_swallowed(runtime):
    module, ctx, _, _ = runtime
    _pre(ctx, session="s", task="t", turn="u", platform="cli", sender="")
    with pytest.raises(RuntimeError, match="handler failure"):
        call(ctx, "terminal", dispatch=lambda args: (_ for _ in ()).throw(RuntimeError("handler failure")))
    assert module._TOOL_EXECUTION_CONTEXT.get() is None
