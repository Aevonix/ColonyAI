"""Installed adapter receives real Hermes lifecycle dispatch without core edits."""
import importlib.util
import json
import os

import pytest
from conftest import run_python


PROBE = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
home = Path(os.environ["HERMES_HOME"])
home.mkdir(mode=0o700)
Path(os.environ["HERMES_BUNDLED_PLUGINS"]).mkdir()
(home / "config.yaml").write_text(json.dumps({"plugins": {"enabled": ["colony"], "colony": {
    "owner_contact_id": "test-owner", "attested_system_platforms": ["cli", "cron"],
    "execution_registry_enabled": True,
    "turn_outbox_path": str(home / "turns.sqlite3")}}}))
import colony_hermes
calls = []
class Reply:
    def raise_for_status(self): pass
    def json(self): return {}
def post(self, path, **kwargs):
    assert path == "/v1/host/executions/observe", path
    calls.append(kwargs["json"])
    return Reply()
colony_hermes.ColonyClient.post = post
from hermes_cli.plugins import get_plugin_manager
from hermes_cli.lifecycle import invoke_hook
manager = get_plugin_manager()
manager.discover_and_load()
plugin = manager._plugins["colony"]
assert plugin.enabled, plugin.error
assert Path(plugin.module.__file__).resolve().is_relative_to(Path(sys.argv[1]))
for event in ("pre_api_request", "post_api_request", "post_tool_call", "subagent_start", "on_session_end"):
    assert event in plugin.hooks_registered, event
invoke_hook("pre_llm_call", session_id="chat", task_id="task-a", turn_id="turn-a", platform="cli", sender_id="", user_message="Private task text")
invoke_hook("pre_llm_call", session_id="cron-job", task_id="task-b", turn_id="turn-b", platform="cron", sender_id="", user_message="Private scheduled text")
invoke_hook("subagent_start", parent_session_id="chat", parent_turn_id="turn-a", child_session_id="child", child_goal="Private delegated text")
invoke_hook("pre_llm_call", session_id="child", task_id="child-task", turn_id="child-turn", platform="subagent", parent_session_id="chat", sender_id="", user_message="Private child text")
invoke_hook("pre_api_request", session_id="child", turn_id="child-turn")
invoke_hook("post_tool_call", session_id="cron-job", turn_id="turn-b", tool_name="terminal", result="Private result")
invoke_hook("on_session_end", session_id="child", turn_id="child-turn", interrupted=True, completed=False, failed=False)
invoke_hook("on_session_end", session_id="chat", turn_id="turn-a", interrupted=False, completed=True, failed=False)
assert len(calls) == 7, calls
assert calls[2]["parent_execution_id"] == calls[0]["execution_id"]
assert calls[2]["contact_id"] == "test-owner"
assert calls[1]["platform"] == "cron"
assert calls[-2]["state"] == "interrupted" and calls[-1]["state"] == "completed"
assert "Private" not in json.dumps(calls)
print(json.dumps({"observations": len(calls), "native_hooks": True}))
'''


def test_native_execution_hooks_from_installed_wheel(artifacts, tmp_path):
    if importlib.util.find_spec("hermes_cli") is None:
        pytest.skip("Install qualified Hermes to exercise native lifecycle")
    _, _, _, installed = artifacts
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG") if key in os.environ}
    env.update(HERMES_HOME=str(tmp_path / "profile"), HERMES_BUNDLED_PLUGINS=str(tmp_path / "bundled"),
               COLONY_GENERAL_PLUGIN_ACTIVE="1", COLONY_MEMORY_WORKER_TOOLS="0", COLONY_MEMORY_TURN_WRITER="disabled")
    result = run_python("-I", "-c", PROBE, installed, cwd=tmp_path, env=env)
    assert json.loads(result.stdout.splitlines()[-1])["native_hooks"] is True
