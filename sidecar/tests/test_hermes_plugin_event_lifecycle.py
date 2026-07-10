"""The Colony event subscriber is process-scoped, not turn-scoped."""

from __future__ import annotations

import importlib.util
import pathlib
import sys


_PLUGIN_DIR = (
    pathlib.Path(__file__).resolve().parents[2] / "plugins" / "hermes-plugin"
)


def _load_plugin():
    name = "colony_hermes_event_lifecycle_test"
    if name in sys.modules:
        return sys.modules[name]
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


class _Client:
    def __init__(self, *_args, **_kwargs):
        pass

    def get(self, *_args, **_kwargs):
        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"contact_id": "cid-owner"}

        return _Response()


class _Context:
    def __init__(self):
        self.hooks = {}

    def register_tool(self, **_kwargs):
        return None

    def register_hook(self, name, fn):
        self.hooks[name] = fn


def test_session_end_does_not_stop_shared_event_subscriber():
    module = _load_plugin()
    context = _Context()
    holder = {}

    class _Subscriber:
        def __init__(self, *_args, **_kwargs):
            self.started = False
            self.stop_calls = 0
            holder["subscriber"] = self

        def start(self):
            self.started = True

        async def stop(self):
            self.stop_calls += 1

    original = (
        module.ColonyClient,
        module.ColonyEventSubscriber,
        module._configure_colony_llm,
    )
    module.ColonyClient = _Client
    module.ColonyEventSubscriber = _Subscriber
    module._configure_colony_llm = lambda *_args, **_kwargs: None
    try:
        module.register(context)
        subscriber = holder["subscriber"]
        assert subscriber.started is True

        context.hooks["on_session_end"](session_id="turn-1")
        context.hooks["on_session_end"](session_id="turn-2")

        assert subscriber.stop_calls == 0
        assert module._event_subscriber is subscriber
    finally:
        (
            module.ColonyClient,
            module.ColonyEventSubscriber,
            module._configure_colony_llm,
        ) = original
