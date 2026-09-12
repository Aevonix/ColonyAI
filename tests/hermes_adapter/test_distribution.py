"""Exercise built artifacts and native Hermes loaders, not mock plugin contexts."""

from __future__ import annotations

import configparser
from email.parser import BytesParser
import importlib.util
import json
import os
from pathlib import Path
import tarfile
import zipfile

import pytest
import yaml
from conftest import ROOT, run_python as run


def test_artifacts_contain_canonical_adapters_without_sidecar_or_worker(artifacts):
    _, wheel, source, _ = artifacts
    expected = {
        "apsimo_hermes/__init__.py": "plugins/hermes-plugin/__init__.py",
        "colony_hermes/__init__.py": "compat/colony_hermes/__init__.py",
        "colony_memory/__init__.py": "compat/colony_memory/__init__.py",
        "apsimo_memory/provider.py": "plugins/apsimo-memory/provider.py",
        "apsimo_memory/cli.py": "plugins/apsimo-memory/cli.py",
        "apsimo_hermes/apsimo_hostworker/catalog.py": "hostworker/apsimo_hostworker/catalog.py",
        "apsimo_hermes/apsimo_hostworker/contract.py": "hostworker/apsimo_hostworker/contract.py",
    }
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        for destination, canonical in expected.items():
            assert archive.read(destination) == (ROOT / canonical).read_bytes()
        metadata = BytesParser().parsebytes(archive.read(next(
            name for name in names if name.endswith(".dist-info/METADATA")
        )))
        for package in ("apsimo_hermes", "apsimo_memory", "colony_hermes", "colony_memory"):
            manifest = yaml.safe_load(archive.read(f"{package}/plugin.yaml"))
            assert manifest["version"] == metadata["Version"], package
        assert "apsimo_memory/SKILL.md" in names
        assert "colony_memory/provider.py" not in names
        assert "colony_hermes/client.py" not in names
        assert not any(
            "colony_sidecar" in name or name.startswith("apsimo/") or "/worker.py" in name or "/ops/" in name
            or "hermes-context" in name for name in names
        )
        entries = configparser.ConfigParser()
        entries.read_string(archive.read(next(
            name for name in names if name.endswith("/entry_points.txt")
        )).decode())
        assert dict(entries["hermes_agent.plugins"]) == {"apsimo": "apsimo_hermes", "colony": "apsimo_hermes"}
        assert dict(entries["hermes_agent.memory_providers"]) == {
            "apsimo-memory": "apsimo_memory", "colony-memory": "apsimo_memory"
        }
    # python -m build builds this wheel from the sdist by default. The source
    # archive must therefore carry both canonical catalog inputs as well.
    with tarfile.open(source) as archive:
        names = archive.getnames()
        for canonical in expected.values():
            assert any(name.endswith("/" + canonical) for name in names)


NATIVE_PROBE = r'''
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import sys

installed = Path(sys.argv[1]).resolve()
checkout = Path(sys.argv[2]).resolve()
active = sys.argv[3] == "active"
selected = sys.argv[4]
provider_name = selected + "-memory"
if sys.argv[5]:sys.path.insert(0, sys.argv[5])
sys.path.insert(0, str(installed))

def no_network(*args, **kwargs):
    raise AssertionError("Native adapter qualification must not contact a service")
socket.socket.connect = no_network
socket.create_connection = no_network

home = Path(os.environ["HERMES_HOME"])
home.mkdir(mode=0o700)
Path(os.environ["HERMES_BUNDLED_PLUGINS"]).mkdir()
config = {
    "plugins": {
        "enabled": [selected] if active else [],
        selected: {
            "owner_contact_id": "test-owner",
            "enabled_read_tools": [selected + "_autonomy_status"],
            "turn_outbox_path": str(home / "state" / "turns.sqlite3"),
        },
    },
    "memory": {"provider": provider_name if active else "builtin"},
}
(home / "config.yaml").write_text(json.dumps(config))

from hermes_cli.plugins import discover_entrypoint_manifests, get_plugin_manager
from plugins.memory import (
    discover_plugin_cli_commands, find_provider_dir,
    find_provider_entry_point, load_memory_provider,
)
from agent.memory_provider import MemoryProvider

manifests = {item.name: item for item in discover_entrypoint_manifests()}
assert manifests[selected].source == "entrypoint"
assert manifests[selected].kind == "standalone"
entry = find_provider_entry_point(provider_name)
assert entry is not None and entry.value == "apsimo_memory"
assert find_provider_dir(provider_name).resolve().is_relative_to(installed)

manager = get_plugin_manager()
manager.discover_and_load()
loaded = manager._plugins.get(selected)
if not active:
    assert loaded is None or not loaded.enabled
    assert "colony_hermes" not in sys.modules and "apsimo_hermes" not in sys.modules
    assert discover_plugin_cli_commands() == []
    assert not (home / "state" / "turns.sqlite3").exists()
else:
    assert loaded is not None and loaded.enabled, getattr(loaded, "error", None)
    assert Path(loaded.module.__file__).resolve().is_relative_to(installed)
    assert selected + "_autonomy_status" in loaded.tools_registered
    assert sum(bool(item.tools_registered) for item in manager._plugins.values()) == 1
    assert len(manager._hooks["pre_llm_call"]) == 1
    assert {"pre_llm_call", "post_llm_call", "transform_llm_output"}.issubset(
        loaded.hooks_registered
    )
    assert loaded.middleware_registered
    assert (home / "state" / "turns.sqlite3").is_file()
    previous_recall_hooks = len(manager._hooks.get("pre_llm_call", []))
    provider = load_memory_provider(provider_name)
    assert isinstance(provider, MemoryProvider) and provider.name == "apsimo"
    assert Path(sys.modules[type(provider).__module__].__file__).resolve().is_relative_to(installed)
    assert len(manager._hooks["pre_llm_call"]) == previous_recall_hooks + 1

    commands = discover_plugin_cli_commands()
    assert len(commands) == 1 and commands[0]["name"] == provider_name
    parser = argparse.ArgumentParser()
    commands[0]["setup_fn"](parser)
    args = parser.parse_args(["status", "--url", "http://127.0.0.1:7777"])
    import httpx
    calls = []
    def response(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, url))
    httpx.get = lambda url, **kw: response("GET", url, **kw)
    httpx.post = lambda url, **kw: response("POST", url, **kw)
    args.func(args)
    assert calls[-1][:2] == ("GET", "http://127.0.0.1:7777/v1/host/health")
    args = parser.parse_args(["goals", "--status", "blocked"])
    args.func(args)
    assert calls[-1][2]["params"] == {"status_filter": "blocked"}
    args = parser.parse_args(["context", "--query", "remember", "--contact", "test-contact"])
    args.func(args)
    assert calls[-1][2]["json"]["context"]["contact_id"] == "test-contact"
    args = parser.parse_args(["sync", "--user", "hello", "--assistant", "reply"])
    args.func(args)
    assert calls[-1][2]["json"]["assistant_message"]["content"] == "reply"
    try:
        parser.parse_args(["sync", "--user", "hello"])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("Native CLI accepted an incomplete sync")
    def unavailable(*args, **kwargs):
        raise httpx.ConnectError("offline")
    httpx.get = unavailable
    args = parser.parse_args(["status"])
    try:
        args.func(args)
    except SystemExit as error:
        assert error.code == 1
    else:
        raise AssertionError("Native CLI reported success for an offline sidecar")

for module in tuple(sys.modules.values()):
    filename = getattr(module, "__file__", None)
    if filename:
        for source in ("plugins", "hostworker", "sidecar"):
            assert not Path(filename).resolve().is_relative_to(checkout / source), filename
assert not any(name == "apsimo" or name.startswith(("apsimo.", "colony_sidecar")) for name in sys.modules)
print(json.dumps({"hermes": importlib.metadata.version("hermes-agent"), "active": active}))
'''


@pytest.mark.parametrize("selected", ["apsimo", "colony"])
@pytest.mark.parametrize("state", ["active", "inactive"])
def test_wheel_uses_native_hermes_discovery_and_loaders(artifacts, tmp_path, state, selected):
    if importlib.util.find_spec("hermes_cli") is None:
        pytest.skip("Install the target Hermes release to run native-loader qualification")
    _, _, _, installed = artifacts
    env = {key: os.environ[key] for key in (
        "PATH", "HOME", "SYSTEMROOT", "TMPDIR", "TEMP", "LANG", "LC_ALL",
    ) if key in os.environ}
    env.update({
        "HERMES_HOME": str(tmp_path / "profile"),
        "HERMES_BUNDLED_PLUGINS": str(tmp_path / "bundled"),
        "COLONY_GENERAL_PLUGIN_ACTIVE": "1",
        "COLONY_MEMORY_WORKER_TOOLS": "0",
        "COLONY_MEMORY_TURN_WRITER": "disabled",
    })
    result = run(
        "-I", "-c", NATIVE_PROBE, installed, ROOT, state, selected,
        os.environ.get("HERMES_TEST_SOURCE", ""),
        cwd=tmp_path, env=env,
    )
    assert json.loads(result.stdout.splitlines()[-1])["active"] == (state == "active")
