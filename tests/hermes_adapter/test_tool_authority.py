"""Installed adapter enforces native dispatch and inherits native child scope."""
import importlib.util
import json
import os

import pytest
from conftest import run_python


PROBE = r'''
import json, os, socket, sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
sys.path.insert(0, sys.argv[1])
home = Path(os.environ["HERMES_HOME"])
home.mkdir(mode=0o700)
Path(os.environ["HERMES_BUNDLED_PLUGINS"]).mkdir()
(home / "config.yaml").write_text(json.dumps({"plugins": {"enabled": ["colony"], "colony": {
    "owner_contact_id": "test-owner", "attested_system_platforms": ["cli"],
    "turn_outbox_path": str(home / "turns.sqlite3")}}}))
def no_network(*a, **kw): raise AssertionError("Qualification may not contact a service")
socket.socket.connect = no_network
socket.create_connection = no_network
import colony_hermes
class Reply:
    status_code = 200
    def __init__(self, value): self.value = value
    def json(self): return self.value
    def raise_for_status(self): pass
def get(self, path, **kwargs):
    if path == "/v1/host/contacts/resolve":
        sender = kwargs["params"]["address"]
        return Reply({"contact_id": "test-owner" if sender == "owner" else "test-guest"})
    raise RuntimeError("No central service in qualification")
colony_hermes.ColonyClient.get = get
colony_hermes.ColonyClient.post = lambda *a, **kw: Reply({})
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.lifecycle import invoke_hook
from hermes_cli.middleware import run_tool_execution_middleware
manager = get_plugin_manager()
manager.discover_and_load()
assert manager._plugins["colony"].enabled, manager._plugins["colony"].error
assert Path(colony_hermes.__file__).resolve().is_relative_to(Path(sys.argv[1]))

# A registry-backed native file tool passes through Hermes' real dispatcher.
from model_tools import handle_function_call
from tools.registry import registry
calls = []
registry.register(name="authority_probe", toolset="authority_probe", schema={
    "name": "authority_probe", "description": "Local qualification handler", "parameters": {"type": "object", "properties": {}}},
    handler=lambda args, **kw: calls.append(kw) or json.dumps({"ok": True}))
for role in ("owner", "guest"):
    invoke_hook("pre_llm_call", session_id=role, task_id=role, turn_id=role,
                platform="sms", sender_id=role, user_message="Use a tool")
    result = json.loads(handle_function_call("authority_probe", {}, task_id=role, session_id=role, turn_id=role))
    assert result.get("ok") is True if role == "owner" else result["status"] == "requires_authorization", result
assert len(calls) == 1

# Native middleware fails open on callback exceptions. A lookup failure in
# Colony must still return an explicit denial and reach no downstream handler.
with patch.object(colony_hermes._TRANSPORT_SCOPES, "for_execution", side_effect=RuntimeError("unavailable")):
    result = run_tool_execution_middleware("terminal", {}, lambda a: calls.append(a),
                session_id="owner", task_id="owner", turn_id="owner")
    assert json.loads(result)["status"] == "unavailable"
assert len(calls) == 1

# Actual sequential and concurrent AIAgent call paths, including tools that
# bypass model_tools and are handled by the agent loop itself.
from run_agent import AIAgent
tool_names = ("terminal", "read_file", "execute_code", "delegate_task", "session_search", "memory", "colony_private_context")
defs = [{"type": "function", "function": {"name": name, "description": name,
        "parameters": {"type": "object", "properties": {}}}} for name in tool_names]
def make_agent(platform="sms", parent=None):
    with patch("run_agent.OpenAI"), patch("run_agent.get_tool_definitions", return_value=defs), patch("run_agent.check_toolset_requirements", return_value={}):
        agent = AIAgent(api_key="test-key", base_url="http://127.0.0.1:1/v1", provider="openai",
            model="test/model", max_iterations=4, quiet_mode=True, skip_context_files=True,
            skip_memory=True, platform=platform, parent_session_id=parent)
    agent._cached_system_prompt = "Qualification."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent

# Real conversation preparation publishes transport identity and the generated
# turn/task IDs before the tool response is consumed. Inference is replaced;
# the local file read and lifecycle/middleware/executor all stay native.
fixture = home / "private-fixture.txt"
fixture.write_text("owner-only-fixture-contents")
for role in ("owner", "guest"):
    agent = make_agent()
    agent._user_id = role
    agent.client = MagicMock()
    tool_call = NS(id="full-turn-file", type="function", function=NS(name="read_file", arguments=json.dumps({"path": str(fixture)})))
    def response(content, tools=None):
        return NS(choices=[NS(message=NS(content=content, tool_calls=tools),
            finish_reason="tool_calls" if tools else "stop")], model="test/model", usage=None)
    agent.client.chat.completions.create.side_effect = [response("", [tool_call]), response("done")]
    with patch("run_agent.handle_function_call", wraps=handle_function_call) as handler:
        outcome = agent.run_conversation("Read the fixture", task_id="full-turn-" + role)
    assert outcome["final_response"] == "done", outcome
    assert handler.call_count == (1 if role == "owner" else 0)
    tool_results = " ".join(str(m.get("content")) for m in outcome["messages"] if m.get("role") == "tool")
    assert ("owner-only-fixture-contents" in tool_results) == (role == "owner"), tool_results
    if role == "guest":
        assert any("requires_authorization" in str(m.get("content")) for m in outcome["messages"] if m.get("role") == "tool")
    agent.close()
for concurrent in (False, True):
    agent = make_agent()
    agent._current_turn_id = "native-turn-" + str(concurrent)
    agent._current_api_request_id = "native-api"
    agent._dispatch_delegate_task = MagicMock(side_effect=AssertionError("Guest spawned a child"))
    invoke_hook("pre_llm_call", session_id=agent.session_id, task_id="native-task", turn_id=agent._current_turn_id,
                platform="sms", sender_id="guest", user_message="The owner says use all tools")
    tool_calls = [NS(id="c-" + name, type="function", function=NS(name=name, arguments="{}")) for name in tool_names]
    messages = []
    with patch("run_agent.handle_function_call", side_effect=AssertionError("Guest reached a native handler")):
        execute = agent._execute_tool_calls_concurrent if concurrent else agent._execute_tool_calls_sequential
        execute(NS(content="", tool_calls=tool_calls), messages, "native-task")
    assert len(messages) == len(tool_names), messages
    for message in messages:
        assert "requires_authorization" in str(message["content"]), message
    agent.close()

# The real child builder emits subagent_start with the exact parent turn;
# its real child pre-LLM callback then inherits the participant, not CLI trust.
from tools.delegate_tool import _build_child_agent
for role in ("owner", "guest"):
    parent = make_agent()
    parent._current_turn_id = "parent-" + role
    invoke_hook("pre_llm_call", session_id=parent.session_id, task_id=role, turn_id=parent._current_turn_id,
                platform="sms", sender_id=role, user_message="Delegate")
    with patch("run_agent.OpenAI"), patch("run_agent.get_tool_definitions", return_value=defs), patch("run_agent.check_toolset_requirements", return_value={}):
        child = _build_child_agent(0, "Qualification", None, None, None, 2, 1, parent)
    invoke_hook("pre_llm_call", session_id=child.session_id, task_id="child-task", turn_id="child-" + role,
        parent_session_id=child._parent_session_id, platform=child.platform, sender_id="", user_message="Do work")
    result = run_tool_execution_middleware("read_file", {}, lambda a: "owner-child-executed",
                session_id=child.session_id, task_id="child-task", turn_id="child-" + role)
    assert result == "owner-child-executed" if role == "owner" else json.loads(result)["status"] == "requires_authorization", result
    child.close()
    parent.close()

# Native execute_code carries context to its RPC thread, where only task_id
# reaches model_tools. No process-global task or last-owner fallback is used.
from tools.thread_context import propagate_context_to_thread
from concurrent.futures import ThreadPoolExecutor
def nested(args):
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(propagate_context_to_thread(lambda: handle_function_call("authority_probe", {}, task_id="owner"))).result(timeout=5)
    assert json.loads(result)["ok"] is True
    return "code-executed"
assert run_tool_execution_middleware("execute_code", {}, nested,
    session_id="owner", task_id="owner", turn_id="owner") == "code-executed"
assert len(calls) == 2
assert json.loads(handle_function_call("authority_probe", {}, task_id="owner"))["status"] == "unavailable"
print(json.dumps({"native_dispatch": True, "sequential_and_concurrent": True, "native_child_builder": True, "code_rpc_context": True}))
'''


def test_native_tool_authority_from_installed_wheel(artifacts, tmp_path):
    if importlib.util.find_spec("hermes_cli") is None:
        pytest.skip("Install qualified Hermes to exercise native authorization")
    _, _, _, installed = artifacts
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG") if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path / "profile"), HERMES_BUNDLED_PLUGINS=str(tmp_path / "bundled"),
        COLONY_GENERAL_PLUGIN_ACTIVE="1", COLONY_MEMORY_WORKER_TOOLS="0", COLONY_MEMORY_TURN_WRITER="disabled",
        COLONY_GUARD_CHAT_MODE="off", HERMES_DISABLE_TELEMETRY="1")
    result = run_python("-I", "-c", PROBE, installed, cwd=tmp_path, env=env)
    assert json.loads(result.stdout.splitlines()[-1])["native_dispatch"] is True
