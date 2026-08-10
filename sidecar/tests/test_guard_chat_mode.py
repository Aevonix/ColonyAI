"""ResponseGuard mode resolution for Hermes transform output governance."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "hermes-plugin"


def _load_plugin():
    name = "colony_hermes_plugin_guard_mode_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        name,
        _PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_mode_defaults_and_legacy_shadow_inheritance(monkeypatch):
    module = _load_plugin()
    monkeypatch.delenv("COLONY_GUARD_CHAT_MODE", raising=False)
    monkeypatch.delenv("COLONY_GUARD_CHAT_SHADOW", raising=False)
    assert module._guard_chat_mode() == "off"
    monkeypatch.setenv("COLONY_GUARD_CHAT_SHADOW", "1")
    assert module._guard_chat_mode() == "shadow"
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "off")
    assert module._guard_chat_mode() == "off"


def test_explicit_enforce_is_not_downgraded_by_obsolete_post_hook_probe(monkeypatch):
    module = _load_plugin()
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "enforce")
    assert module._guard_chat_mode() == "enforce"
    assert not hasattr(module, "_effective_guard_chat_mode")
    assert not hasattr(module, "_host_supports_post_reply_mutation")


def test_invalid_explicit_mode_uses_legacy_default(monkeypatch):
    module = _load_plugin()
    monkeypatch.setenv("COLONY_GUARD_CHAT_MODE", "invalid")
    monkeypatch.delenv("COLONY_GUARD_CHAT_SHADOW", raising=False)
    assert module._guard_chat_mode() == "off"
    monkeypatch.setenv("COLONY_GUARD_CHAT_SHADOW", "yes")
    assert module._guard_chat_mode() == "shadow"


def test_guard_is_wired_to_transform_not_post_hook():
    source = (_PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    assert 'register_hook("transform_llm_output", transform_llm_output)' in source
    post_body = source.split("def post_llm_call", 1)[1].split(
        "def transform_llm_output", 1
    )[0]
    assert "/v1/host/response-guard/check" not in post_body
