"""The governed general plugin has no process-scoped event lifecycle."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "hermes-plugin"


def _load_plugin():
    name = "colony_hermes_event_lifecycle_test"
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


def test_general_plugin_exports_no_process_scoped_event_subscriber():
    module = _load_plugin()
    assert module.GOVERNED_EVENT_TYPES == ()
    assert not hasattr(module, "ColonyEventSubscriber")
    assert not hasattr(module, "_event_subscriber")
    source = (_PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    assert "register_hook(\"on_session_end\"" not in source
    assert "websockets" not in source
