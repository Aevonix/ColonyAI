"""No shared replay/cache path may reintroduce cross-viewer event state."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


_EVENTS_PATH = (
    Path(__file__).resolve().parents[2] / "plugins" / "hermes-plugin" / "events.py"
)


def _load_events_module():
    name = "colony_hermes_events_replay_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _EVENTS_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_event_catalog_is_explicitly_empty_and_immutable():
    module = _load_events_module()
    assert module.GOVERNED_EVENT_TYPES == ()
    assert module.event_catalog() == ()
    assert not hasattr(module, "EventCache")
    assert not hasattr(module, "ColonyEventSubscriber")


def test_event_module_has_no_network_or_replay_state_dependency():
    source = _EVENTS_PATH.read_text(encoding="utf-8")
    assert "websockets" not in source
    assert "asyncio" not in source
    assert "last_event" not in source
    assert "/v1/host/events" not in source
