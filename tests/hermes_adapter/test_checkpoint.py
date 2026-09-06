"""Native Hermes checkpoint success, replay and loss-prevention boundaries."""

import importlib.util
import os
import subprocess
import sys

import pytest


PROBE = r'''
import copy
import json
import os
from pathlib import Path
import socket
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1])
home = Path(os.environ["HERMES_HOME"])
home.mkdir(mode=0o700)
Path(os.environ["HERMES_BUNDLED_PLUGINS"]).mkdir()
config = {
    "plugins": {"enabled": ["colony"], "colony": {"owner_contact_id": "test-owner"}},
    "memory": {"provider": "colony-memory", "config": {"contact_id": "test-owner"}},
}
(home / "config.yaml").write_text(json.dumps(config))
def offline(*args, **kwargs):
    raise OSError("sidecar offline")
socket.socket.connect = offline
socket.create_connection = offline

from hermes_cli.plugins import get_plugin_manager
from plugins.memory import load_memory_provider
from agent.memory_manager import MemoryManager
from agent.conversation_compression import (
    compress_context, CompressionCheckpointUnavailable,
    _direct_messages_for_pre_compress_memory,
)
from colony_hermes.client import TurnOutbox
from colony_hermes import evidence

plugins = get_plugin_manager()
plugins.discover_and_load()
assert plugins._plugins["colony"].enabled
provider = load_memory_provider("colony-memory")
manager = MemoryManager()
manager.add_provider(provider)
manager.initialize_all("session-a", hermes_home=str(home))
assert manager.supports_pre_compress_checkpoint(2)

fact = "ordinary text " * 250 + "The hydrofoil leaves Friday at nine."
raw = [
    {"role": "system", "content": "system-only-marker"},
    {"role": "user", "content": fact, "api_content": "injected-only-marker"},
    {"role": "assistant", "content": "Understood.", "tool_calls": [{"id": "wrapper-only"}]},
    {"role": "tool", "content": "tool-only-marker"},
    {"role": "assistant", "content": "derivative-only-marker", "_compressed_summary": True},
]
original = copy.deepcopy(raw)
manager.on_pre_compress(
    raw, evidence_messages=_direct_messages_for_pre_compress_memory(raw),
    require_checkpoint=True,
)
assert raw == original
assert provider.get_diagnostics()["checkpoint"]["state"] == "pending"
path = home / "state" / "colony-turn-outbox.sqlite3"
outbox = TurnOutbox(path)
rows = outbox.snapshot()
assert len(rows) == 1
stored = rows[0]["payload"]["checkpoint_messages"]
assert stored == [{"role": "user", "content": fact}, {"role": "assistant", "content": "Understood."}]
assert "only-marker" not in json.dumps(stored)

# The same callback can replay after process loss without adding another row.
manager.on_pre_compress(raw, evidence_messages=stored, require_checkpoint=True)
assert len(TurnOutbox(path).snapshot()) == 1

# Ordinary completed turns use the existing writer and retain their full text.
plugins.invoke_hook("pre_llm_call", session_id="session-a", task_id="task-a", turn_id="ordinary-a", platform="cli", sender_id="", user_message=fact)
plugins.invoke_hook("post_llm_call", session_id="session-a", task_id="task-a", turn_id="ordinary-a", platform="cli", user_message=fact, assistant_response="Full reply", conversation_history=[], model="processor-a")
rows = TurnOutbox(path).snapshot()
ordinary = [row for row in rows if "checkpoint_messages" not in row["payload"]]
assert len(ordinary) == 1 and ordinary[0]["payload"]["user_message"] == fact

# Reopen the durable outbox and deliver through the actual client serializer.
wire = []
import httpx
def accepted(self, route, **kwargs):
    wire.append((route, kwargs["json"]))
    return httpx.Response(201, json={"accepted": True, "source_recorded": True}, request=httpx.Request("PUT", "http://test" + route))
client = evidence.ColonyClient(url="http://127.0.0.1:7777", api_key="")
erasure_checks = []
def erasure_feed(self, route, **kwargs):
    erasure_checks.append(kwargs["params"])
    return httpx.Response(200, json={"contact_id": "test-owner", "head": 0, "through": 0, "events": [], "complete": True}, request=httpx.Request("GET", "http://test" + route))
evidence.ColonyClient.get = erasure_feed
deliver = lambda payload, *, timeout_seconds: client.sync_turn(**payload, outbox=outbox, timeout_seconds=timeout_seconds)
# An old sidecar can ignore additive fields and say accepted; that response
# must not discard the only source copy during a rolling upgrade.
evidence.ColonyClient.put = lambda *a, **k: httpx.Response(200, json={"accepted": True}, request=httpx.Request("PUT", "http://test"))
assert TurnOutbox(path).drain(deliver, timeout_seconds=1) == 0
assert all(row["state"] == "pending" for row in TurnOutbox(path).snapshot())
evidence.ColonyClient.put = accepted
# Delivery is bounded per pass, not guaranteed to empty the queue in one
# second on a shared runner. Preserve the real retry/lease behavior and check
# eventual delivery after restart instead of asserting host filesystem speed.
delivered = 0
deadline = time.monotonic() + 10
while delivered < 2 and time.monotonic() < deadline:
    delivered += TurnOutbox(path).drain(deliver, timeout_seconds=1)
    if delivered < 2:
        time.sleep(0.1)
assert delivered == 2, TurnOutbox(path).snapshot()
assert TurnOutbox(path).drain(deliver, timeout_seconds=1) == 0
assert any(body.get("checkpoint_messages", [{}])[0].get("content") == fact for _, body in wire)
assert any(body.get("user_message", {}).get("content") == fact for _, body in wire)

assert erasure_checks and all(item["contact_id"] == "test-owner" for item in erasure_checks)

# The real manager forwards an exact committed remove even in coexistence mode.
removed = []
def removed_source(self, route, **kwargs):
    removed.append((route, kwargs["json"]))
    return httpx.Response(200, json={"source_erased": True, "watermark": 1}, request=httpx.Request("POST", route))
httpx.Client.post = removed_source
manager.notify_memory_tool_write({"success": True}, {"action": "remove", "old_text": fact})
assert len(removed) == 1 and removed[0][1]["old_text"] == fact
assert removed[0][1]["contact_id"] == "test-owner"
assert provider.get_diagnostics()["source_erasure"]["state"] == "source_erased"

# Empty evidence produces no new source record.
manager.on_pre_compress([], evidence_messages=[], require_checkpoint=True)
assert len(TurnOutbox(path).snapshot()) == 2

# Source size is bounded explicitly; a large message is never silently clipped.
try:
    manager.on_pre_compress([{"role": "user", "content": "x" * (8 * 1024 * 1024)}], require_checkpoint=True)
except RuntimeError:
    pass
else:
    raise AssertionError("oversize checkpoint was silently accepted")
assert len(TurnOutbox(path).snapshot()) == 2

# Hermes' actual native image builder reaches the ordinary outbox and wire
# without stringifying content blocks or putting base64 into a text summary.
import base64
from agent.image_routing import build_native_content_parts
image_path = home / "neutral.png"
image_path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j4v8AAAAASUVORK5CYII="))
parts, skipped = build_native_content_parts("Remember this image", [str(image_path)])
assert not skipped and any(part["type"] == "image_url" for part in parts)
plugins.invoke_hook("pre_llm_call", session_id="session-image", task_id="task-image", turn_id="image-a", platform="cli", sender_id="", user_message=parts)
plugins.invoke_hook("post_llm_call", session_id="session-image", task_id="task-image", turn_id="image-a", platform="cli", user_message=parts, assistant_response="Image received", conversation_history=[], model="processor-a")
image_rows = [row for row in TurnOutbox(path).snapshot() if row["turn_id"] == "image-a"]
assert len(image_rows) == 1 and image_rows[0]["payload"]["user_message"] == parts
assert "base64," not in image_rows[0]["payload"].get("summary", "")
TurnOutbox(path).drain(deliver, timeout_seconds=1)
assert any(body.get("user_message", {}).get("content") == parts for _, body in wire)

# A local persistence failure reaches the real compression host and leaves
# its caller's transcript unchanged, without invoking the compressor.
def failed_enqueue(*args, **kwargs):
    raise OSError("disk unavailable")
evidence.TurnOutbox.enqueue = failed_enqueue
def should_not_compress(*args, **kwargs):
    raise AssertionError("compression discarded uncheckpointed evidence")
agent = SimpleNamespace(
    context_compressor=SimpleNamespace(compress=should_not_compress),
    session_id="session-a", model="processor-b", _memory_manager=manager,
    _session_db=None, _compression_feasibility_checked=True,
    compression_checkpoint_required=True, _emit_status=lambda *a, **k: None,
)
try:
    compress_context(agent, raw, "system", force=True)
except CompressionCheckpointUnavailable:
    pass
else:
    raise AssertionError("required checkpoint failure did not stop compression")
assert raw == original
assert provider.get_diagnostics()["checkpoint"]["state"] == "failed"
print("native checkpoint, replay, complete turn and compression failure verified")
'''


def test_native_checkpoint_and_full_turn_capture(artifacts, tmp_path):
    if importlib.util.find_spec("hermes_cli") is None:
        pytest.skip("Install the target Hermes release for native checkpoint qualification")
    env = {key: os.environ[key] for key in ("PATH", "HOME", "SYSTEMROOT", "TMPDIR", "LANG") if key in os.environ}
    env.update({
        "HERMES_HOME": str(tmp_path / "profile"),
        "HERMES_BUNDLED_PLUGINS": str(tmp_path / "bundled"),
        "COLONY_GENERAL_PLUGIN_ACTIVE": "1",
        "COLONY_MEMORY_WORKER_TOOLS": "0",
        "COLONY_MEMORY_TURN_WRITER": "disabled",
        "COLONY_MEMORY_DEFAULT_CONTEXT_AUTHORITY": "owner_system",
    })
    result = subprocess.run(
        [sys.executable, "-I", "-c", PROBE, str(artifacts[3])],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
